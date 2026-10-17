"""在账户已有 worker 中读取近期记录；历史查询与纯观测读取明确分开。"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.executor.models.unified_order import TradeRecord
from axile.server.account_orders_runtime import public_offset, public_order
from axile.server.db.models.account_order import AccountActivityPublic, AccountOrderPublic, AccountTradePublic


def _number(value: Any) -> float | None:
    """只发布原生有效数值，缺失与零分开，不用估算值补手续费或金额。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _trade_attributes(extra: dict[str, Any], raw: dict[str, Any]) -> dict[str, str]:
    """只发布成交本身的渠道核对信息，不读取订单当前状态或原始载荷。"""
    values = {**raw, **{key: value for key, value in extra.items() if value is not None and value != ""}}
    if extra.get("channel_type") == "gm" and not values.get("gm_symbol"):
        values["gm_symbol"] = raw.get("symbol")
    fields = (
        "exchange_id",
        "order_sys_id",
        "order_ref",
        "event_date",
        "event_clock",
        "tq_symbol",
        "gm_symbol",
        "cl_ord_id",
        "exchange_order_id",
        "client_order_id",
        "execution_type",
        "order_status",
    )
    return {
        key: str(values[key])
        for key in fields
        if isinstance(values.get(key), (str, int)) and not isinstance(values[key], bool) and str(values[key])
    }


def _trade(trade: TradeRecord) -> AccountTradePublic:
    extra = trade.extra
    raw = extra.get("raw_trade_data", {})
    raw = raw if isinstance(raw, dict) else {}
    direction = extra.get("direction", "")
    direction = str(getattr(direction, "value", direction))
    if isinstance(raw, dict) and not direction:
        side = raw.get("side")
        direction = {1: "BUY", 2: "SELL"}.get(side, "") if isinstance(side, int) and not isinstance(side, bool) else ""
    if extra.get("channel_type") == "ctp" or direction in {"0", "1"}:
        direction = {"0": "BUY", "1": "SELL"}.get(direction, direction)
    maker = extra.get("is_maker")
    return AccountTradePublic(
        trade_id=trade.trade_id,
        attributes=_trade_attributes(extra, raw),
        order_id=trade.order_id,
        symbol=trade.symbol,
        direction=direction,
        offset=public_offset(extra),
        time=trade.trade_time,
        price=trade.trade_price,
        volume=trade.trade_volume,
        value=trade.trade_value,
        trading_day=_text(extra.get("trading_day")),
        hedge_flag=_text(extra.get("hedge_flag")),
        exchange_trade_id=_text(extra.get("exchange_trade_id")),
        amount=_number(raw.get("amount")),
        commission=_number(extra.get("commission", raw.get("commission"))),
        commission_asset=_text(extra.get("commission_asset")),
        cost=_number(raw.get("cost")),
        realized_pnl=_number(extra.get("realized_pnl")),
        is_maker=maker if isinstance(maker, bool) else None,
        position_side=_text(extra.get("position_side")),
    )


def _time(value: str | None) -> float:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))).timestamp()
    except ValueError:
        return 0


def _query(executor: Any, symbol: str | None) -> None:
    activity = executor.account_orders.activity
    channel = str(getattr(executor.channel_type, "value", executor.channel_type))
    errors: list[str] = []
    if channel not in {"ctp", "tq", "gm"}:
        if symbol is None and _query_symbol(executor, None, errors, allow_unsupported=True):
            activity.mark_query(
                symbol, scope="渠道近期全账户记录，每类最多1000笔；时间范围由渠道提供；另含本次连接观测", errors=errors
            )
            return
        symbols = [symbol] if symbol else activity.symbols()
        if not symbol:
            try:
                symbols = sorted(set(symbols) | {p.symbol for p in executor.get_account_assets().positions})
            except Exception:
                errors.append("持仓品种读取失败，仅核对已观测品种")
        scope = "渠道近期指定品种记录，单品种最多1000笔；另含本次连接观测"
        if not symbol:
            scope += "；全部视图仅覆盖已观测及持仓品种"
        if len(symbols) > 8:
            errors.append("品种较多，本次仅核对前8个品种；可指定品种继续核对")
        symbols = symbols[:8]
    else:
        symbols = [symbol]
        scope = "柜台当前交易日记录 + 本次连接观测" if channel in {"ctp", "tq"} else "GM 当前可查询记录 + 本次连接观测"
    deadline = clock_monotonic() + 20
    for item in symbols:
        if clock_monotonic() >= deadline:
            errors.append("核对时间已达上限，剩余品种未核对")
            break
        _query_symbol(executor, item, errors)
    activity.mark_query(symbol, scope=scope, errors=errors)


def _query_symbol(executor: Any, symbol: str | None, errors: list[str], *, allow_unsupported: bool = False) -> bool:
    """可选账户查询只在明确不支持时回退，不把失败当作空的成功结果。"""
    activity = executor.account_orders.activity
    supported = True
    for label, query, observe in (
        ("委托", executor.query_recent_orders, activity.observe_order),
        ("成交", executor.query_recent_trades, activity.observe_trade),
    ):
        try:
            rows = query(symbol)
            for row in rows:
                observe(row)
            if len(rows) >= 1000:
                errors.append(f"{symbol or '账户'}{label}记录较多，查询结果可能被渠道截断")
        except NotImplementedError:
            supported = False
            if not allow_unsupported:
                errors.append(f"{symbol or '账户'}{label}查询尚未支持，保留最后观测记录")
        except Exception:
            errors.append(f"{symbol or '账户'}{label}核对失败，保留最后观测记录")
    return supported


def run_activity_command(executor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """显式核对才访问渠道；分页及自动刷新只复制连接内记录。"""
    symbol = payload.get("symbol") or None
    if payload.get("refresh"):
        _query(executor, symbol)
    activity = executor.account_orders.activity
    snapshot = activity.snapshot(symbol)
    limit = min(500, max(1, int(payload.get("limit", 100))))
    orders = sorted(
        [AccountOrderPublic.model_validate(public_order(order)) for order in snapshot["orders"]],
        key=lambda order: (_time(order.create_time), order.order_id),
        reverse=True,
    )
    trades = sorted(snapshot["trades"], key=lambda trade: (_time(trade.trade_time), trade.trade_id), reverse=True)
    ended_orders = [order for order in orders if order.status in {"已撤销", "已拒绝", "已过期"}]
    failures = sorted(snapshot["failures"], key=lambda row: (_time(row["time"]), row["event_id"]), reverse=True)
    result = AccountActivityPublic(
        channel=str(getattr(executor.channel_type, "value", executor.channel_type)),
        orders=orders[:limit],
        ended_orders=ended_orders[:limit],
        trades=[_trade(trade) for trade in trades[:limit]],
        failures=failures[:limit],
        scope=snapshot["scope"],
        started_at=snapshot["started_at"],
        sampled_at=clock_now_iso(),
        queried_at=snapshot["queried_at"],
        errors=snapshot["errors"]
        + ([f"本次连接记录较多，每类仅保留最近{snapshot['capacity']}笔"] if snapshot["dropped"] else []),
        symbols=activity.symbols(),
        limit=limit,
        truncated=any(len(rows) > limit for rows in (orders, ended_orders, trades, failures)),
    )
    return result.model_dump(mode="json")
