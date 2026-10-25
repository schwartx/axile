"""编译并执行用户提供的组合目标函数."""

from __future__ import annotations

import asyncio
from typing import cast

from axile.server.portfolio_function import (
    PortfolioFunctionError,
    _error_from_exception,
    calculate_portfolio_target,
)


async def calculate_portfolio_for_account(
    account: object,
    code: str,
    *,
    execution_id: str | None = None,
) -> dict[str, float]:
    """通过账户常驻 worker 运行自定义组合函数."""
    from axile.server.db.models import Account
    from axile.server.execution.worker_backend.manager import get_worker_backend_manager

    resolved_account = cast("Account", account)
    try:
        return await get_worker_backend_manager().calculate_portfolio(
            resolved_account,
            code,
            execution_id=execution_id,
        )
    except asyncio.CancelledError:
        # 任务取消必须原样上抛：在这里转成失败结果会截断终止协议，
        # 被终止的执行会被误记成「自定义脚本失败」，并与终止收尾并发落库。
        raise
    except BaseException as exc:  # noqa: BLE001 - worker 故障也使用结构化函数错误
        raise exc if isinstance(exc, PortfolioFunctionError) else _error_from_exception(exc)


__all__ = [
    "PortfolioFunctionError",
    "calculate_portfolio_for_account",
    "calculate_portfolio_target",
]
