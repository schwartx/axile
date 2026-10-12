"""文档专用离线数据替身；不连接天勤，不验证或执行真实下单算法。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.algorithms.utils.clock import clock_now_iso, clock_now_ms
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus, TargetSizingDecision
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.models.value_conversion import PriceValueConversion

ROOT = Path(os.environ.get("AXILE_DOCS_STATE_DIR", "/tmp/axile-docs-20261012"))
QUOTES = {"rb2701": (3200.0, 10.0), "ag2612": (8000.0, 15.0)}


class DocumentationExecutor(AbstractExecutor):
    """返回可重复的演示资产、行情和执行结果，所有数字均为教学夹具。"""

    def _initialize_connection(self, account_config):
        if account_config.account_mode != "sim" or not account_config.tq_username.startswith("offline-docs"):
            raise ValueError("文档替身仅接受 offline-docs* 用户名和本地模拟模式")
        self.state_file = ROOT / f"{account_config.tq_username}.json"

    def _state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {"positions": {"rb2701": 6, "ag2612": -1}, "equity": 1_000_000.0}

    def _verify_connection(self):
        return True

    def _check_trading_time(self):
        return True

    def get_account_assets(self):
        state = self._state()
        positions = []
        gross = 0.0
        for symbol, qty in state["positions"].items():
            if not qty:
                continue
            price, multiplier = QUOTES[symbol]
            value = abs(qty) * price * multiplier
            gross += value
            positions.append(
                Position(
                    symbol=symbol,
                    volume=abs(qty),
                    available_volume=abs(qty),
                    market_value=value,
                    direction="多头" if qty > 0 else "空头",
                    avg_price=price,
                    extra={"volume_multiple": multiplier, "documentation_fixture": True},
                )
            )
        return UnifiedAccountAssets(
            available_cash=state["equity"] - gross * 0.12,
            total_asset=state["equity"],
            market_value=gross,
            positions=positions,
            currency="CNY",
            source="documentation_fixture",
            update_time=clock_now_iso(),
            extra={"documentation_fixture": True, "channel_type": "tq"},
        )

    def get_market_data(self, symbols):
        result = {}
        for symbol in symbols:
            price, multiplier = QUOTES.get(symbol, (100.0, 10.0))
            result[symbol] = UnifiedPriceData(
                symbol=symbol,
                last_price=price,
                bid_price=price - 1,
                ask_price=price + 1,
                bid_volume=100,
                ask_volume=100,
                volume=10000,
                turnover=price * multiplier * 10000,
                timestamp=clock_now_ms(),
                update_time=clock_now_iso(),
                extra={"volume_multiple": multiplier, "price_tick": 1.0, "documentation_fixture": True},
            )
        return result

    def _fixture_orders(self, symbol, delta, fill, price, multiplier, partial, blocked):
        """构造教学订单和成交，不调用渠道接口。"""
        if not delta or blocked:
            return [], []
        order_id = f"DOCS-{symbol}-{clock_now_ms()}"
        order = UnifiedOrder.create(
            order_id=order_id,
            symbol=symbol,
            direction="BUY" if delta > 0 else "SELL",
            order_type="LIMIT",
            volume=abs(delta),
            price=price,
            filled_volume=abs(fill),
            avg_price=price,
            status=OrderStatus.CANCELED if partial else OrderStatus.FILLED,
            raw_order_data={"offset": "OPEN", "documentation_fixture": True},
        )
        trade = TradeRecord(
            trade_id=f"{order_id}-F1",
            symbol=symbol,
            order_id=order_id,
            trade_time=clock_now_iso(),
            trade_volume=abs(fill),
            trade_price=price,
            trade_value=abs(fill) * price * multiplier,
            value_conversion=PriceValueConversion(
                price_currency="CNY",
                value_currency="CNY",
                rate=multiplier,
                reference={"unit_multiplier": multiplier},
            ),
            extra={"documentation_fixture": True, "offset": "OPEN"},
        )
        return [order], [trade]

    def _symbol_result(self, symbol, weight, state, scenario, forbidden):
        """根据教学状态构造一个品种的结果及换算依据。"""
        price, multiplier = QUOTES[symbol]
        before = state["positions"].get(symbol, 0)
        raw = weight * state["equity"] / (price * multiplier)
        target = int(raw)
        delta = target - before
        blocked = symbol in forbidden
        partial = scenario.get("partial_symbol") == symbol and abs(delta) > 1
        fill = (1 if delta > 0 else -1) if partial else delta
        if blocked:
            fill = 0
        after = before + fill
        state["positions"][symbol] = after
        orders, trades = self._fixture_orders(symbol, delta, fill, price, multiplier, partial, blocked)
        status, error, reason_code = self._symbol_outcome(blocked, partial, delta)
        return AlgorithmResult(
            symbol=symbol,
            algorithm="TARGET-POS-TASK",
            status=status,
            orders=orders,
            trades=trades,
            target_volume=target,
            final_volume=after,
            first_tick=self.get_market_data([symbol])[symbol],
            error=error,
            reason_code=reason_code,
            sizing=TargetSizingDecision(
                symbol=symbol,
                account_weight=weight,
                equity=state["equity"],
                reference_price=price,
                unit_multiplier=multiplier,
                unit_notional=price * multiplier,
                target_notional=weight * state["equity"],
                raw_quantity=raw,
                target_quantity=target,
                current_quantity=before,
                quantity_step=1,
            ),
            memory={"documentation_fixture": True},
        )

    @staticmethod
    def _symbol_outcome(blocked, partial, delta):
        """选择教学状态和清楚标注的夹具原因。"""
        if blocked:
            return ExecutionStatus.BLOCKED, "禁用品种不参与交易", "DOCS.FORBIDDEN_SYMBOL"
        if partial:
            return ExecutionStatus.PARTIAL, "离线演示：执行窗口结束，剩余委托已撤销", "DOCS.PARTIAL_FILL"
        return (ExecutionStatus.SUCCEEDED if delta else ExecutionStatus.NOOP), None, None

    @staticmethod
    def _overall_status(results):
        """汇总教学品种状态。"""
        statuses = {result.status for result in results.values()}
        for status in (ExecutionStatus.PARTIAL, ExecutionStatus.BLOCKED):
            if status in statuses:
                return status
        if statuses == {ExecutionStatus.NOOP}:
            return ExecutionStatus.NOOP
        return ExecutionStatus.SUCCEEDED

    def execute(self, standard_input, **kwargs):
        """生成完整或部分成交的夹具；由真实 Axile 服务端保存和投影。"""
        state = self._state()
        scenario_path = ROOT / "scenario.json"
        scenario = json.loads(scenario_path.read_text()) if scenario_path.exists() else {}
        results = {
            symbol: self._symbol_result(symbol, weight, state, scenario, standard_input.forbidden_symbols)
            for symbol, weight in standard_input.curr_target.items()
        }
        self.state_file.write_text(json.dumps(state))
        return UnifiedStandardOutput(
            account_assets=self.get_account_assets(),
            inputs=standard_input,
            symbol_results=results,
            status=self._overall_status(results),
            execution_time=0.8,
            channel_type=TradeChannel.TQ,
            memory={"documentation_fixture": True},
        )

    def _cleanup(self):
        pass

    def _get_account_mark(self):
        return "离线文档演示"

    def _get_default_trade_rules_for_empty(self, symbols):
        return {}

    def register_order_callback(self, callback):
        pass

    def register_price_callback(self, callback):
        pass

    def unregister_order_callback(self, callback):
        pass

    def unregister_price_callback(self, callback):
        pass

    def initialize_websocket(self, symbols=None):
        pass

    def is_monitoring(self):
        return True

    def _place_order_impl(self, *args, **kwargs):
        raise RuntimeError("文档环境禁止直接下单；演示结果仅由 execute 夹具生成")

    def _get_pending_orders_impl(self, symbol=None):
        return []

    def _query_trades_impl(self, symbol, order_id):
        return []

    def _cancel_order_impl(self, symbol, order_id):
        raise RuntimeError("文档环境不连接交易渠道")


def create_executor(config):
    return DocumentationExecutor(TradeChannel.TQ, config)


def install():
    """只替换天勤执行器工厂，保留公共 UI、描述、API 和数据库处理逻辑。"""
    import axile.channels.builtins as builtins

    builtins._create_tq_executor = create_executor
