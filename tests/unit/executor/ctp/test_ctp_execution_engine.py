"""CTP 渠道品种时段执行编排测试。"""

from __future__ import annotations

from typing import Any

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionReasonFamily
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.executor.ctp.ctp_execute import CtpExecutionEngine
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus, TargetSizingStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderStatus, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class _CtpSessionExecutor(AbstractExecutor):
    def __init__(self, decisions: dict[str, str | None]) -> None:
        self.decisions = decisions
        self.market_data_requests: list[list[str]] = []
        self.websocket_requests: list[list[str]] = []
        self.cancel_all_orders_calls = 0
        self.pending_orders: list[UnifiedOrder] = []
        self.cancelled_orders: list[tuple[str, str]] = []
        super().__init__(
            TradeChannel.CTP,
            CTPAccountConfig(
                broker_id="b",
                investor_id="i",
                password="p",
                td_front="tcp://td:1",
                md_front="tcp://md:2",
                app_id="app",
                auth_code="auth",
            ),
        )

    def _initialize_connection(self, account_config: CTPAccountConfig) -> None:
        self.account_config = account_config

    def _verify_connection(self) -> bool:
        return True

    def _check_trading_time(self) -> bool:
        return True

    def _execution_engine(self) -> CtpExecutionEngine:
        return CtpExecutionEngine(self, self.require_execution_runtime())

    def _get_ctp_session_block_reason(self, symbol: str) -> str | None:
        return self.decisions[symbol]

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(available_cash=1000, total_asset=1000, market_value=0, positions=[])

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        blocked = [symbol for symbol in symbols if self.decisions.get(symbol)]
        if blocked:
            raise TimeoutError(f"CLOSED 品种进入阻塞行情: {blocked}")
        self.market_data_requests.append(symbols)
        return {
            symbol: UnifiedPriceData(
                symbol=symbol,
                last_price=100,
                bid_price=99,
                ask_price=101,
                bid_volume=1,
                ask_volume=1,
                volume=1,
                turnover=100,
                timestamp=clock_now_ms(),
                update_time="2026-08-25T21:29:00",
            )
            for symbol in symbols
        }

    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        self.websocket_requests.append(list(symbols or []))

    def cancel_all_orders(self) -> None:
        self.cancel_all_orders_calls += 1
        for order in self.pending_orders:
            self.cancel_order(order.symbol, order.order_id)

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = (direction, order_type, volume, price, kwargs)
        return UnifiedOrder(
            order_id=symbol,
            symbol=symbol,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1,
            price=100,
            status="SUBMITTED",
        )

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        return [order for order in self.pending_orders if symbol is None or order.symbol == symbol]

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        _ = (symbol, order_id)
        return []

    def _cleanup(self) -> None:
        return None

    def _get_account_mark(self) -> str:
        return "ctp-session"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        return {symbol: {} for symbol in symbols}

    def register_order_callback(self, callback: object) -> None:
        _ = callback

    def register_price_callback(self, callback: object) -> None:
        _ = callback

    def unregister_order_callback(self, callback: object) -> None:
        _ = callback

    def unregister_price_callback(self, callback: object) -> None:
        _ = callback

    def is_monitoring(self) -> bool:
        return False

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        self.cancelled_orders.append((symbol, order_id))
        return True


def _input() -> UnifiedStandardInput:
    return UnifiedStandardInput(
        channel_type=TradeChannel.CTP,
        account_config=CTPAccountConfig(
            broker_id="b",
            investor_id="i",
            password="p",
            td_front="tcp://td:1",
            md_front="tcp://md:2",
            app_id="app",
            auth_code="auth",
        ),
        curr_target={"IF2609": 0.1, "ag2612": 0.2},
        algorithm={"method": "SINGLE-MAKER"},
    )


def test_ctp_engine_only_queries_and_dispatches_session_allowed_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    from axile.executor import execution_engine as engine_module

    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": None})
    dispatched_symbols: list[str] = []
    monkeypatch.setattr(
        engine_module,
        "resolve_algorithm",
        lambda _name, _session: (
            lambda _session, algorithm_input: (
                dispatched_symbols.append(algorithm_input.symbol)
                or AlgorithmResult(
                    symbol=algorithm_input.symbol,
                    algorithm="SINGLE-MAKER",
                    target_volume=algorithm_input.target_volume,
                )
            )
        ),
    )

    output = executor.execute(_input(), cleanup=False)

    assert output.status == ExecutionStatus.SUCCEEDED
    assert output.symbol_results["IF2609"].status == ExecutionStatus.NOOP
    assert output.symbol_results["IF2609"].error is None
    assert output.symbol_results["IF2609"].outcome_reason == "非交易时段"
    assert output.symbol_results["IF2609"].reason_code == "COMMON.SESSION.CLOSED"
    assert output.symbol_results["IF2609"].reason_family == ExecutionReasonFamily.MARKET_RULE
    assert output.symbol_results["IF2609"].sizing is not None
    assert output.symbol_results["IF2609"].sizing.status is TargetSizingStatus.UNAVAILABLE
    assert executor.market_data_requests == [["ag2612"]]
    assert all("IF2609" not in request for request in executor.websocket_requests)
    assert executor.cancel_all_orders_calls == 1
    assert dispatched_symbols == ["ag2612"]


