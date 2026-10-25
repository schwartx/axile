"""执行结果摘要辅助函数."""

from collections.abc import Mapping
from math import isfinite
from typing import Any

from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import is_degraded_snapshot_source

# 判定「持仓量约等于零」的绝对容差。
_QTY_EPS = 1e-9
# 判定「到位」的相对带宽：``|after - target| <= |target| * 该比例`` 视为到位。
_ATTAINED_TOLERANCE = 0.01


def build_execution_outcome_details(result: Mapping[str, object]) -> dict[str, object]:
    """转发当前结果；审计写入阶段统一规范展示字段。"""
    forwarded: dict[str, object] = {
        key: result[key]
        for key in ("status", "error", "reason_code", "outcome", "outcome_reason", "symbol_results")
        if key in result
    }
    memory = result.get("memory")
    if isinstance(memory, dict):
        context = {
            key: memory[key] for key in ("message", "symbol_decision_reason_code") if isinstance(memory.get(key), str)
        }
        if context:
            forwarded["memory"] = context
    forwarded["outcome_symbols"] = []  # 审计写入阶段从规范化结果生成最终列表。
    return forwarded


def build_execution_summary_from_symbol_results(result: dict[str, object]) -> dict[str, int]:
    """
    根据按品种执行结果生成执行摘要.

    Parameters
    ----------
    result : dict[str, object]
        执行结果的 JSON 化内容。

    Returns
    -------
    dict[str, int]
        按品种统计得到的执行摘要，含 ``symbols_total``、``symbols_succeeded``、
        ``symbols_failed``、``symbols_noop`` 四项。

    Notes
    -----
    ``symbols_succeeded`` 仍按「总数减失败」计（含空跑），保持向后兼容；
    ``symbols_noop`` 单列出 ``NOOP`` 空跑品种数，供前端折叠无变化的执行，
    「真实动作成功数」= ``symbols_succeeded - symbols_noop``。
    """
    symbol_results_raw = result.get("symbol_results")
    if not isinstance(symbol_results_raw, dict) or not symbol_results_raw:
        return {}

    symbols_total = len(symbol_results_raw)
    symbols_failed = 0
    symbols_noop = 0
    for symbol_result in symbol_results_raw.values():
        if not isinstance(symbol_result, dict):
            continue
        status_raw = symbol_result.get("status")
        try:
            status = ExecutionStatus(status_raw) if isinstance(status_raw, str) else None
        except ValueError:
            status = None
        if status in {ExecutionStatus.BLOCKED, ExecutionStatus.PARTIAL, ExecutionStatus.FAILED}:
            symbols_failed += 1
        elif status == ExecutionStatus.NOOP:
            symbols_noop += 1

    return {
        "symbols_total": symbols_total,
        "symbols_succeeded": symbols_total - symbols_failed,
        "symbols_failed": symbols_failed,
        "symbols_noop": symbols_noop,
    }


def count_orders_from_symbol_results(result: dict[str, object]) -> int:
    """
    根据按品种执行结果统计订单总数.

    Parameters
    ----------
    result : dict[str, object]
        执行结果的 JSON 化内容。

    Returns
    -------
    int
        统计得到的订单总数。
    """
    symbol_results_raw = result.get("symbol_results")
    if not isinstance(symbol_results_raw, dict) or not symbol_results_raw:
        return 0

    orders_count = 0
    for symbol_result in symbol_results_raw.values():
        if not isinstance(symbol_result, dict):
            continue
        orders_raw = symbol_result.get("orders")
        if isinstance(orders_raw, list):
            orders_count += len(orders_raw)
    return orders_count


