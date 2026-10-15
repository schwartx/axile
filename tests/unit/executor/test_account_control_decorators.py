"""账户控制装饰器测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

import axile.executor.account_control.decorators as decorators_module
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.decorators import controlled_operation
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.registry import (
    get_default_account_control_registry,
    reset_default_account_control_registry_for_tests,
)
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot


def _clock() -> datetime:
    return datetime(2026, 3, 25, 10, 0, 0)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    reset_default_account_control_registry_for_tests()


def _build_guard() -> AccountControlGuard:
    return AccountControlGuard(
        account_id=77,
        execution_id="exec-decorator",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy("default"),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )


def test_controlled_operation_registers_defaults_and_records_single_event() -> None:
    """装饰器应只注册结构信息并完成一次性记账。"""

    class DemoExecutor:
        def __init__(self, guard: AccountControlGuard) -> None:
            self._account_control_guard = guard

        @controlled_operation(
            "demo_place",
            symbol_arg="symbol",
            success_outcome="submitted",
            result_metadata_resolver=lambda result: {"order_id": result["order_id"]},
        )
        def place(self, symbol: str) -> dict[str, str]:
            return {"order_id": f"oid-{symbol}"}

    registry = get_default_account_control_registry()
    registered = registry.require_operation("demo_place")
    assert registered.key == "demo_place"

    guard = AccountControlGuard(
        account_id=77,
        execution_id="exec-decorator",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                {
                    "operations": {
                        "demo_place": {
                            "account": {"per_day": {"limit": 2, "on_trigger": "block"}},
                            "symbol": {"min_interval_ms": {"limit": 300, "on_trigger": "wait"}},
                        }
                    }
                }
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )
    result = DemoExecutor(guard).place("rb2610")

    assert result["order_id"] == "oid-rb2610"
    _, events = guard.flush_records()
    assert len(events) == 1
    assert events[0].operation == "demo_place"
    assert events[0].symbol == "rb2610"
    assert events[0].metadata == {"order_id": "oid-rb2610"}
    assert events[0].outcome == "submitted"


def test_controlled_operation_allows_duplicate_operation_key_with_different_symbol_binding() -> None:
    """同一个 operation key 的 symbol 绑定差异不应由注册层判冲突。"""

    @controlled_operation(
        "demo_query",
        symbol_arg="symbol",
    )
    def _first(self, symbol: str) -> None:
        _ = symbol
        return None

    assert _first is not None

    @controlled_operation(
        "demo_query",
        success_outcome="submitted",
    )
    def _second(self) -> None:
        return None

    assert _second is not None


def test_controlled_operation_prefers_explicit_guard_getter(monkeypatch: pytest.MonkeyPatch) -> None:
    """装饰器应优先读取显式 guard getter，而不是依赖兼容字段。"""
    captured: dict[str, object] = {}

    def fake_run_controlled_call(
        *,
        guard: object,
        operation: str,
        call: object,
        symbol: str | None = None,
        metadata: object = None,
        success_outcome: object = "succeeded",
        result_metadata_resolver: object = None,
    ) -> object:
        captured.update(
            {
                "guard": guard,
                "operation": operation,
                "symbol": symbol,
                "metadata": metadata,
            }
        )
        return call()

    monkeypatch.setattr(decorators_module, "run_controlled_call", fake_run_controlled_call)

    class DemoExecutor:
        def __init__(self, guard: AccountControlGuard) -> None:
            self._guard = guard

        def get_account_control_guard(self) -> AccountControlGuard:
            return self._guard

        @controlled_operation("demo_query", symbol_arg="symbol")
        def query(self, symbol: str) -> str:
            return symbol

    guard = _build_guard()

    assert DemoExecutor(guard).query("rb2610") == "rb2610"
    assert captured == {
        "guard": guard,
        "operation": "demo_query",
        "symbol": "rb2610",
        "metadata": {},
    }
