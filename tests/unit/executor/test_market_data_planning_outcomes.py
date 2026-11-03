"""行情不可用时两阶段规划结果及审计归因。"""

from typing import Any

import pytest

from axile.domain.execution import ExecutionReasonFamily
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.models.execution_result import AlgorithmResult, ExecutionOutcome, ExecutionStatus
from axile.executor.models.unified_account_assets import PositionDirection
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from tests.unit.executor.test_base_executor_symbol_dispatch import _assets, _price, _TestExecutor

SYMBOL = "rb2610"
REASON = "COMMON.MARKET_DATA.UNAVAILABLE"


class _AuditRuntime:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit_audit_event(self, **kwargs: Any) -> bool:
        self.events.append(kwargs)
        return True


def _input(executor: _TestExecutor, targets: dict[str, float]) -> UnifiedStandardInput:
    return UnifiedStandardInput(channel_type="ctp", account_config=executor.account_config, curr_target=targets)


def _stale_price(symbol: str):
    price = _price(symbol)
    price.timestamp -= 86400 * 1000
    return price


def _assert_market_event(event: dict[str, Any], status: ExecutionStatus) -> None:
    assert event["reason_code"] == REASON
    assert event["reason_family"] == ExecutionReasonFamily.MARKET_RULE
    assert event["details"]["decision"]["status"] == status.value


def test_initial_planning_unavailable_emits_market_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _TestExecutor()
    runtime = _AuditRuntime()
    engine = ExecutionEngine(executor, runtime=runtime)  # type: ignore[arg-type]
    monkeypatch.setattr(
        executor, "get_market_data", lambda symbols: {symbol: _stale_price(symbol) for symbol in symbols}
    )

    result = engine._run_symbol_algorithms(_input(executor, {SYMBOL: 0.1}))[0]

    assert result.status == ExecutionStatus.BLOCKED
    assert result.outcome == ExecutionOutcome.BLOCKED
    assert result.reason_code == REASON
    assert result.sizing is not None and result.sizing.target_quantity is None
    _assert_market_event(runtime.events[0], ExecutionStatus.BLOCKED)


@pytest.mark.parametrize("reverse", [False, True])
def test_replanning_unavailable_preserves_progress_and_audit(monkeypatch: pytest.MonkeyPatch, reverse: bool) -> None:
    executor = _TestExecutor(
        account_assets_snapshots=[
            _assets(positions=[(SYMBOL, 5, PositionDirection.LONG)]),
            _assets(),
        ]
    )
    runtime = _AuditRuntime()
    engine = ExecutionEngine(executor, runtime=runtime)  # type: ignore[arg-type]
    target = SYMBOL if reverse else "ag2612"
    requests: list[list[str]] = []

    def quotes(symbols: list[str]):
        requests.append(symbols)
        return {symbol: (_price(symbol) if len(requests) == 1 else _stale_price(symbol)) for symbol in symbols}

    order = UnifiedOrder(
        order_id="close",
        symbol=SYMBOL,
        direction=OrderDirection.SELL,
        order_type=OrderType.LIMIT,
        volume=5,
        price=100,
        status="FILLED",
    )
    trade = TradeRecord.create(trade_id="fill", symbol=SYMBOL, order_id="close", trade_volume=5, trade_price=100)
    calls: list[list[str]] = []

    def dispatch(tasks):
        calls.append([task.symbol for task in tasks])
        return [
            AlgorithmResult(
                symbol=task.symbol,
                algorithm=task.algorithm_name,
                orders=[order],
                trades=[trade],
                final_volume=0,
                status=ExecutionStatus.SUCCEEDED,
            )
            for task in tasks
        ]

    monkeypatch.setattr(executor, "get_market_data", quotes)
    monkeypatch.setattr(engine, "_run_prepared_symbol_algorithms", dispatch)
    results = engine._run_symbol_algorithms(_input(executor, {target: -0.3 if reverse else 0.3}))
    result = next(item for item in results if item.symbol == target)
    event = next(item for item in runtime.events if item["symbol"] == target)

    assert len(requests) == 2
    assert calls == [[SYMBOL], []]
    assert result.reason_code == REASON
    assert result.sizing is not None and result.sizing.target_quantity is None
    assert result.error
    if reverse:
        assert result.status == ExecutionStatus.PARTIAL
        assert result.outcome == ExecutionOutcome.NOT_REACHED
        assert result.orders == [order] and result.trades == [trade]
        assert result.final_volume == 0
    else:
        assert result.status == ExecutionStatus.BLOCKED
        assert result.outcome == ExecutionOutcome.BLOCKED
        assert result.orders == result.trades == []
    _assert_market_event(event, result.status)