@pytest.mark.parametrize("open_status", [ExecutionStatus.NOOP, ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED])
def test_mixed_session_output_preserves_open_results_and_closed_targets(monkeypatch, open_status):
    from axile.executor import execution_engine as engine_module
    from axile.executor.session_closed import COMMON_SESSION_CLOSED, session_closed_scope

    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": None})
    monkeypatch.setattr(
        engine_module,
        "resolve_algorithm",
        lambda _name, _session: (
            lambda _session, value: AlgorithmResult(
                symbol=value.symbol,
                algorithm="SINGLE-MAKER",
                status=open_status,
                target_volume=value.target_volume,
                final_volume=0,
                error="下单失败" if open_status == ExecutionStatus.FAILED else None,
            )
        ),
    )
    output = executor.execute(_input(), cleanup=False)
    assert output.symbol_results["ag2612"].status == open_status
    assert output.symbol_results["IF2609"].final_volume is None
    assert executor.market_data_requests == [["ag2612"]]
    if open_status == ExecutionStatus.FAILED:
        assert output.status == ExecutionStatus.FAILED
        assert output.reason_code is None
        assert session_closed_scope(output.model_dump(mode="json")) is None
    else:
        assert output.status == open_status
        assert output.reason_code == COMMON_SESSION_CLOSED
        assert output.error is None
        assert output.success
        assert session_closed_scope(output.model_dump(mode="json")) == "partial"


def test_final_snapshot_failure_is_not_labeled_partial_session(monkeypatch):
    from axile.executor import execution_engine as engine_module
    from axile.executor.session_closed import session_closed_scope
    from axile.server.execution.legacy_compat import normalize_legacy_result

    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": None})
    monkeypatch.setattr(
        engine_module,
        "resolve_algorithm",
        lambda _name, _session: (
            lambda _session, value: AlgorithmResult(
                symbol=value.symbol,
                algorithm="SINGLE-MAKER",
                target_volume=value.target_volume,
            )
        ),
    )
    monkeypatch.setattr(
        CtpExecutionEngine,
        "_read_result_assets",
        lambda _self: (UnifiedAccountAssets.unavailable(), "最终持仓查询失败"),
    )
    output = executor.execute(_input(), cleanup=False)
    assert output.status == ExecutionStatus.PARTIAL
    assert output.error == "最终持仓查询失败"
    assert output.reason_code is None
    assert session_closed_scope(normalize_legacy_result(output.model_dump(mode="json"))) is None


def test_ctp_engine_sizes_closed_symbol_from_cached_quote_without_blocking_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from axile.executor import execution_engine as engine_module

    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": None})
    executor._quotes = {
        "IF2609": UnifiedPriceData(
            symbol="IF2609",
            last_price=100,
            bid_price=99,
            ask_price=101,
            bid_volume=1,
            ask_volume=1,
            volume=1,
            turnover=100,
            timestamp=clock_now_ms(),
            update_time="2026-08-25T11:29:00",
        )
    }
    dispatched_symbols: list[str] = []
    monkeypatch.setattr(
        engine_module,
        "resolve_algorithm",
        lambda _name, _session: (
            lambda _session, algorithm_input: (
                dispatched_symbols.append(algorithm_input.symbol)
                or AlgorithmResult(
                    symbol=algorithm_input.symbol,
                    algorithm="SINGLE-MAKER",
                    target_volume=algorithm_input.target_volume,
                )
            )
        ),
    )

    output = executor.execute(_input(), cleanup=False)

    blocked = output.symbol_results["IF2609"]
    assert blocked.status == ExecutionStatus.NOOP
    assert blocked.sizing is not None
    assert blocked.sizing.target_quantity == pytest.approx(1.0)
    assert blocked.target_volume == pytest.approx(1.0)
    assert executor.market_data_requests == [["ag2612"]]
    assert dispatched_symbols == ["ag2612"]