def _to_float(value: object) -> float | None:
    """把任意标量安全转为浮点数；无法转换时返回 ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _signed_position_qty(position: Mapping[str, Any]) -> float:
    """按持仓方向给数量定号：空头为负、其余为正.

    Parameters
    ----------
    position : Mapping[str, Any]
        序列化后的持仓字典，含 ``volume`` 与 ``direction``。

    Returns
    -------
    float
        带符号持仓量；``空头``/``short`` 记负，其余记正。
    """
    volume = _to_float(position.get("volume")) or 0.0
    direction = str(position.get("direction") or "")
    is_short = "空" in direction or "short" in direction.lower()
    return -abs(volume) if is_short else abs(volume)


def _positions_by_symbol(account_assets: Mapping[str, Any] | None) -> dict[str, float]:
    """把账户快照的持仓列表折叠成 ``symbol -> 带符号持仓量`` 映射."""
    result: dict[str, float] = {}
    if not isinstance(account_assets, dict):
        return result
    positions = account_assets.get("positions")
    if not isinstance(positions, list):
        return result
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = position.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        result[symbol] = result.get(symbol, 0.0) + _signed_position_qty(position)
    return result


def _converted_value(native_value: float | None, conversion: object) -> float | None:
    """使用逐笔冻结的汇率换算金额；缺少换算契约的渠道保留原生口径。"""
    if native_value is None or not isinstance(conversion, dict):
        return native_value
    rate = _to_float(conversion.get("rate"))
    if rate is None or not isfinite(rate) or rate <= 0:
        return None
    return native_value * rate


def _signed_filled(orders: object) -> tuple[float, float | None, float, float | None]:
    """汇总带符号成交量、账户币种金额、原生金额及原生加权均价.

    Parameters
    ----------
    orders : object
        序列化后的订单列表；非列表时按空处理。

    Returns
    -------
    tuple[float, float | None, float, float | None]
        买入为正、卖出为负；账户币种金额逐笔换算。均价由原生金额和数量求得，
        保持与交易所价格及 TCA 基准同币种；换算契约无效时账户币种金额为 ``None``。
    """
    if not isinstance(orders, list):
        return 0.0, 0.0, 0.0, None
    qty = 0.0
    value: float | None = 0.0
    native_value = 0.0
    for order in orders:
        if not isinstance(order, dict):
            continue
        filled = abs(_to_float(order.get("filled_volume")) or 0.0)
        price = _to_float(order.get("avg_price")) or 0.0
        direction = str(order.get("direction") or "")
        sign = -1.0 if ("SELL" in direction.upper() or "卖" in direction) else 1.0
        qty += sign * filled
        native = sign * filled * price
        native_value += native
        converted = _converted_value(native, order.get("value_conversion"))
        value = value + converted if value is not None and converted is not None else None
    avg_price = abs(native_value) / abs(qty) if abs(qty) > _QTY_EPS else None
    return qty, value, native_value, avg_price


def _as_dict(value: object) -> dict[str, Any] | None:
    """把值收敛为字典或 ``None``。"""
    return value if isinstance(value, dict) else None


def _enum_tail(value: object) -> str:
    """把 ``OrderType.LIMIT`` 这类枚举串裁成尾段 ``LIMIT``。"""
    s = str(value or "")
    return s.rsplit(".", 1)[-1] if "." in s else s


def _order_side(direction: str) -> str:
    """订单方向串 → ``buy``/``sell``/``none``。"""
    d = direction.upper()
    if "SELL" in d or "卖" in direction:
        return "sell"
    if "BUY" in d or "买" in direction:
        return "buy"
    return "none"


def _arrival_prices(symbol_result: Mapping[str, Any]) -> tuple[float | None, float | None, float | None]:
    """从 ``first_tick`` 取到达价：买一/卖一/中间价（缺买卖一时退化为最新价）。"""
    ft = _as_dict(symbol_result.get("first_tick"))
    if ft is None:
        return None, None, None
    bid = _to_float(ft.get("bid_price"))
    ask = _to_float(ft.get("ask_price"))
    last = _to_float(ft.get("last_price"))
    mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else last
    return bid, ask, mid


def _slippage_bps(avg_price: float | None, mid: float | None, is_sell: bool) -> float | None:
    """相对到达中间价的滑点（bps，有利为正）：卖出高于中间价、买入低于中间价记正。"""
    if avg_price is None or mid is None or mid <= 0:
        return None
    raw = (avg_price - mid) / mid * 1e4
    return raw if is_sell else -raw


def _liquidity(avg_price: float | None, bid: float | None, ask: float | None, is_sell: bool) -> str | None:
    """主/被动判读：贴到达对手价成交=被动（挂单被吃）、穿越到同侧=主动（吃单）。"""
    if avg_price is None or bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    tol = (ask - bid) / 2 if ask > bid else ask * 1e-4
    if is_sell:
        if avg_price >= ask - tol:
            return "passive"
        if avg_price <= bid + tol:
            return "aggressive"
    else:
        if avg_price <= bid + tol:
            return "passive"
        if avg_price >= ask - tol:
            return "aggressive"
    return "mixed"


def _trade_fee(trade: Mapping[str, Any]) -> tuple[float, str | None]:
    """从成交记录 ``extra`` 取手续费与计费资产。"""
    extra = _as_dict(trade.get("extra")) or {}
    fee = _to_float(extra.get("commission")) or 0.0
    asset = extra.get("commission_asset")
    return fee, asset if isinstance(asset, str) and asset else None


def _build_orders_tree(symbol_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """裁剪出 订单→成交 树：每笔订单挂上按 ``order_id`` 归组的成交明细。"""
    orders_raw = symbol_result.get("orders")
    trades_raw = symbol_result.get("trades")
    orders = orders_raw if isinstance(orders_raw, list) else []
    trades = trades_raw if isinstance(trades_raw, list) else []

    by_order: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        if not isinstance(t, dict):
            continue
        fee, fee_asset = _trade_fee(t)
        conversion = _as_dict(t.get("value_conversion")) or {}
        native_value = _to_float(t.get("trade_value"))
        by_order.setdefault(str(t.get("order_id") or ""), []).append(
            {
                "price": _to_float(t.get("trade_price")),
                "volume": _to_float(t.get("trade_volume")),
                "value": _converted_value(native_value, t.get("value_conversion")),
                "native_value": native_value,
                "price_currency": conversion.get("price_currency"),
                "value_currency": conversion.get("value_currency"),
                "time": t.get("trade_time"),
                "fee": fee,
                "fee_asset": fee_asset,
            }
        )

    tree: list[dict[str, Any]] = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        oid = str(o.get("order_id") or "")
        extra = _as_dict(o.get("extra")) or {}
        client_id = extra.get("client_order_id")
        tree.append(
            {
                "order_id": oid,
                "side": _order_side(str(o.get("direction") or "")),
                "order_type": _enum_tail(o.get("order_type")),
                "price": _to_float(o.get("price")),
                "avg_price": _to_float(o.get("avg_price")),
                "value_conversion": o.get("value_conversion"),
                "volume": _to_float(o.get("volume")),
                "filled_volume": _to_float(o.get("filled_volume")),
                "status": str(o.get("status") or ""),
                "client_order_id": client_id if isinstance(client_id, str) else None,
                "trades": by_order.get(oid, []),
            }
        )
    return tree


def _symbol_tca(symbol_result: Mapping[str, Any], avg_price: float | None, is_sell: bool) -> dict[str, Any]:
    """逐只执行质量：到达价滑点、主/被动、费用、成交率、订单/成交笔数。"""
    orders_raw = symbol_result.get("orders")
    trades_raw = symbol_result.get("trades")
    orders = orders_raw if isinstance(orders_raw, list) else []
    trades = trades_raw if isinstance(trades_raw, list) else []

    bid, ask, mid = _arrival_prices(symbol_result)
    ordered = 0.0
    filled = 0.0
    for o in orders:
        if isinstance(o, dict):
            ordered += abs(_to_float(o.get("volume")) or 0.0)
            filled += abs(_to_float(o.get("filled_volume")) or 0.0)
    fee_total = 0.0
    fee_asset: str | None = None
    for t in trades:
        if isinstance(t, dict):
            f, a = _trade_fee(t)
            fee_total += f
            fee_asset = fee_asset or a

    return {
        "n_orders": len(orders),
        "n_trades": len(trades),
        "arrival_bid": bid,
        "arrival_ask": ask,
        "arrival_mid": mid,
        "slippage_bps": _slippage_bps(avg_price, mid, is_sell),
        "liquidity": _liquidity(avg_price, bid, ask, is_sell),
        "fill_ratio": (filled / ordered) if ordered > _QTY_EPS else None,
        "fee": fee_total,
        "fee_asset": fee_asset,
    }


def _account_equity(account_assets: Mapping[str, Any] | None) -> float | None:
    """从账户快照取总权益（``total_asset``）；缺失时返回 ``None``."""
    if not isinstance(account_assets, dict) or is_degraded_snapshot_source(_account_source(account_assets)):
        return None
    equity = _to_float(account_assets.get("total_asset"))
    return equity if equity is not None and isfinite(equity) else None


def _account_source(account_assets: Mapping[str, Any] | None) -> str:
    """取账户快照的来源标；``None`` 快照视为 ``unavailable``."""
    if not isinstance(account_assets, dict):
        return "unavailable"
    source = account_assets.get("source")
    return source if isinstance(source, str) and source else "real"


def _build_symbol_row(
    symbol: str,
    symbol_result: Mapping[str, Any],
    before_qty: float | None,
    after_qty: float | None,
    account_currency: str | None = None,
) -> dict[str, Any]:
    """构造单只对账行：意图/成交/前后持仓/到位度/漂移."""
    target = _to_float(symbol_result.get("target_volume"))
    filled, filled_value, native_value, avg_price = _signed_filled(symbol_result.get("orders"))
    moved = after_qty - before_qty if after_qty is not None and before_qty is not None else None
    drift = moved - filled if moved is not None else None

    attained_ratio: float | None = None
    reached: bool | None = None
    if target is not None and after_qty is not None:
        if abs(target) <= _QTY_EPS:
            reached = abs(after_qty) <= max(abs(before_qty or 0.0), 1.0) * _ATTAINED_TOLERANCE
            attained_ratio = 1.0 if reached else None
        else:
            attained_ratio = after_qty / target
            reached = abs(after_qty - target) <= abs(target) * _ATTAINED_TOLERANCE

    sizing = symbol_result.get("sizing")
    conversion = _as_dict(sizing.get("value_conversion")) if isinstance(sizing, dict) else None
    conversion = conversion or {}
    return {
        "symbol": symbol,
        "status": symbol_result.get("status"),
        "error": symbol_result.get("error"),
        "outcome": symbol_result.get("outcome"),
        "outcome_reason": symbol_result.get("outcome_reason"),
        "final_volume": symbol_result.get("final_volume"),
        "target": target,
        "filled": filled,
        "filled_value": filled_value,
        "value_currency": conversion.get("value_currency") or account_currency,
        "filled_native_value": native_value,
        "native_currency": conversion.get("price_currency") or account_currency,
        "avg_price": avg_price,
        "before": before_qty,
        "after": after_qty,
        "moved": moved,
        "drift": drift,
        "attained_ratio": attained_ratio,
        "reached": reached,
        "sizing": sizing if isinstance(sizing, dict) else None,
        "tca": _symbol_tca(symbol_result, avg_price, filled < 0),
        "orders": _build_orders_tree(symbol_result),
    }


def _observed_quantity(assets: Mapping[str, Any] | None, positions: dict[str, float], symbol: str) -> float | None:
    """降级快照不提供持仓事实；自定义仿真来源保留原标记和数量。"""
    if is_degraded_snapshot_source(_account_source(assets)) or not isinstance(
        assets.get("positions") if assets else None, list
    ):
        return None
    quantity = positions.get(symbol, 0.0)
    return quantity if isfinite(quantity) else None


def build_symbol_reconciliation(
    result: Mapping[str, object],
    before_account_assets: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """由执行结果与执行前账户快照派生逐只对账.

    把三处分散的口径——意图（``target_volume``）、成交（``orders[].filled_volume``）、
    执行前后持仓（前/后账户快照的 ``positions``）——在服务端 join 成唯一权威口径，避免下游
    各自计算导致「到位度」分叉。逐只给出 ``target``（意图带号目标量）、``filled``（本次带号
    成交量）、``before``/``after``（执行前后带号持仓）、``moved = after - before``（本次实际
    净变动）、``drift = moved - filled``（成交与实际变动之差，非零即滑点/费用/外部变动的信号）、
    ``attained_ratio = after / target``（到位度）与 ``reached``（是否到位）。

    Parameters
    ----------
    result : Mapping[str, object]
        ``UnifiedStandardOutput`` 的 JSON 序列化，含 ``symbol_results`` 与执行后 ``account_assets``。
    before_account_assets : Mapping[str, Any] | None
        执行前账户快照的 JSON 序列化；``None`` 表示读取不可用，账户级来源记 ``unavailable``。

    Returns
    -------
    dict[str, Any]
        含 ``account``（前后权益与来源标）与 ``symbols``（逐只对账行）两部分；``symbol_results``
        缺失时返回空的 ``symbols`` 列表。
    """
    after_account_assets = result.get("account_assets")
    after_assets_dict = after_account_assets if isinstance(after_account_assets, dict) else None

    account_block = {
        "equity_before": _account_equity(before_account_assets),
        "equity_after": _account_equity(after_assets_dict),
        "source_before": _account_source(before_account_assets),
        "source_after": _account_source(after_assets_dict),
    }

    symbol_results_raw = result.get("symbol_results")
    if not isinstance(symbol_results_raw, dict) or not symbol_results_raw:
        return {"account": account_block, "symbols": []}

    before_map = _positions_by_symbol(before_account_assets)
    after_map = _positions_by_symbol(after_assets_dict)

    rows: list[dict[str, Any]] = []
    for symbol, symbol_result in symbol_results_raw.items():
        if not isinstance(symbol_result, dict):
            continue
        before_qty = _observed_quantity(before_account_assets, before_map, symbol)
        after_qty = _observed_quantity(after_assets_dict, after_map, symbol)
        if after_qty is None:
            # 最后一次账户查询失败时，仍可回放算法先前确认过的单品种持仓。
            final_volume = _to_float(symbol_result.get("final_volume"))
            after_qty = final_volume if final_volume is not None and isfinite(final_volume) else None
        rows.append(
            _build_symbol_row(
                symbol,
                symbol_result,
                before_qty,
                after_qty,
                after_assets_dict.get("currency") if after_assets_dict else None,
            )
        )

    return {"account": account_block, "symbols": rows}
