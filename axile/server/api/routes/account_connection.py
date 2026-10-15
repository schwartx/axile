"""账户连接操作与调度启停分离；连接恢复不触发任何交易执行。"""

import asyncio

from fastapi import APIRouter, HTTPException

from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.connection_state import public_connection
from axile.server.core.db import SessionLocal
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_connection import AccountConnectionDetail
from axile.server.execution.registry import get_queued_execution_id, get_running_execution_id
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.worker_backend.manager import get_worker_backend_manager

router = APIRouter()
_reconnect_tasks: dict[int, asyncio.Task[AccountConnectionDetail]] = {}


def _check_idle(account_id: int) -> None:
    if get_running_execution_id(account_id) or get_queued_execution_id(account_id):
        raise HTTPException(409, detail="账户有待执行或正在执行的任务，当前不能操作连接")


async def _operate_connection(account: AccountContext, *, reconnect: bool) -> None:
    manager = get_worker_backend_manager()
    if reconnect:
        await manager.prepare_account(account, force_reconnect=True)
    else:
        await manager.disconnect_account(account)


async def _connection_operation(session: SessionDep, account_id: int, *, reconnect: bool) -> AccountConnectionDetail:
    async with account_runtime_lock(account_id):
        _check_idle(account_id)
        account = (await _get_account_or_404(session, account_id)).snapshot()
        await session.close()
        try:
            await _operate_connection(account, reconnect=reconnect)
        except Exception as exc:
            # 原始连接证据仍由渠道/宿主保存；HTTP 摘要不推断凭证失效。
            message = str(getattr(exc, "execution_error", None) or "账户连接操作失败，请查看连接详情")
            raise HTTPException(502, detail=message) from exc
        refreshed = await _get_account_or_404(session, account_id)
        return AccountConnectionDetail.model_validate(public_connection(refreshed, detail=True))


@router.post("/{account_id}/connection/reconnect", response_model=AccountConnectionDetail)
async def reconnect_account(session: SessionDep, account_id: int) -> AccountConnectionDetail:
    """显式恢复连接，可绕过冷却；健康会话仍复用，不重放下单或撤单。"""
    await _get_account_or_404(session, account_id)
    await session.close()
    task = _reconnect_tasks.get(account_id)
    if task is None or task.done():
        task = asyncio.create_task(_reconnect_in_owned_session(account_id))
        _reconnect_tasks[account_id] = task

        def completed(done):
            if _reconnect_tasks.get(account_id) is done:
                _reconnect_tasks.pop(account_id, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
    # HTTP 断开不能取消已开始的连接，也不能让后续调用重复登录。
    return await asyncio.shield(task)


async def _reconnect_in_owned_session(account_id: int) -> AccountConnectionDetail:
    """合并同时到达的重连请求；后台工作不持有调用方的数据库会话。"""
    async with SessionLocal() as session:
        return await _connection_operation(session, account_id, reconnect=True)


@router.post("/{account_id}/connection/disconnect", response_model=AccountConnectionDetail)
async def disconnect_account(session: SessionDep, account_id: int) -> AccountConnectionDetail:
    """主动断开账户会话；定时任务的启停配置保持独立。"""
    return await _connection_operation(session, account_id, reconnect=False)
