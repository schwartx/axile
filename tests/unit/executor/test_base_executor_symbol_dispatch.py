"""AbstractExecutor 按品种算法调度的测试。"""

from __future__ import annotations

from threading import Barrier, Event, Lock
from time import time
from typing import Any, cast

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.algorithms.core.base import AlgorithmInput, AlgorithmResult
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import ExecutionStatus
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController


class _Logger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("debug", message))

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("info", message))

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("warning", message))

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("error", message))

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("exception", message))


class _SharedState:
    def __init__(self) -> None:
        self.executor_ids: list[int] = []


class _TestExecutor(AbstractExecutor):
    max_parallel_symbol_workers = 12

    def __init__(
        self,
        shared_state: _SharedState | None = None,
        account_assets_snapshots: list[UnifiedAccountAssets] | None = None,
    ) -> None:
        self.logger = _Logger()
        self.cleaned_up = False
        self.checkpoint_calls = 0
        self.account_assets_calls = 0
        self.market_data_requests: list[list[str]] = []
        self._account_assets_snapshots = account_assets_snapshots or []
        self.shared_state = shared_state or _SharedState()
        self.shared_state.executor_ids.append(id(self))
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
        return True

    def _check_trading_time(self) -> bool:
        return True

    def get_account_assets(self) -> UnifiedAccountAssets:
        self.account_assets_calls += 1
        if self._account_assets_snapshots:
            index = min(self.account_assets_calls - 1, len(self._account_assets_snapshots) - 1)
            return self._account_assets_snapshots[index].model_copy(deep=True)
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        self.market_data_requests.append(list(symbols))
        return {symbol: _price(symbol) for symbol in symbols}

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = kwargs
        return UnifiedOrder(
            order_id=f"order-{symbol}",
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status="SUBMITTED",
        )

    def _get_pending_orders_impl(self, _symbol: str | None = None) -> list[UnifiedOrder]:
        return []

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        _ = (symbol, order_id)
        raise NotImplementedError

    def _cleanup(self) -> None:
        self.cleaned_up = True

    def _get_account_mark(self) -> str:
        return "test-account"

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
        _ = symbols

    def is_monitoring(self) -> bool:
        return False

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        _ = (symbol, order_id)
        return True

    def handle_termination_checkpoint(self, symbol: str | None = None) -> None:
        self.checkpoint_calls += 1
        super().handle_termination_checkpoint(symbol)

    def _supports_parallel_symbol_dispatch(self) -> bool:
        return True

    def _max_parallel_symbol_workers(self) -> int:
        return self.max_parallel_symbol_workers


class _ScopedOrderViewsExecutor(_TestExecutor):
    def __init__(self, pending_orders: list[UnifiedOrder]) -> None:
        self._pending_orders = pending_orders
        super().__init__()

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        assert symbol is not None
        return [order.model_copy(deep=True) for order in self._pending_orders if order.symbol == symbol]


def _price(symbol: str) -> UnifiedPriceData:
    return UnifiedPriceData(
        symbol=symbol,
        last_price=100.0,
        bid_price=99.0,
        ask_price=101.0,
        bid_volume=1.0,
        ask_volume=1.0,
        volume=10.0,
        turnover=1000.0,
        timestamp=1,
        update_time="2026-03-21T00:00:00",
    )


def _assets(
    *,
    total_asset: float = 1000.0,
    positions: list[tuple[str, float, PositionDirection]] | None = None,
) -> UnifiedAccountAssets:
    """构造带持仓的账户资产快照。"""
    position_models: list[Position] = []
    total_market_value = 0.0
    for symbol, volume, direction in positions or []:
        market_value = abs(volume) * 100.0
        total_market_value += market_value
        position_models.append(
            Position(
                symbol=symbol,
                volume=abs(volume),
                available_volume=abs(volume),
                market_value=market_value,
                direction=direction,
                avg_price=100.0,
            )
        )

    return UnifiedAccountAssets(
        available_cash=max(total_asset - total_market_value, 0.0),
        total_asset=total_asset,
        market_value=total_market_value,
        positions=position_models,
    )


