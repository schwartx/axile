"""账户连接操作与调度启停分离；连接恢复不触发任何交易执行。"""

import asyncio

from fastapi import APIRouter, HTTPException, Response

from axile.channels import get_channel
from axile.executor.connection_progress import ConnectionTable
from axile.server.api.routes.account_crud import account_connection
from axile.server.api.routes.account_support import (
    _get_account_or_404,
    _get_account_snapshot_or_404,
    ensure_account_current,
)
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_connection import AccountConnectionDetail
from axile.server.execution.registry import get_queued_execution_id, get_running_execution_id
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.worker_backend.manager import get_worker_backend_manager

router = APIRouter()
_reconnect_tasks: dict[tuple[int, bool], asyncio.Task[Response]] = {}


def _check_idle(account_id: int) -> None:
    if get_running_execution_id(account_id) or get_queued_execution_id(account_id):
        raise HTTPException(409, detail="账户有待执行或正在执行的任务，当前不能操作连接")


async def _operate_connection(account: AccountContext, *, reconnect: bool) -> None:
    manager = get_worker_backend_manager()
    if reconnect:
        await manager.prepare_account(account, force_reconnect=True)
    else:
        await manager.disconnect_account(account)


async def _connection_operation(account_id: int, *, reconnect: bool, connect_account: bool = False) -> Response:
    async with account_runtime_lock(account_id):
        _check_idle(account_id)
        account = await _get_account_snapshot_or_404(account_id)
        ensure_account_current(account)
        try:
            if connect_account:
                plugin = get_channel(account.trade_channel)
                if plugin.connect_account is None:
                    raise HTTPException(400, "当前渠道不支持连接交易账户，请按连接说明处理")
                config = plugin.account_config_model.model_validate(account.account_config)
                await asyncio.to_thread(plugin.connect_account, config)
            await _operate_connection(account, reconnect=reconnect)
        except HTTPException:
            raise
        except Exception as exc:
            # 原始连接证据仍由渠道/宿主保存；HTTP 摘要不推断凭证失效。
            message = str(getattr(exc, "execution_error", None) or "账户连接操作失败，请查看连接详情")
            raise HTTPException(502, detail=message) from exc
        return await account_connection(account_id)


@router.post("/{account_id}/connection/reconnect", response_model=AccountConnectionDetail)
async def reconnect_account(account_id: int) -> Response:
    """显式恢复连接，可绕过冷却；健康会话仍复用，不重放下单或撤单。"""
    return await _start_connection(account_id)


@router.post("/{account_id}/connection/connect-account", response_model=AccountConnectionDetail)
async def connect_trading_account(account_id: int) -> Response:
    """用户显式调用渠道登录能力，随后核对并恢复公共会话。"""
    return await _start_connection(account_id, connect_account=True)


async def _start_connection(account_id: int, *, connect_account: bool = False) -> Response:
    """合并同一入口的在途操作；不同入口仍由账户运行锁串行化。"""
    await _get_account_or_404(account_id)
    key = (account_id, connect_account)
    task = _reconnect_tasks.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_connection_operation(account_id, reconnect=True, connect_account=connect_account))
        _reconnect_tasks[key] = task

        def completed(done):
            if _reconnect_tasks.get(key) is done:
                _reconnect_tasks.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
    # HTTP 断开不能取消已开始的连接，也不能让后续调用重复登录。
    return await asyncio.shield(task)


@router.post("/{account_id}/connection/disconnect", response_model=AccountConnectionDetail)
async def disconnect_account(account_id: int) -> Response:
    """主动断开账户会话；定时任务的启停配置保持独立。"""
    return await _connection_operation(account_id, reconnect=False)


@router.get("/{account_id}/connection/progress/{run_id}/steps/{step_id}/table", response_model=ConnectionTable)
async def get_connection_table(account_id: int, run_id: str, step_id: str, response: Response) -> ConnectionTable:
    """读取本次连接阶段的记录快照，不访问柜台。"""
    from axile.server.connection_runtime import connection_table
    from axile.server.connection_state import connection_config_key

    response.headers["Cache-Control"] = "no-store"
    account = await _get_account_or_404(account_id)
    key = connection_config_key(account.trade_channel, account.account_config)
    table = connection_table(account_id, key, run_id, step_id)
    if table is None:
        raise HTTPException(404, "连接过程记录不存在或已被新连接替换")
    return ConnectionTable.model_validate(table)
