"""账户路由共享的验证、账户读取与调度辅助函数."""

from __future__ import annotations

import json
from typing import cast

from fastapi import HTTPException, status

from axile.server.api.deps import SchedDep
from axile.server.cron import (
    combine_cron_triggers,
    cron_triggers_equivalent,
    is_blank_cron_expr,
    parse_cron_expr,
)
from axile.server.db.models import Account
from axile.server.db.models.account import AccountContext
from axile.server.execution.scheduler import create_job, delete_job
from axile.server.repositories import get_account, get_latest_portfolio_id_by_account_id


async def _get_account_or_404(account_id: int) -> Account:
    """读取账户，不存在时抛出 404."""
    account = await get_account(account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账户不存在")
    return account


def _build_empty_positions_algorithm(
    algorithm_method: str | None = None,
    algorithm_params: str | None = None,
) -> dict[str, object] | None:
    """
    构造清仓接口使用的算法配置.

    Parameters
    ----------
    algorithm_method : str | None, optional
        清仓算法名称。
    algorithm_params : str | None, optional
        JSON 字符串形式的算法参数。

    Returns
    -------
    dict[str, object] | None
        标准化后的算法配置；未提供任何参数时返回 ``None``。

    Raises
    ------
    ValueError
        当参数 JSON 非法，或仅提供参数未提供算法名时抛出。
    """
    method = algorithm_method.strip() if algorithm_method else ""
    params_raw = algorithm_params.strip() if algorithm_params else ""

    if not method and not params_raw:
        return None

    if params_raw and not method:
        raise ValueError("提供 algorithm_params 时必须同时提供 algorithm_method")

    params: dict[str, object] = {}
    if params_raw:
        try:
            parsed = json.loads(params_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"algorithm_params 不是合法 JSON: {exc.msg}") from exc

        if not isinstance(parsed, dict):
            raise ValueError("algorithm_params 必须是 JSON object")
        params = parsed

    return {
        "method": method,
        "params": params,
    }


async def _reconcile_account_job(
    sched: SchedDep,
    account: AccountContext,
) -> None:
    """
    对齐账户的调度任务状态.

    Parameters
    ----------
    sched : SchedDep
        当前调度器实例。
    account : Account
        需要对齐调度状态的账户对象。
    """
    account_id = cast("int", account.id)
    latest_portfolio_id = await get_latest_portfolio_id_by_account_id(account_id)
    await _apply_account_job(sched, account, latest_portfolio_id)


async def _apply_account_job(sched: SchedDep, account: AccountContext, latest_portfolio_id: int | None) -> None:
    """使用已提交的账户及绑定快照对齐 scheduler，不访问数据库。"""
    account_id = cast("int", account.id)
    # 关「自动调仓」→ 空 cron：合法，表示仅手动；与 is_started / 组合绑定一并决定是否建 job。
    should_have_job = bool(
        account.is_started and latest_portfolio_id is not None and not is_blank_cron_expr(account.cron_expr)
    )
    existing_job = sched.get_job(str(account_id))  # type: ignore[no-untyped-call]

    if not should_have_job:
        if existing_job is not None:
            delete_job(sched, account_id)
        return

    triggers = parse_cron_expr(account.cron_expr)
    desired_trigger = combine_cron_triggers(triggers)
    # CompactOrTrigger.__str__ 只有条数，不能当排程指纹。
    from axile.server.execution.scheduler import _scheduled_signal

    if (
        existing_job is not None
        and cron_triggers_equivalent(existing_job.trigger, desired_trigger)
        and (getattr(existing_job, "func", None) == _scheduled_signal) == bool(getattr(account, "supplement", None))
    ):
        return

    if existing_job is not None:
        delete_job(sched, account_id)
    await create_job(sched, account, triggers, binding_checked=True)  # type: ignore[misc]
