"""清仓执行场景."""

from typing import cast

import loguru

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionKind, ExecutionReasonFamily
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.termination import ExecutionTerminated
from axile.server.core.log_config import execution_log_context
from axile.server.db.models import Account, ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.error_details import exception_details, json_error_value
from axile.server.execution.execution_algorithms import resolve_empty_positions_algorithm
from axile.server.execution.input_persistence import sanitize_execution_input
from axile.server.execution.notification_assets import load_notification_snapshot
from axile.server.execution.records import append_error_execute_record, append_execute_record_from_output
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.execution_audit import append_execution_event
from axile.server.utils import trade_channel_check


async def _run_clear_positions(
    account: AccountContext,
    execution_id: str,
    algorithm: dict[str, object] | None = None,
) -> ExecuteRecord:
    """
    执行队列已认领的清仓任务，并持久化执行记录.

    Parameters
    ----------
    account : Account
        待清仓的账户对象。
    execution_id : str
        当前队列任务的 execution 标识。
    algorithm : dict[str, object] | None, optional
        显式指定的清仓算法配置；为空时使用账户级默认配置。

    Returns
    -------
    ExecuteRecord
        本次清仓对应的执行记录。
    """
    if isinstance(account, Account):
        account = account.snapshot()
    logger = loguru.logger
    with logger.contextualize(
        **execution_log_context(
            account_id=account.id,
            account_name=account.name,
            channel=account.trade_channel,
            execution_id=execution_id,
        )
    ):
        logger.info("清除持仓")
        await trade_channel_check(account, execution_id=execution_id)
        notification_snapshot = await load_notification_snapshot(account)
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
        audit_input = sanitize_execution_input(empty_kwargs)
        if execution_id:
            from axile.server.execution.intents import promote_intent_to_running

            await promote_intent_to_running(execution_id, cast("int", account.id))
        try:
            output = await get_worker_backend_manager().empty_positions(
                account=account,
                empty_kwargs=empty_kwargs,
                audit_input=audit_input,
                execution_id=execution_id,
                **({"notification_snapshot": notification_snapshot} if notification_snapshot else {}),
            )
            record = await append_execute_record_from_output(
                account=account,
                raw_input={},
                output=output,
                execution_id=execution_id,
                execution_kind=ExecutionKind.CLEAR_POSITIONS,
            )
            logger.info(
                f"名称={account.name} | 渠道={account.trade_channel} | 清仓完成 | "
                f"execution_id={execution_id} | record_id={record.id} | success={record.is_success}"
            )
            return record
        except ExecutionTerminated:
            logger.warning(
                f"名称={account.name} | 渠道={account.trade_channel} | 清仓被终止 | "
                f"execution_id={execution_id} | trigger=empty_positions"
            )
            raise
        except Exception as exc:
            msg = execution_error_message(exc, "清仓执行")
            await append_execution_event(
                execution_id=execution_id,
                account_id=cast("int", account.id),
                channel=account.trade_channel,
                algorithm=cast("str", resolved_algorithm.get("method", "SINGLE-MAKER")),
                event_type=ExecutionEventType.EXECUTION_FAILED,
                status=ExecutionEventStatus.ERROR,
                reason_family=ExecutionReasonFamily.SYSTEM,
                reason_code="COMMON.EXECUTION_FAILED",
                # worker 尚未准备成功时，由主进程保留失败证据。
                seq=0,
                # 阶段说明与技术原文一起进入既有事件。
                details=_failure_details(exc, msg, "empty_positions"),
            )
            await append_error_execute_record(
                account_id=account.id,
                msg=msg,
                raw_result=exception_details(exc),
                execution_id=execution_id,
                error=exc,
            )
            logger.opt(exception=exc).error("{}", msg)
            raise ValueError(execution_error_message(exc, "清仓执行")) from exc


def _failure_details(error: Exception, message: str, trigger_source: str) -> dict[str, object]:
    """保留渠道结构化分类和恢复时间；诊断原文完整进入事件。"""
    details: dict[str, object] = {
        **exception_details(error),
        "error": message,
        "debug": {"trigger_source": trigger_source},
    }
    diagnostics = getattr(error, "diagnostics", None)
    if isinstance(diagnostics, list):
        details["error_type"] = getattr(error, "exception_type", None) or type(error).__name__
        details["diagnostics"] = json_error_value(diagnostics)
    return details