def test_execute_continues_other_symbols_and_marks_overall_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个品种失败不应中断其他品种，但整体执行应失败。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor()

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            symbol = algorithm_input.symbol
            if name == "FAIL-ALGO":
                raise RuntimeError(f"{symbol} failed")
            return AlgorithmResult(
                orders=[
                    UnifiedOrder(
                        order_id=f"order-{symbol}",
                        symbol=symbol,
                        direction=OrderDirection.BUY,
                        order_type=OrderType.LIMIT,
                        volume=1.0,
                        price=100.0,
                        status="FILLED",
                    )
                ],
                account_assets=executor.get_account_assets(),
                target_volume=algorithm_input.target_volume or 0.0,
                first_tick=_price(symbol),
                memory={"algorithm": name},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "curr_target": {"rb2610": 0.1, "ag2612": 0.2},
                "algorithm": {"method": "SUCCESS-ALGO", "params": {"timeout": 3}},
                "symbol_algorithms": {"ag2612": {"method": "FAIL-ALGO", "params": {"timeout": 7}}},
            }
        )
    )

    assert output.success is False
    assert output.status == ExecutionStatus.PARTIAL
    assert len(output.symbol_results) == 2
    assert [order.symbol for order in output.orders] == ["rb2610"]
    assert output.error == "执行失败，具体原因未确认"
    assert output.symbol_results["ag2612"].algorithm == "FAIL-ALGO"
    assert output.symbol_results["ag2612"].status == ExecutionStatus.FAILED
    assert output.symbol_results["ag2612"].success is False
    assert output.symbol_results["ag2612"].error is not None
    assert output.symbol_results["ag2612"].error == "执行失败，具体原因未确认"


