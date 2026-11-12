"""ExecutionEngine 生命周期 ownership 的测试。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.algorithms.core.base import AlgorithmInput
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.execution_engine import ExecutionEngine, _PreparedSymbolAlgorithm
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus, TargetSizingDecision
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.models.value_conversion import PriceValueConversion
from axile.executor.termination import ExecutionTerminated


class _Logger:
    def info(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def warning(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def error(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)


class _LifecycleRecorderExecutor(AbstractExecutor):
    def __init__(self) -> None:
        self.logger = _Logger()
        self.calls: list[str] = []
        self.websocket_init_calls: list[list[str]] = []
        super().__init__(
            TradeChannel.CTP,
            CTPAccountConfig.model_validate(
                {
                    "broker_id": "b",
                    "investor_id": "i",
                    "password": "p",
                    "td_front": "tcp://td:1",
                    "md_front": "tcp://md:2",
                    "app_id": "app",
                    "auth_code": "auth",
                }
            ),
        )

    def _initialize_connection(self, account_config: CTPAccountConfig) -> None:
        self.account_config = account_config

    def _verify_connection(self) -> bool:
        self.calls.append("verify_connection")
        return True

    def _ensure_connection(self) -> None:
        self.calls.append("ensure_connection")
        super()._ensure_connection()

    def _reset_execution_state(self, standard_input: UnifiedStandardInput) -> None:
        self.calls.append("reset_execution_state")
        super()._reset_execution_state(standard_input)

    def _check_trading_time(self) -> bool:
        self.calls.append("check_trading_time")
        return True

    def _validate_input(self, standard_input: UnifiedStandardInput) -> None:
        self.calls.append("validate_input")
        super()._validate_input(standard_input)

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        return {
            symbol: UnifiedPriceData(
                symbol=symbol,
                last_price=100.0,
                asks=[(101.0, 1.0)],
                bids=[(99.0, 1.0)],
                volume=1.0,
                turnover=1.0,
                timestamp=1,
                update_time="2026-03-22T10:00:00",
            )
            for symbol in symbols
        }

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
            order_id=f"order-{symbol}",
            symbol=symbol,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status="SUBMITTED",
        )

    def _get_pending_orders_impl(self, _symbol: str | None = None) -> list[UnifiedOrder]:
        return []

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        _ = (symbol, order_id)
        raise NotImplementedError

    def _cleanup(self) -> None:
        self.calls.append("cleanup")

    def _get_account_mark(self) -> str:
        return "lifecycle-test"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, Any]:
        return {symbol: {} for symbol in symbols}

    def register_order_callback(self, callback: object) -> None:
        _ = callback

    def register_price_callback(self, callback: object) -> None:
        _ = callback

    def unregister_order_callback(self, callback: object) -> None:
        _ = callback

    def unregister_price_callback(self, callback: object) -> None:
        _ = callback

    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        self.websocket_init_calls.append(list(symbols or []))

    def is_monitoring(self) -> bool:
        return False

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        _ = (symbol, order_id)
        return True


def _standard_input() -> UnifiedStandardInput:
    return UnifiedStandardInput.from_dict(
        {
            "channel_type": TradeChannel.CTP.value,
            "account_config": {
                "broker_id": "b",
                "investor_id": "i",
                "password": "p",
                "td_front": "tcp://td:1",
                "md_front": "tcp://md:2",
                "app_id": "app",
                "auth_code": "auth",
            },
            "curr_target": {"rb2610": 0.1},
            "algorithm": {"method": "DEFAULT-ALGO", "params": {"timeout": 3}},
        }
    )


def _output() -> UnifiedStandardOutput:
    return UnifiedStandardOutput(
        account_assets=UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        ),
        memory={},
        inputs=_standard_input(),
        symbol_results={},
        status="NOOP",
        channel_type=TradeChannel.CTP,
        success=True,
    )


def test_base_executor_execute_owns_execution_lifecycle(monkeypatch) -> None:
    """AbstractExecutor.execute 应先完成 execution 生命周期，再进入编排器。"""
    executor = _LifecycleRecorderExecutor()

    class _FakeEngine:
        def run(self, standard_input: UnifiedStandardInput) -> UnifiedStandardOutput:
            assert standard_input.curr_target == {"rb2610": 0.1}
            executor.calls.append("engine_run")
            return _output()

    monkeypatch.setattr(executor, "_execution_engine", lambda: _FakeEngine())

    result = executor.execute(_standard_input())

    assert result.success is True
    assert executor.calls == [
        "ensure_connection",
        "verify_connection",
        "reset_execution_state",
        "check_trading_time",
        "validate_input",
        "engine_run",
        "cleanup",
    ]


def test_execution_engine_run_only_orchestrates_symbol_work(monkeypatch) -> None:
    """ExecutionEngine.run 不应再接管 execution 生命周期。"""
    executor = _LifecycleRecorderExecutor()
    engine = ExecutionEngine(executor)

    def fake_run_symbol_algorithms(standard_input: UnifiedStandardInput) -> list[object]:
        assert standard_input.curr_target == {"rb2610": 0.1}
        executor.calls.append("engine_run_symbol_algorithms")
        return []

    def fake_create_output(standard_input: UnifiedStandardInput, results: list[object]) -> UnifiedStandardOutput:
        assert standard_input.curr_target == {"rb2610": 0.1}
        assert results == []
        executor.calls.append("engine_build_output")
        return _output()

    monkeypatch.setattr(engine, "_run_symbol_algorithms", fake_run_symbol_algorithms)
    monkeypatch.setattr(engine, "_create_standard_output_from_results", fake_create_output)

    result = engine.run(_standard_input())  # type: ignore[attr-defined]

    assert result.success is True
    assert executor.calls == ["engine_run_symbol_algorithms", "engine_build_output"]


def test_execution_engine_prewarms_runtime_before_serial_dispatch(monkeypatch) -> None:
    """串行调度前也应按 stage 一次性预热 runtime。"""
    executor = _LifecycleRecorderExecutor()
    engine = ExecutionEngine(executor)
    tasks = [
        _PreparedSymbolAlgorithm(
            symbol="rb2610",
            algorithm_name="TEST",
            algorithm_input=AlgorithmInput(symbol="rb2610", target_volume=1.0, trade_rule={}),
        ),
        _PreparedSymbolAlgorithm(
            symbol="ag2612",
            algorithm_name="TEST",
            algorithm_input=AlgorithmInput(symbol="ag2612", target_volume=1.0, trade_rule={}),
        ),
    ]
    serial_calls: list[list[str]] = []

    def fake_run_symbol_algorithms_serially(
        prepared_tasks: list[_PreparedSymbolAlgorithm],
    ) -> list[object]:
        serial_calls.append([task.symbol for task in prepared_tasks])
        return []

    monkeypatch.setattr(executor, "_supports_parallel_symbol_dispatch", lambda: False)
    monkeypatch.setattr(engine, "_run_symbol_algorithms_serially", fake_run_symbol_algorithms_serially)

    assert engine._run_prepared_symbol_algorithms(tasks) == []
    assert executor.websocket_init_calls == [["rb2610", "ag2612"]]
    assert serial_calls == [["rb2610", "ag2612"]]


def _prepared_task(symbol: str = "rb2610") -> _PreparedSymbolAlgorithm:
    return _PreparedSymbolAlgorithm(
        symbol=symbol,
        algorithm_name="TEST",
        algorithm_input=AlgorithmInput(symbol=symbol, target_volume=1.0, trade_rule={}),
    )


def test_symbol_dispatch_freezes_conversion_on_each_phase_order_and_trade() -> None:
    engine = ExecutionEngine(_LifecycleRecorderExecutor())
    phase_results = []
    for rate in (1.01, 1.03):
        conversion = PriceValueConversion(price_currency="HKD", value_currency="CNY", rate=rate)
        task = replace(
            _prepared_task("00700.HK"), sizing=TargetSizingDecision(symbol="00700.HK", value_conversion=conversion)
        )
        order = UnifiedOrder(
            order_id=str(rate),
            symbol="00700.HK",
            direction=OrderDirection.SELL,
            order_type=OrderType.LIMIT,
            volume=1,
            price=2,
            status="已成交",
            filled_volume=1,
            avg_price=2,
        )
        trade = TradeRecord.create(trade_id=str(rate), symbol="00700.HK", trade_volume=1, trade_price=2)
        result = AlgorithmResult(symbol="00700.HK", algorithm="TEST", orders=[order], trades=[trade])
        phase_results.append(engine._run_symbol_algorithm_with_error_capture(task, runner=lambda: result))
    merged = engine._merge_symbol_algorithm_results(phase_results)[0]
    assert [order.value_conversion.rate for order in merged.orders] == [1.01, 1.03]
    assert [trade.value_conversion.rate for trade in merged.trades] == [1.01, 1.03]
    assert [order.avg_price for order in merged.orders] == [2, 2]
    assert [trade.trade_value for trade in merged.trades] == [2, 2]


def test_symbol_error_capture_propagates_termination_instead_of_marking_failed() -> None:
    """协作式终止必须穿透 symbol 级错误捕获，否则会被误报为「执行失败」。"""
    executor = _LifecycleRecorderExecutor()
    engine = ExecutionEngine(executor)

    def terminating_runner() -> object:
        raise ExecutionTerminated(reason="manual stop", mode="cancel_pending")

    with pytest.raises(ExecutionTerminated) as exc_info:
        engine._run_symbol_algorithm_with_error_capture(_prepared_task(), runner=terminating_runner)

    assert exc_info.value.reason == "manual stop"
    assert exc_info.value.mode == "cancel_pending"


def test_symbol_error_capture_propagates_session_recovery() -> None:
    executor = _LifecycleRecorderExecutor()
    engine = ExecutionEngine(executor)

    def recovery_runner() -> object:
        raise CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        engine._run_symbol_algorithm_with_error_capture(_prepared_task(), runner=recovery_runner)


def test_symbol_error_capture_still_converts_generic_error_to_failed_result() -> None:
    """普通异常仍应归一化为该 symbol 的失败结果，不改变既有行为。"""
    executor = _LifecycleRecorderExecutor()
    engine = ExecutionEngine(executor)

    def failing_runner() -> object:
        raise RuntimeError("boom")

    result = engine._run_symbol_algorithm_with_error_capture(_prepared_task("ag2612"), runner=failing_runner)

    assert result.symbol == "ag2612"
    assert result.status == ExecutionStatus.FAILED
    assert result.error == "执行失败，具体原因未确认"


@pytest.mark.parametrize("status", [ExecutionStatus.BLOCKED, ExecutionStatus.FAILED])
def test_final_account_query_failure_does_not_rewrite_non_position_conclusions(monkeypatch, status):
    from unittest.mock import Mock

    from axile.executor.models.execution_result import AlgorithmResult

    executor = _LifecycleRecorderExecutor()
    executor.logger = Mock()
    monkeypatch.setattr(executor, "get_account_assets", Mock(side_effect=RuntimeError("native query error")))
    result = AlgorithmResult(symbol="A", algorithm="test", status=status, error="当前不在交易时段")
    output = ExecutionEngine(executor)._create_standard_output_from_results(_standard_input(), [result])
    assert output.status == status
    assert output.error == "当前不在交易时段"
    assert output.symbol_results == {"A": result}
    assert output.account_assets.source == "unavailable"


@pytest.mark.parametrize(
    "status, expected",
    [(ExecutionStatus.SUCCEEDED, ExecutionStatus.PARTIAL), (ExecutionStatus.NOOP, ExecutionStatus.FAILED)],
)
def test_final_account_query_failure_keeps_symbol_results(monkeypatch, status, expected):
    from unittest.mock import Mock

    from axile.executor.models.execution_result import AlgorithmResult

    executor = _LifecycleRecorderExecutor()
    executor.logger = Mock()
    monkeypatch.setattr(executor, "get_account_assets", Mock(side_effect=RuntimeError("native query error")))
    result = AlgorithmResult(symbol="A", algorithm="test", status=status)
    output = ExecutionEngine(executor)._create_standard_output_from_results(_standard_input(), [result])
    assert output.status == expected
    assert output.error == "最终持仓查询失败，具体原因未确认"
    assert output.symbol_results == {"A": result}


@pytest.mark.parametrize("source", ["unavailable", "error", "assumed", "real", "simulation", "custom-channel"])
@pytest.mark.parametrize(
    "status, expected",
    [(ExecutionStatus.SUCCEEDED, ExecutionStatus.PARTIAL), (ExecutionStatus.NOOP, ExecutionStatus.FAILED)],
)
def test_final_account_observation_source_controls_output(monkeypatch, source, status, expected):
    """降级收尾快照不能报成功，同时保留快照与品种结果。"""
    from axile.executor.models.execution_result import AlgorithmResult

    executor = _LifecycleRecorderExecutor()
    assets = UnifiedAccountAssets.unavailable().model_copy(update={"source": source})
    monkeypatch.setattr(executor, "get_account_assets", lambda: assets)
    result = AlgorithmResult(symbol="A", algorithm="test", status=status)

    output = ExecutionEngine(executor)._create_standard_output_from_results(_standard_input(), [result])

    assert output.status == (status if source in {"real", "simulation", "custom-channel"} else expected)
    assert output.success is (source in {"real", "simulation", "custom-channel"})
    assert output.error == (None if source in {"real", "simulation", "custom-channel"} else "最终持仓尚未确认")
    assert output.account_assets == assets
    assert output.symbol_results == {"A": result}


@pytest.mark.parametrize("capture_fails", [False, True])
def test_server_notification_captures_without_sending_or_changing_trade(monkeypatch, capture_fails):
    """服务端托管通知只保存快照；快照异常不能改写交易结果。"""
    from axile.executor.abstract_executor import execution_lifecycle

    executor = _LifecycleRecorderExecutor()
    output = _output()

    class Engine:
        def run(self, _input):
            return output

    sent = []

    def capture(*_args):
        if capture_fails:
            raise RuntimeError("snapshot unavailable")
        return {"account": {"mark": "original-channel-mark"}}

    monkeypatch.setattr(executor, "_execution_engine", lambda: Engine())
    monkeypatch.setattr(execution_lifecycle, "build_execution_notification_context", capture)
    monkeypatch.setattr(execution_lifecycle, "enqueue_execute_results_to_feishu", lambda *_args: sent.append(True))
    inputs = _standard_input()
    inputs.extra["server_notification"] = True
    inputs.execution_notification_code = "def notify(context): pass"
    result = executor.execute(inputs)
    assert result.success
    assert sent == []
    if not capture_fails:
        assert result.extra["server_notification_context"]["account"]["mark"] == "original-channel-mark"


def test_progress_enters_settling_only_after_both_dispatch_phases(monkeypatch):
    from types import SimpleNamespace

    executor = _LifecycleRecorderExecutor()
    seen = []
    executor.set_progress_observer(seen.append)
    engine = ExecutionEngine(executor)
    assets = executor.get_account_assets()
    planning = SimpleNamespace(plans=[], planning_failures=[], account_assets=assets)
    dispatch = SimpleNamespace(phase_one_tasks=[object()], phase_two_plans=[object()], direct_results=[])
    monkeypatch.setattr(engine, "_build_symbol_algorithm_plans", lambda value: planning)
    monkeypatch.setattr(engine, "_build_dispatch_phases", lambda value: dispatch)
    monkeypatch.setattr(engine, "_prepare_account_for_symbol_dispatch", lambda value: None)

    def dispatch_phase(*args, **kwargs):
        assert seen == ["planning", "executing"]
        # 两阶段之间也会读取持仓，但不能把整轮进度提前改成对账。
        engine._settle_result_positions([])
        assert seen[-1] == "executing"
        return []

    monkeypatch.setattr(engine, "_run_phase_one_dispatch", dispatch_phase)
    monkeypatch.setattr(engine, "_run_phase_two_dispatch", dispatch_phase)
    monkeypatch.setattr(engine, "_read_result_assets", lambda: (assets, None))
    engine._run_symbol_algorithms(_standard_input())
    assert seen == ["planning", "executing", "settling"]
