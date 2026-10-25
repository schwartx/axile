"""
执行记录持久化辅助函数.

按执行场景统一封装成功、失败与 terminated 三类 ``dict[str, Any]`` 的落库逻辑。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import loguru

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.executor.models.execution_result import ExecutionStatus, outcome_from_status
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.session_closed import session_closed_scope
from axile.executor.termination import TERMINATION_TRIGGER_OPERATOR
from axile.server._native import _core
from axile.server.business_facts.summary_refs import raise_business_fact_error
from axile.server.db.models.account import AccountContext
from axile.server.db.models.base import now_str
from axile.server.execution.input_persistence import sanitize_execution_input
from axile.server.native_store import native_operation


async def _persist_execute_record(
    *,
    account_id: int,
    execution_id: str | None,
    raw_input: dict[str, object] | None,
    raw_result: dict[str, object],
    is_success: int,
) -> dict[str, Any]:
    """一次原生事务提交终态、输入、结果事实、资产历史与完整摘要快照。"""
    try:
        stored = await native_operation(
            "persist_execution_state",
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
        raise_business_fact_error(error)
    from axile.server.execution.notification_outbox import wake_notifications

    wake_notifications(stored["account_id"])
    return stored


async def append_error_execute_record(
    account_id: int | None,
    raw_input: dict[str, object] | None = None,
    raw_result: dict[str, object] | None = None,
    msg: str = "",
    execution_id: str | None = None,
    logger: "loguru.Logger" = loguru.logger,
    *,
    error: Exception | None = None,
) -> dict[str, Any]:
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
    error : Exception | None, optional
        本次失败的异常；同一执行沿异常链复用已经提交的记录。

    Returns
    -------
    dict[str, Any]
        已写入数据库的失败执行记录。
    """
    if error is not None and execution_id is not None:
        existing = _recorded_error(error, account_id, execution_id)
        if existing is not None:
            return existing
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
    record = await _persist_execute_record(
        account_id=cast("int", account_id),
        execution_id=execution_id,
        raw_input=raw_input,
        raw_result=persisted_raw_result,
        is_success=0,
    )
    if error is not None and execution_id is not None:
        error.__dict__["_axile_execution_record"] = record
    return record


def _recorded_error(error: BaseException | None, account_id: int | None, execution_id: str) -> dict[str, Any] | None:
    """只复用同账户、同执行的已提交记录，显式异常链允许出现环。"""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        record = error.__dict__.get("_axile_execution_record")
        if (
            isinstance(record, dict)
            and record.get("account_id") == account_id
            and record.get("execution_id") == execution_id
        ):
            return record
        error = error.__cause__
    return None


async def append_execute_record_from_output(
    *,
    account: AccountContext,
    raw_input: dict[str, object],
    output: UnifiedStandardOutput,
    execution_id: str | None,
    execution_kind: ExecutionKind,
) -> dict[str, Any]:
    """
    根据统一输出结果持久化执行记录.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    raw_input : dict[str, object]
        原始输入的字典快照。
    output : UnifiedStandardOutput
        执行器返回的统一输出对象。
    execution_id : str | None
        当前 execution 标识。
    execution_kind : ExecutionKind
        本次执行类型（``rebalance`` / ``clear_positions``），写入成功记录供上层区分。

    Returns
    -------
    dict[str, Any]
        持久化后的执行记录。
    """
    result = output.model_dump(mode="json")
    result["execution_kind"] = execution_kind.value
    if isinstance(result.get("inputs"), dict):
        result["inputs"] = sanitize_execution_input(result["inputs"])
    # 落库边界按当前 status 重新派生展示结论，防止绕过校验器的中途改写造成两套结论分离。
    result["outcome"] = outcome_from_status(output.status).value
    result["outcome_reason"] = output.outcome_reason
    if output.success:
        return await append_success_execute_record(
            account,
            raw_input,
            result,
            execution_id,
            execution_kind=execution_kind,
        )

    return await append_error_execute_record(
        account_id=account.id,
        raw_input=raw_input,
        raw_result=result,
        msg=output.get_error_message() or "执行未成功完成",
        execution_id=execution_id,
    )


async def append_success_execute_record(
    account: AccountContext,
    raw_input: dict[str, object],
    result: dict[str, object],
    execution_id: str | None = None,
    execution_kind: ExecutionKind | None = None,
) -> dict[str, Any]:
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
    dict[str, Any]
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
) -> dict[str, Any]:
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
    dict[str, Any]
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
