"""
执行记录持久化辅助函数.

按执行场景统一封装成功、失败与 terminated 三类 ``ExecuteRecord`` 的落库逻辑。
"""

from __future__ import annotations

import uuid
from typing import cast

import loguru

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.session_closed import session_closed_scope
from axile.executor.termination import TERMINATION_TRIGGER_OPERATOR
from axile.server._native import _core
from axile.server.business_facts.summary_refs import BusinessFactReferenceError
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.db.models.base import now_str
from axile.server.native_store import native_operation


async def _persist_execute_record(
    *,
    account_id: int,
    execution_id: str | None,
    raw_input: dict[str, object] | None,
    raw_result: dict[str, object],
    is_success: int,
) -> ExecuteRecord:
    """一次原生事务提交终态、输入、结果事实、资产历史与摘要引用。"""
    try:
        stored = await native_operation(
            "persist_execute_record",
            {
                "account_id": account_id,
                "execution_id": execution_id,
                "raw_input": raw_input,
                "raw_result": raw_result,
                "is_success": is_success,
                "created_at": now_str(),
                "input_id": str(uuid.uuid4()),
                "result_input_id": str(uuid.uuid4()),
            },
        )
    except _core.NativeError as error:
        if error.args and error.args[0] == "BUSINESS_FACTS":
            raise ValueError(error.args[1]) from None
        if error.args and error.args[0] == "BUSINESS_FACTS_REFERENCE":
            raise BusinessFactReferenceError(error.args[1]) from None
        raise
    return ExecuteRecord.model_validate(stored)


def resolve_record_task_status(record: ExecuteRecord | None) -> ExecutionTaskStatus:
    """从业务证据解析任务终态，休市未完成不能记作成功或异常失败。"""
    if record is None:
        return ExecutionTaskStatus.FAILED
    if record.task_status is not None:
        return ExecutionTaskStatus(record.task_status)
    return ExecutionTaskStatus.SUCCEEDED if record.is_success == 1 else ExecutionTaskStatus.FAILED


async def append_error_execute_record(
    account_id: int | None,
    raw_input: dict[str, object] | None = None,
    raw_result: dict[str, object] | None = None,
    msg: str = "",
    execution_id: str | None = None,
    logger: "loguru.Logger" = loguru.logger,
) -> ExecuteRecord:
    """
    添加失败的执行记录.

    Parameters
    ----------
    account_id : int | None
        本次执行对应的账户 ID。
    raw_input : dict[str, object] | None, optional
        需要持久化的输入快照。
    raw_result : dict[str, object] | None, optional
        需要持久化的结果快照；为空时会回填 ``msg``。
    msg : str, default=""
        错误日志和默认结果消息。
    execution_id : str | None, optional
        当前执行 ID。
    logger : loguru.Logger, optional
        用于输出错误日志的 logger。

    Returns
    -------
    ExecuteRecord
        已写入数据库的失败执行记录。
    """
    persisted_raw_result = dict(raw_result or {"msg": msg})
    # 柔和版契约：status/error 为权威结论，outcome 同步保留供旧展示路径消费。
    persisted_raw_result.setdefault("status", ExecutionStatus.FAILED.value)
    persisted_raw_result.setdefault("error", msg)
    persisted_raw_result.setdefault("outcome", "error")
    persisted_raw_result.setdefault("outcome_reason", msg)
    session_scope = session_closed_scope(persisted_raw_result)
    if session_scope:
        persisted_raw_result["task_status"] = (
            ExecutionTaskStatus.SKIPPED.value if session_scope == "all" else ExecutionTaskStatus.PARTIAL.value
        )
        if not persisted_raw_result.get("outcome_reason"):
            persisted_raw_result["outcome_reason"] = persisted_raw_result.get("error") or msg
        persisted_raw_result["outcome"] = "blocked" if session_scope == "all" else "not_reached"
        logger.info("{}", msg)
    else:
        persisted_raw_result.setdefault("task_status", ExecutionTaskStatus.FAILED.value)
        logger.error("{}", msg)
    return await _persist_execute_record(
        account_id=cast("int", account_id),
        execution_id=execution_id,
        raw_input=raw_input,
        raw_result=persisted_raw_result,
        is_success=0,
    )