def test_next_execution_cancels_deadline_residual_orders_from_blocked_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    """下一轮即使阻断旧 symbol，仍要账户级清理 deadline 残单。"""
    from axile.executor import execution_engine as engine_module

    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": None})
    executor.pending_orders = [
        UnifiedOrder(
            order_id="deadline-if-order",
            symbol="IF2609",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1,
            price=100,
            status=OrderStatus.SUBMITTED,
        )
    ]
    monkeypatch.setattr(
        engine_module,
        "resolve_algorithm",
        lambda _name, _session: (
            lambda _session, algorithm_input: AlgorithmResult(
                symbol=algorithm_input.symbol,
                algorithm="SINGLE-MAKER",
                target_volume=algorithm_input.target_volume,
            )
        ),
    )

    executor.execute(_input(), cleanup=False)

    assert executor.cancel_all_orders_calls == 1
    assert executor.cancelled_orders == [("IF2609", "deadline-if-order")]


def test_ctp_engine_blocks_all_session_rejections_without_execution_io() -> None:
    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": "CTP.SESSION.NO_SESSION_TABLE"})

    output = executor.execute(_input(), cleanup=False)

    assert output.status == ExecutionStatus.BLOCKED
    assert output.error is not None
    assert output.symbol_results["ag2612"].status == ExecutionStatus.BLOCKED
    assert output.error == output.symbol_results["ag2612"].error
    assert output.reason_code == "CTP.SESSION.NO_SESSION_TABLE"
    assert set(output.symbol_results) == {"IF2609", "ag2612"}
    assert executor.market_data_requests == []
    assert executor.websocket_requests == []
    assert executor.cancel_all_orders_calls == 0
    for symbol in ("IF2609", "ag2612"):
        result = output.symbol_results[symbol]
        assert result.status == (ExecutionStatus.NOOP if symbol == "IF2609" else ExecutionStatus.BLOCKED)
        assert result.sizing is not None
        assert result.sizing.status is TargetSizingStatus.UNAVAILABLE


class _AuditRuntime:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit_audit_event(self, **kwargs: Any) -> bool:
        self.events.append(kwargs)
        return True


def test_ctp_session_decision_emits_market_rule_audit() -> None:
    runtime = _AuditRuntime()
    engine = CtpExecutionEngine(object(), runtime=runtime)  # type: ignore[arg-type]

    engine._emit_symbol_decision_events(
        _input(),
        [
            AlgorithmResult(
                symbol="ag2609C5000",
                algorithm="SINGLE-MAKER",
                status=ExecutionStatus.BLOCKED,
                error="CTP.SESSION.NO_SESSION_TABLE",
                reason_code="CTP.SESSION.NO_SESSION_TABLE",
                reason_family=ExecutionReasonFamily.MARKET_RULE,
            )
        ],
    )

    event = runtime.events[0]
    assert event["reason_family"] == ExecutionReasonFamily.MARKET_RULE
    assert event["reason_code"] == "CTP.SESSION.NO_SESSION_TABLE"


def test_startup_restricted_cancellation_blocks_dispatch_and_keeps_live_order(monkeypatch):
    from axile.executor import execution_engine
    from axile.executor.order_cancellation import CancelNotAllowedError

    executor = _CtpSessionExecutor({"IF2609": None, "ag2612": None})
    order = UnifiedOrder(
        order_id="retained",
        symbol="IF2609",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=100,
        status=OrderStatus.SUBMITTED,
    )
    executor.account_orders.apply(order)

    def restricted():
        raise CancelNotAllowedError("当前不可撤单")

    monkeypatch.setattr(executor, "cancel_all_orders", restricted)
    monkeypatch.setattr(execution_engine, "resolve_algorithm", lambda *_: pytest.fail("不应调度算法"))
    output = executor.execute(_input(), cleanup=False)
    assert output.status == ExecutionStatus.BLOCKED
    assert output.reason_code == "COMMON.CANCEL.RESTRICTED"
    assert output.symbol_results["IF2609"].orders == [order]


def test_global_closed_session_keeps_retained_order_instead_of_claiming_success(monkeypatch):
    executor = _CtpSessionExecutor({"IF2609": "CTP.SESSION.CLOSED", "ag2612": "CTP.SESSION.CLOSED"})
    order = UnifiedOrder(
        order_id="retained",
        symbol="IF2609",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=100,
        status=OrderStatus.SUBMITTED,
    )
    executor.account_orders.apply(order)
    monkeypatch.setattr(executor, "_check_trading_time", lambda: False)
    output = executor.execute(_input(), cleanup=False)
    assert output.status == ExecutionStatus.BLOCKED
    assert not output.success
    assert output.orders == [order]
    assert executor.cancel_all_orders_calls == 0
