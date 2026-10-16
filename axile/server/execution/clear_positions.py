"""清仓执行场景."""

from typing import cast

import loguru

from axile.domain.execution import ExecutionKind
from axile.executor.termination import ExecutionTerminated
from axile.server.core.log_config import execution_log_context
from axile.server.db.models import (
    Account,
    ExecuteRecord,
    new_execution_id,
)
from axile.server.db.models.account import AccountContext
from axile.server.execution import backend as execution_backend
from axile.server.execution import lifecycle as execution_lifecycle
from axile.server.execution.execution_algorithms import (
    resolve_empty_positions_algorithm,
    resolve_execution_algorithm_name,
)
from axile.server.execution.execution_records_output import sanitize_standard_input_for_audit
from axile.server.execution.notification_assets import load_notification_snapshot
from axile.server.execution.registry import clear_running_execution, register_inline_execution
from axile.server.utils import trade_channel_check


async def empty_positions(
    account: AccountContext,
    algorithm: dict[str, object] | None = None,
    logger: "loguru.Logger | None" = None,
    *,
    execution_id: str | None = None,
    lock_acquired: bool = False,
) -> ExecuteRecord:
    """
    使用已配置的清仓算法清空账户全部持仓.

    Parameters
    ----------
    account : Account
        待清仓的账户对象。
    algorithm : dict[str, object] | None, optional
        显式指定的清仓算法配置；为空时使用账户级默认配置。
    logger : loguru.Logger | None, optional
        用于输出日志的 logger；未提供时使用全局 logger。
    execution_id : str | None, optional
        指定的 execution 标识；为空且未持锁时按当前流程注册生成。
    lock_acquired : bool, optional
        调用方是否已完成运行中任务互斥注册。为 ``True`` 时复用既有
        execution 上下文，不再重复注册与释放，交由上层统一收敛生命周期。

    Returns
    -------
    ExecuteRecord
        本次清仓对应的执行记录。
    """
    if logger is None:
        logger = loguru.logger

    if isinstance(account, Account):
        account = account.snapshot()
    resolved_algorithm = resolve_empty_positions_algorithm(account, algorithm)
    if lock_acquired:
        tracked_execution_id = cast("str", execution_id)
    else:
        tracked_execution_id = register_inline_execution(
            account=account,
            execution_kind=ExecutionKind.CLEAR_POSITIONS,
            execution_id=execution_id,
            algorithm_name=resolve_execution_algorithm_name(
                account,
                ExecutionKind.CLEAR_POSITIONS,
                algorithm_override=resolved_algorithm,
            ),
        )

    with logger.contextualize(
        **execution_log_context(
            account_id=account.id,
            account_name=account.name,
            channel=account.trade_channel,
            execution_id=tracked_execution_id,
        )
    ):
        try:
            logger.info("清除持仓")
            await trade_channel_check(account, execution_id=tracked_execution_id)
            record = await __empty_positions(
                account,
                algorithm=resolved_algorithm,
                execution_id=tracked_execution_id,
            )
            if not lock_acquired:
                await execution_lifecycle.mark_inline_execution_succeeded(
                    tracked_execution_id,
                    record,
                )
            return record
        except ExecutionTerminated as exc:
            if not lock_acquired:
                await execution_lifecycle.handle_inline_execution_terminated(
                    account_id=cast("int", account.id),
                    execution_id=tracked_execution_id,
                    execution_kind=ExecutionKind.CLEAR_POSITIONS,
                    exc=exc,
                )
            raise
        except Exception as e:
            if not lock_acquired:
                await execution_lifecycle.handle_inline_execution_failure(
                    account=account,
                    execution_id=tracked_execution_id,
                    error=e,
                )
            raise
        finally:
            if not lock_acquired:
                clear_running_execution(cast("int", account.id), tracked_execution_id)


async def __empty_positions(
    account: AccountContext,
    algorithm: dict[str, object] | None = None,
    execution_id: str | None = None,
    logger: "loguru.Logger | None" = None,
) -> ExecuteRecord:
    """
    运行清仓流程，并持久化对应的执行记录.

    Parameters
    ----------
    account : Account
        待清仓的账户对象。
    algorithm : dict[str, object] | None, optional
        显式指定的清仓算法配置；为空时使用账户级默认配置。
    execution_id : str | None, optional
        当前 execution 标识。
    logger : loguru.Logger | None, optional
        用于输出日志的 logger；未提供时使用全局 logger。

    Returns
    -------
    ExecuteRecord
        本次清仓对应的执行记录。
    """
    if logger is None:
        logger = loguru.logger

    local_execution_id = execution_id or new_execution_id()
    notification_snapshot = await load_notification_snapshot(account)
    request = _build_clear_positions_backend_request(
        account=account,
        algorithm=algorithm,
        execution_id=local_execution_id,
        logger=logger,
        notification_snapshot=notification_snapshot,
    )
    return await execution_backend.run_clear_positions_via_backend(request=request)


def _build_clear_positions_backend_request(
    *,
    account: AccountContext,
    algorithm: dict[str, object] | None,
    execution_id: str,
    logger: "loguru.Logger",
    notification_snapshot: dict[str, object] | None = None,
) -> execution_backend.ClearPositionsBackendRequest:
    """组装后端执行清仓所需的请求对象。"""
    if isinstance(account, Account):
        account = account.snapshot()
    resolved_algorithm = resolve_empty_positions_algorithm(account, algorithm)
    empty_kwargs = {
        "trade_rules": account.trade_rules,
        "futures_close_policy": account.futures_close_policy.model_dump(),
        "algorithm": resolved_algorithm,
        "feishu_key": account.feishu_key if account.feishu_key else None,
        "execution_notification_code": account.execution_notification_code,
        "feishu_account": {
            "id": account.id,
            "name": account.name,
            "market": account.market,
            "trade_channel": str(account.trade_channel),
            "brokerage": account.brokerage,
            "remark": account.remark,
            "portfolio_id": account.portfolio_id,
            "weight_precision": account.weight_precision,
            "long_leverage": account.long_leverage,
            "short_leverage": account.short_leverage,
        },
        "extra": {
            "audit": {
                "execution_id": execution_id,
                "account_id": account.id,
                "channel": str(account.trade_channel),
                "algorithm": resolved_algorithm.get("method", "SINGLE-MAKER"),
                "trigger_source": "empty_positions",
                "execution_kind": "clear_positions",
            }
        },
    }
    return execution_backend.ClearPositionsBackendRequest(
        account=account,
        resolved_algorithm=resolved_algorithm,
        empty_kwargs=empty_kwargs,
        audit_input=sanitize_standard_input_for_audit(empty_kwargs),
        execution_id=execution_id,
        trigger_source="empty_positions",
        logger=logger,
        notification_snapshot=notification_snapshot,
    )