def test_execute_single_symbol_failure_is_aggregated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单品种失败也应返回聚合失败结果，而不是直接抛异常。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor()

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            symbol = algorithm_input.symbol
            raise RuntimeError(f"{symbol} failed with {name}")

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "algorithm": {"method": "FAIL-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert output.success is False
    assert output.status == ExecutionStatus.FAILED
    assert len(output.symbol_results) == 1
    assert sum(1 for result in output.symbol_results.values() if not result.success) == 1
    assert output.symbol_results["rb2610"].algorithm == "FAIL-ALGO"
    assert output.symbol_results["rb2610"].status == ExecutionStatus.FAILED
    assert output.error == "执行失败，具体原因未确认"
    assert output.symbol_results["rb2610"].error is not None
    assert output.symbol_results["rb2610"].error == "执行失败，具体原因未确认"


def test_execute_serial_dispatch_treats_explicit_failed_status_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """串行分发路径应按显式失败状态归并品种失败。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor()
    monkeypatch.setattr(_TestExecutor, "_supports_parallel_symbol_dispatch", lambda _self: False)

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            symbol = algorithm_input.symbol
            return AlgorithmResult(
                orders=[],
                account_assets=executor.get_account_assets(),
                target_volume=algorithm_input.target_volume or 0.0,
                first_tick=_price(symbol),
                status=ExecutionStatus.FAILED,
                error=f"{name} failed",
                memory={"debug": f"{name} failed"},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "algorithm": {"method": "ERROR-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert output.success is False
    assert output.status == ExecutionStatus.FAILED
    assert len(output.symbol_results) == 1
    assert sum(1 for result in output.symbol_results.values() if not result.success) == 1
    assert output.symbol_results["rb2610"].algorithm == "ERROR-ALGO"
    assert output.symbol_results["rb2610"].status == ExecutionStatus.FAILED
    assert output.error == "ERROR-ALGO failed"
    assert output.symbol_results["rb2610"].error == "ERROR-ALGO failed"


def test_execute_does_not_treat_memory_error_key_as_failure_when_status_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """算法显式成功时，不应再因为 memory.error 被判成失败。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor()
    monkeypatch.setattr(_TestExecutor, "_supports_parallel_symbol_dispatch", lambda _self: False)

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            symbol = algorithm_input.symbol
            return AlgorithmResult(
                orders=[],
                account_assets=executor.get_account_assets(),
                target_volume=algorithm_input.target_volume or 0.0,
                first_tick=_price(symbol),
                status=ExecutionStatus.SUCCEEDED,
                memory={"error": f"{name} debug only"},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "algorithm": {"method": "SUCCESS-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert output.success is True
    assert output.status == ExecutionStatus.SUCCEEDED
    assert output.memory == {}
    assert output.symbol_results["rb2610"].status == ExecutionStatus.SUCCEEDED
    assert output.symbol_results["rb2610"].success is True
    assert output.symbol_results["rb2610"].error is None
    assert output.symbol_results["rb2610"].memory["error"] == "SUCCESS-ALGO debug only"


def test_execute_runs_reduce_phase_before_open_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通用层应先执行减仓/清仓，再执行加仓/开仓。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor(
        account_assets_snapshots=[
            _assets(positions=[("rb2610", 5.0, PositionDirection.LONG)]),
            _assets(positions=[]),
        ]
    )
    calls: list[tuple[str, int | float]] = []

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            calls.append((algorithm_input.symbol, algorithm_input.target_volume))
            return AlgorithmResult(
                orders=[],
                account_assets=_assets(positions=[]),
                target_volume=algorithm_input.target_volume,
                first_tick=_price(algorithm_input.symbol),
                memory={"algorithm": name},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "curr_target": {"rb2610": 0.2, "ag2612": 0.3},
                "algorithm": {"method": "DEFAULT-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert calls == [("rb2610", 2.0), ("ag2612", 3.0)]
    assert executor.market_data_requests == [["rb2610", "ag2612"], ["ag2612"]]
    assert output.success is True


def test_execute_splits_reverse_symbol_into_two_phases_and_replans_open_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反手品种应先平到 0，再在第二阶段按刷新后的账户快照重新开仓。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor(
        account_assets_snapshots=[
            _assets(positions=[("rb2610", 5.0, PositionDirection.LONG)]),
            _assets(total_asset=2000.0, positions=[]),
        ]
    )
    calls: list[int | float] = []

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            calls.append(algorithm_input.target_volume)
            return AlgorithmResult(
                orders=[],
                account_assets=_assets(total_asset=2000.0, positions=[]),
                target_volume=algorithm_input.target_volume,
                first_tick=_price(algorithm_input.symbol),
                memory={"algorithm": name},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "curr_target": {"rb2610": -0.3},
                "algorithm": {"method": "DEFAULT-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert calls == [0, -6.0]
    assert executor.market_data_requests == [["rb2610"], ["rb2610"]]
    assert output.symbol_results["rb2610"].success is True
    assert output.target_volume["rb2610"] == -6.0


def test_execute_only_cancels_all_orders_once_across_two_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两阶段执行时，账户级撤单也只应在开始前执行一次。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor(
        account_assets_snapshots=[
            _assets(positions=[("rb2610", 5.0, PositionDirection.LONG)]),
            _assets(total_asset=2000.0, positions=[]),
        ]
    )
    call_log: list[str] = []

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_cancel_all_orders() -> None:
        call_log.append("cancel_all_orders")

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            call_log.append(f"{name}:{algorithm_input.target_volume}")
            return AlgorithmResult(
                orders=[],
                account_assets=_assets(total_asset=2000.0, positions=[]),
                target_volume=algorithm_input.target_volume,
                first_tick=_price(algorithm_input.symbol),
                memory={"algorithm": name},
            )

        return _algorithm

    monkeypatch.setattr(executor, "cancel_all_orders", fake_cancel_all_orders)
    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "curr_target": {"rb2610": -0.3},
                "algorithm": {"method": "DEFAULT-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert call_log == ["cancel_all_orders", "DEFAULT-ALGO:0", "DEFAULT-ALGO:-6.0"]
    assert output.symbol_results["rb2610"].success is True


def test_execute_skips_cancel_all_orders_when_all_symbols_are_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯 no-op 执行不应触发账户级撤单。"""
    executor = _TestExecutor()
    cancel_calls = 0

    def fake_cancel_all_orders() -> None:
        nonlocal cancel_calls
        cancel_calls += 1

    monkeypatch.setattr(executor, "cancel_all_orders", fake_cancel_all_orders)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "curr_target": {"rb2610": 0.0},
                "algorithm": {"method": "DEFAULT-ALGO"},
            }
        )
    )

    assert cancel_calls == 0
    assert output.status == ExecutionStatus.NOOP
    assert output.success is True


def test_execute_aborts_before_dispatch_when_cancel_all_orders_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账户级撤单失败时，应直接中止 execution，不再分发任何 symbol。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor()
    algorithm_calls: list[str] = []

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_cancel_all_orders() -> None:
        raise RuntimeError("cancel_all_orders failed")

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            algorithm_calls.append(f"{name}:{algorithm_input.symbol}")
            return AlgorithmResult(
                orders=[],
                account_assets=executor.get_account_assets(),
                target_volume=algorithm_input.target_volume or 0.0,
                first_tick=_price(algorithm_input.symbol),
                memory={"algorithm": name},
            )

        return _algorithm

    monkeypatch.setattr(executor, "cancel_all_orders", fake_cancel_all_orders)
    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    with pytest.raises(RuntimeError, match="cancel_all_orders failed"):
        executor.execute(
            UnifiedStandardInput.from_dict(
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
        )

    assert algorithm_calls == []


@pytest.mark.parametrize(
    "reduce_status", [None, ExecutionStatus.FAILED, ExecutionStatus.PARTIAL, ExecutionStatus.BLOCKED]
)
def test_execute_blocks_open_phase_when_reduce_phase_has_failure(
    monkeypatch: pytest.MonkeyPatch,
    reduce_status: ExecutionStatus | None,
) -> None:
    """第一阶段失败时，不应继续执行第二阶段。"""
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    executor = _TestExecutor(
        account_assets_snapshots=[
            _assets(positions=[("rb2610", 5.0, PositionDirection.LONG)]),
        ]
    )
    calls: list[str] = []

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_exec: AbstractExecutor, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            calls.append(algorithm_input.symbol)
            if algorithm_input.symbol == "rb2610":
                if reduce_status is None:
                    raise RuntimeError("rb2610 reduce failed")
                return AlgorithmResult(status=reduce_status, error="rb2610 reduce failed")
            return AlgorithmResult(
                orders=[],
                account_assets=_assets(positions=[]),
                target_volume=algorithm_input.target_volume,
                first_tick=_price(algorithm_input.symbol),
                memory={"algorithm": name},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "curr_target": {"rb2610": 0.2, "ag2612": 0.3},
                "algorithm": {"method": "DEFAULT-ALGO", "params": {"timeout": 3}},
            }
        )
    )

    assert calls == ["rb2610"]
    assert output.success is False
    # 减仓失败 + 开仓阻塞、无任何品种成功 → 整体判 FAILED（不再被 BLOCKED 稀释成 PARTIAL）。
    assert not output.success
    assert output.symbol_results["rb2610"].status == (reduce_status or ExecutionStatus.FAILED)
    assert output.symbol_results["ag2612"].status == ExecutionStatus.BLOCKED
    assert output.symbol_results["ag2612"].error == "第一阶段存在未成功的 symbol，已跳过后续开仓阶段"


def test_execution_session_pending_orders_only_return_current_symbol_orders() -> None:
    """ExecutionSession 的挂单查询应始终限制在当前 symbol。"""
    from axile.executor.execution_session import ExecutionSession

    executor = _ScopedOrderViewsExecutor(
        pending_orders=[
            UnifiedOrder(
                order_id="pending-rb",
                symbol="rb2610",
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=1.0,
                price=100.0,
                status="NEW",
            ),
            UnifiedOrder(
                order_id="pending-ag",
                symbol="ag2612",
                direction=OrderDirection.SELL,
                order_type=OrderType.LIMIT,
                volume=1.0,
                price=200.0,
                status="NEW",
            ),
        ]
    )
    session = ExecutionSession(owner=executor, symbol="rb2610")

    assert [order.order_id for order in session.get_pending_orders()] == ["pending-rb"]


def test_execute_maps_account_control_block_to_blocked_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防护层抛出的阻断异常应映射为 BLOCKED。"""
    from axile.executor import execution_engine as execution_engine_module

    executor = _TestExecutor()

    def fake_resolve_algorithm(_name: str, _executor: AbstractExecutor) -> Any:
        def _algorithm(_session: object, _algorithm_input: AlgorithmInput) -> AlgorithmResult:
            raise AccountControlBlockedError(
                "account control blocked rb2610",
                account_id=1,
                execution_id="exec-guard",
                channel=TradeChannel.CTP,
                operation="place_order",
                symbol="rb2610",
            )

        return _algorithm

    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)

    output = executor.execute(
        UnifiedStandardInput.from_dict(
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
                "algorithm": {"method": "DEFAULT-ALGO"},
            }
        ),
        cleanup=False,
    )

    assert output.status == ExecutionStatus.BLOCKED
    assert output.symbol_results["rb2610"].status == ExecutionStatus.BLOCKED
    assert "account control blocked" in cast(str, output.symbol_results["rb2610"].error)


def _trivial_algorithm_result(symbol: str) -> AlgorithmResult:
    """构造并发上限测试用的最小算法结果。"""
    return AlgorithmResult(
        orders=[],
        account_assets=UnifiedAccountAssets(
            available_cash=0.0,
            total_asset=0.0,
            market_value=0.0,
            positions=[],
        ),
        target_volume=0.0,
        first_tick=_price(symbol),
        memory={},
    )


def _prepared_task(symbol: str) -> Any:
    """构造仅供并发上限测试使用的 `_PreparedSymbolAlgorithm`（算法输入不会被真正读取）。"""
    from axile.executor.execution_engine import _PreparedSymbolAlgorithm

    return _PreparedSymbolAlgorithm(
        symbol=symbol,
        algorithm_name="CAP-ALGO",
        algorithm_input=cast("AlgorithmInput", None),
    )


def test_parallel_dispatch_caps_workers_at_derived_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务数超过限额推导上限时，线程池并发度应被钳到上限而非任务数。"""
    from concurrent.futures import ThreadPoolExecutor as _RealPool

    from axile.executor import execution_engine as execution_engine_module

    captured: dict[str, object] = {}

    class _SpyPool(_RealPool):
        def __init__(self, *args: object, max_workers: int | None = None, **kwargs: object) -> None:
            captured["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_engine_module, "ThreadPoolExecutor", _SpyPool)
    executor = _TestExecutor()
    executor.max_parallel_symbol_workers = 2
    engine = executor._execution_engine()
    monkeypatch.setattr(engine, "_run_symbol_algorithm", lambda task: _trivial_algorithm_result(task.symbol))

    tasks = [_prepared_task(f"SYM{i}") for i in range(5)]
    results = engine._run_symbol_algorithms_in_parallel(tasks)

    assert captured["max_workers"] == 2
    assert len(results) == 5


def test_parallel_operator_terminate_merges_all_cancel_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行人工终止应汇总每个品种的撤单失败订单。"""
    engine = _TestExecutor()._execution_engine()
    tasks = [_prepared_task(symbol) for symbol in ("rb2610", "ag2612", "au2612")]

    def terminate(task: Any) -> AlgorithmResult:
        raise ExecutionTerminated(
            reason="manual stop",
            mode="cancel_pending",
            cancel_failed_order_ids=[f"order-fail-{task.symbol}"],
        )

    monkeypatch.setattr(engine, "_run_symbol_algorithm", terminate)

    with pytest.raises(ExecutionTerminated) as exc_info:
        engine._run_symbol_algorithms_in_parallel(tasks)

    assert exc_info.value.mode == "cancel_pending"
    assert sorted(exc_info.value.cancel_failed_order_ids) == sorted(f"order-fail-{task.symbol}" for task in tasks)


class _DeadlineParallelExecutor(_TestExecutor):
    """记录各品种撤单尝试的并行执行器，用于验证总超时下的并发收尾。"""

    def __init__(self, pending_by_symbol: dict[str, list[UnifiedOrder]]) -> None:
        self._pending_by_symbol = pending_by_symbol
        self.cancel_attempts: list[tuple[str, str]] = []
        self._cancel_lock = Lock()
        super().__init__()

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        # symbol=None 是编排层 dispatch 前的账户级全撤查询；此时先不报挂单，
        # 好让待验证的挂单留到各品种线程的 terminate 收尾里去撤。
        if symbol is None:
            return []
        # 与真实渠道一样只报活动单：不过滤的话，构造出非活动状态的挂单也会被"撤掉"，
        # 撤单断言就会在夹具状态写错时依然通过。
        return [order.model_copy(deep=True) for order in self._pending_by_symbol.get(symbol, []) if order.is_active()]

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        with self._cancel_lock:
            self.cancel_attempts.append((symbol, order_id))
        # order-fail-* 模拟撤单失败，用于观察多线程下失败清单的收敛行为。
        return not order_id.startswith("order-fail")


def _resting_order(symbol: str, order_id: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.SUBMITTED,
    )


class _VirtualClock:
    """推进虚拟时间并记录累计等待的时钟。

    Notes
    -----
    用虚拟时间而非真实等待，是因为这两条用例一旦回归就表现为「各品种线程睡满算法
    预算」（几百到几千秒）。本仓库没有配置全局测试超时，真的去等会把整个测试套挂住
    而不是干脆失败；推进虚拟时间则让回归瞬间完成，再由用例断言 :attr:`advanced` 来
    抓出「等待没有被钳制」。

    断言刻意放在用例里而不是这里抛异常：品种级异常会被编排层的错误捕获吞掉转成失败
    结果，在时钟内部抛断言反而看不见——更糟的是，被吞掉之前虚拟时间已经推过了头，
    下一个检查点照样会抛终止异常，让用例「因为错误的理由通过」。

    ``time()`` 必须落在真实纪元上：runtime 用 ``clock_now()`` 折算 ``datetime`` 来量
    已耗时，换成 ``monotonic`` 那种小基数会让 elapsed 永远追不上 deadline。
    """

    def __init__(self) -> None:
        self._base = time()
        self._advanced = 0.0
        self._lock = Lock()

    @property
    def advanced(self) -> float:
        """本次执行里累计推进的等待秒数。"""
        with self._lock:
            return self._advanced

    def _advance(self, seconds: float) -> None:
        with self._lock:
            self._advanced += max(seconds, 0.0)

    def time(self) -> float:
        with self._lock:
            return self._base + self._advanced

    def sleep(self, seconds: float) -> None:
        self._advance(seconds)

    def event_wait(self, event: Event, timeout: float) -> bool:
        self._advance(timeout)
        return event.is_set()


def _install_waiting_algorithm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    barrier: Barrier,
    wait_seconds: float,
) -> None:
    """注册一个「先会合、再长时间协作等待」的假算法。

    会合点保证所有品种线程都已进入等待，从而让总超时对它们同时成立——这正是
    实盘里多品种并行跑满额度的形态。
    """
    from axile.executor import execution_engine as execution_engine_module
    from axile.executor.algorithms.core import base as algorithm_base

    class _Params:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

    class _Meta:
        params_class = _Params

    def fake_get_algorithm_metadata(_name: str) -> _Meta:
        return _Meta()

    def fake_resolve_algorithm(_name: str, _executor: object) -> Any:
        def _algorithm(session: Any, algorithm_input: AlgorithmInput) -> AlgorithmResult:
            barrier.wait(timeout=5)
            # 算法自己愿意等很久；能否被打断完全取决于执行层总超时。
            session.sleep_or_terminate(wait_seconds)
            return AlgorithmResult(
                orders=[],
                account_assets=session.get_account_assets(),
                target_volume=algorithm_input.target_volume or 0.0,
                first_tick=_price(algorithm_input.symbol),
                memory={},
            )

        return _algorithm

    monkeypatch.setattr(algorithm_base, "get_algorithm_metadata", fake_get_algorithm_metadata)
    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", fake_resolve_algorithm)


def _use_virtual_clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    """把全局默认时钟换成虚拟时钟；``monkeypatch`` 负责用例结束后还原。"""
    from axile.executor.algorithms.utils import clock as clock_module

    clock = _VirtualClock()
    monkeypatch.setattr(clock_module, "_default_clock", clock)
    return clock


def _multi_symbol_input(symbols: list[str], *, execution_timeout: int) -> UnifiedStandardInput:
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
            "curr_target": {symbol: 0.1 for symbol in symbols},
            "algorithm": {"method": "DEFAULT-ALGO", "params": {"timeout": 3}},
            "execution_timeout": execution_timeout,
        }
    )


def test_parallel_symbols_terminate_on_deadline_without_cancelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """多品种并行超时时应整体硬中断，且不发任何撤单请求。

    这是支持按品种并行的渠道形态：deadline 同时对所有在途
    线程成立。超时是兜底中断而非有序收尾——若每个品种都要先等自己的撤单往返，
    渠道挂死时 deadline 会跟着挂死，兜底就失去意义。
    """
    symbols = ["rb2610", "ag2612", "au2612"]
    executor = _DeadlineParallelExecutor({symbol: [_resting_order(symbol, f"order-{symbol}")] for symbol in symbols})
    # 算法各自愿意等 600s；能否被打断完全取决于总超时。
    _install_waiting_algorithm(monkeypatch, barrier=Barrier(len(symbols)), wait_seconds=600.0)
    _use_virtual_clock(monkeypatch)

    with pytest.raises(ExecutionTerminated) as exc_info:
        executor.execute(_multi_symbol_input(symbols, execution_timeout=1))

    assert exc_info.value.trigger == "timeout"
    assert exc_info.value.mode == "graceful"
    assert executor.cancel_attempts == []


def test_parallel_deadline_does_not_wait_full_algorithm_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """并行品种的等待应被钳到剩余额度，而不是各自睡满算法预算。

    用虚拟时钟断言「累计等了多久」而不是真实墙钟：回归时各线程会睡满算法预算，
    虚拟时间会被推到几千秒，用例立刻失败且无需真的等待。
    """
    symbols = ["rb2610", "ag2612"]
    algorithm_budget = 3600.0
    executor = _DeadlineParallelExecutor({symbol: [] for symbol in symbols})
    _install_waiting_algorithm(monkeypatch, barrier=Barrier(len(symbols)), wait_seconds=algorithm_budget)
    clock = _use_virtual_clock(monkeypatch)

    with pytest.raises(ExecutionTerminated) as exc_info:
        executor.execute(_multi_symbol_input(symbols, execution_timeout=1))

    assert exc_info.value.trigger == "timeout"
    # 钳制生效时累计等待应在总超时量级；未钳制则会逼近「品种数 × 算法预算」。
    assert clock.advanced < algorithm_budget, (
        f"累计等待 {clock.advanced:.1f}s 达到了算法预算量级：并行品种的等待没有被总超时钳制"
    )


def test_parallel_operator_terminate_reports_every_symbol_cancel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工 cancel_pending 终止时，记录必须报出全部品种的游离挂单，一个都不能丢。

    ``cancel_failed_order_ids`` 是运维唯一能看到「有单子被留在交易所」的信号。
    多线程同时终止时若只有先抛出的那个品种的清单能存活，其余品种的失败挂单就会
    在审计里彻底消失——记录显示一切正常，实际仓位敞口还挂在盘上。

    Notes
    -----
    走人工终止而非总超时：超时已改为不撤单的硬中断，只有 ``cancel_pending`` 这条路
    才会在并行品种上真正逐笔撤单，也才有「失败清单被吞掉」的风险。
    """
    symbols = ["rb2610", "ag2612", "au2612"]
    # 全部品种都撤单失败：避免依赖 as_completed 的先后顺序，让断言稳定可复现。
    executor = _DeadlineParallelExecutor(
        {symbol: [_resting_order(symbol, f"order-fail-{symbol}")] for symbol in symbols}
    )
    # terminate 请求在各品种线程都已进场后才置位：编排层在 dispatch 前还有一次
    # 账户级 cancel_all_orders 检查点，提前置位会让执行在进入并行阶段前就被截断。
    cancel_event = Event()
    executor.set_termination_controller(
        ExecutionTerminationController(
            cancel_event=cancel_event,
            reason_provider=lambda: "manual stop",
            mode_provider=lambda: "cancel_pending",
        )
    )
    _install_waiting_algorithm(
        monkeypatch,
        barrier=Barrier(len(symbols), action=cancel_event.set),
        wait_seconds=600.0,
    )
    _use_virtual_clock(monkeypatch)

    with pytest.raises(ExecutionTerminated) as exc_info:
        executor.execute(_multi_symbol_input(symbols, execution_timeout=600))

    assert exc_info.value.trigger == "operator"
    assert sorted(exc_info.value.cancel_failed_order_ids) == sorted(f"order-fail-{symbol}" for symbol in symbols)


@pytest.mark.parametrize("last_target", [{}, {"rb2610": 0.1}])
def test_execute_empty_target_on_flat_account_is_noop(monkeypatch, last_target) -> None:
    executor = _TestExecutor()

    def unexpected_call(*args, **kwargs):
        pytest.fail("空仓账户不应下单或撤单")

    monkeypatch.setattr(executor, "cancel_all_orders", unexpected_call)
    monkeypatch.setattr(executor, "_place_order_impl", unexpected_call)
    output = executor.execute(
        UnifiedStandardInput(
            channel_type=TradeChannel.CTP,
            account_config=executor.account_config,
            curr_target={},
            last_target=last_target,
        )
    )

    assert output.success is True
    assert output.status == ExecutionStatus.NOOP
    assert output.inputs.curr_target == {}
    if not last_target:
        assert output.symbol_results == {}
        assert executor.market_data_requests == []


@pytest.mark.parametrize("direction", [PositionDirection.LONG, PositionDirection.SHORT])
def test_execute_empty_target_dispatches_existing_position_to_zero(monkeypatch, direction) -> None:
    from axile.executor import execution_engine as execution_engine_module

    executor = _TestExecutor(account_assets_snapshots=[_assets(positions=[("rb2610", 5.0, direction)])])
    calls = []

    def resolve_algorithm(name, session):
        def algorithm(executor, inputs):
            calls.append((inputs.symbol, inputs.target_volume))
            return AlgorithmResult(
                orders=[],
                account_assets=_assets(),
                target_volume=inputs.target_volume,
                first_tick=_price(inputs.symbol),
            )

        return algorithm

    monkeypatch.setattr(execution_engine_module, "resolve_algorithm", resolve_algorithm)
    output = executor.execute(
        UnifiedStandardInput(
            channel_type=TradeChannel.CTP,
            account_config=executor.account_config,
            curr_target={},
        )
    )

    assert calls == [("rb2610", 0.0)]
    assert output.success is True
    assert output.inputs.curr_target == {}
