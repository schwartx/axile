"""组合目标函数的任务取消透传：``CancelledError`` 不得被转成失败结果."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from axile.server.portfolio_targets import PortfolioFunctionError, calculate_portfolio_for_account


class _CancellingManager:
    """模拟 worker manager 在任务取消时直接抛 CancelledError."""

    async def calculate_portfolio(self, account: object, code: str, *, execution_id: str | None = None) -> object:
        raise asyncio.CancelledError


def test_calculate_portfolio_reraises_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """操作员终止引发的取消必须原样上抛，而不是被包装成 ok=False 的失败结果。"""
    monkeypatch.setattr(
        "axile.server.execution.worker_backend.manager.get_worker_backend_manager",
        lambda: _CancellingManager(),
    )

    async def scenario() -> Any:
        return await calculate_portfolio_for_account(object(), "def target(ctx): return {}", execution_id="exec-1")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


@pytest.mark.parametrize("structured", [False, True])
def test_portfolio_target_preserves_structured_errors_and_wraps_getter_failures(monkeypatch, structured):
    error = (
        PortfolioFunctionError("script", error_line=4, error_type="SyntaxError") if structured else RuntimeError("init")
    )

    def getter():
        if not structured:
            raise error

        class Manager:
            async def calculate_portfolio(self, *args, **kwargs):
                raise error

        return Manager()

    monkeypatch.setattr("axile.server.execution.worker_backend.manager.get_worker_backend_manager", getter)
    with pytest.raises(PortfolioFunctionError) as caught:
        asyncio.run(calculate_portfolio_for_account(object(), "code"))
    if structured:
        assert caught.value is error and caught.value.error_line == 4
    else:
        assert caught.value.error_type == "RuntimeError" and caught.value.error_message == "init"