async def append_success_execute_record(
    account: AccountContext,
    raw_input: dict[str, object],
    result: dict[str, object],
    execution_id: str | None = None,
    execution_kind: ExecutionKind | None = None,
) -> ExecuteRecord:
    """
    添加成功的执行记录.

    Parameters
    ----------
    account : Account
        本次执行对应的账户对象。
    raw_input : dict[str, object]
        需要持久化的输入快照。
    result : dict[str, object]
        需要持久化的结果快照。
    execution_id : str | None, optional
        当前执行 ID。
    execution_kind : ExecutionKind | None, optional
        本次执行类型（``rebalance`` / ``clear_positions``）。写入 ``raw_result`` 供上层区分，
        避免成功清仓被按调仓口径误判为「空跑」。

    Returns
    -------
    ExecuteRecord
        已写入数据库的成功执行记录。
    """
    persisted_raw_result = dict(result)
    persisted_raw_result["task_status"] = ExecutionTaskStatus.SUCCEEDED.value
    if execution_kind is not None:
        persisted_raw_result["execution_kind"] = execution_kind.value
    return await _persist_execute_record(
        account_id=cast("int", account.id),
        execution_id=execution_id,
        raw_input=raw_input,
        raw_result=persisted_raw_result,
        is_success=1,
    )


async def append_terminated_execute_record(
    *,
    account_id: int,
    execution_id: str,
    execution_kind: ExecutionKind,
    reason: str | None,
    mode: str | ExecutionTerminateMode,
    requested_at: str,
    acked_at: str | None,
    finished_at: str,
    cancel_attempted: bool,
    cancel_failed_order_ids: list[str],
    trigger: str = TERMINATION_TRIGGER_OPERATOR,
    forced: bool = False,
    cancel_unconfirmed: bool = False,
    raw_input: dict[str, object] | None = None,
    raw_result: dict[str, object] | None = None,
) -> ExecuteRecord:
    """
    添加 terminated 状态的执行记录.

    Parameters
    ----------
    account_id : int
        本次执行对应的账户 ID。
    execution_id : str
        当前执行 ID。
    execution_kind : ExecutionKind
        当前执行类型。
    reason : str | None
        终止原因。
    mode : str | ExecutionTerminateMode
        终止模式。
    requested_at : str
        发起终止请求的时间。
    acked_at : str | None
        终止请求被执行侧确认的时间。
    finished_at : str
        执行真正结束的时间。
    cancel_attempted : bool
        是否尝试过撤单。
    cancel_failed_order_ids : list[str]
        撤单失败的订单 ID 列表。
    trigger : str, optional
        终止来源：``operator``（人工请求）或 ``timeout``（执行层总超时）。与 ``mode``
        正交——``mode`` 只表示是否撤单，人工终止两种 mode 都可能取，因此不能靠它区分二者。
    raw_input : dict[str, object] | None, optional
        需要持久化的输入快照。
    raw_result : dict[str, object] | None, optional
        需要持久化的结果快照。

    Returns
    -------
    ExecuteRecord
        已写入数据库的 terminated 执行记录。
    """
    persisted_raw_result = dict(raw_result or {})
    persisted_raw_result["outcome"] = "terminated"
    persisted_raw_result["task_status"] = ExecutionTaskStatus.TERMINATED.value
    persisted_raw_result["execution_kind"] = execution_kind.value
    mode_value = mode.value if isinstance(mode, ExecutionTerminateMode) else mode
    persisted_raw_result["termination"] = {
        "reason": reason,
        "mode": mode_value,
        "trigger": trigger,
        "requested_at": requested_at,
        "acked_at": acked_at,
        "finished_at": finished_at,
        "cancel_attempted": cancel_attempted,
        "cancel_failed_order_ids": list(cancel_failed_order_ids),
        "forced": forced,
        "cancel_unconfirmed": cancel_unconfirmed,
    }
    return await _persist_execute_record(
        account_id=account_id,
        execution_id=execution_id,
        raw_input=raw_input,
        raw_result=persisted_raw_result,
        is_success=0,
    )
