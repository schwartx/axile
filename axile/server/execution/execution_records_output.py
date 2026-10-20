"""执行输出落库与审计输入辅助函数."""

from axile.domain.execution import ExecutionEventStatus, ExecutionKind
from axile.executor.models.execution_result import ExecutionStatus, outcome_from_status
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.execution.input_persistence import sanitize_execution_input
from axile.server.execution.records import append_error_execute_record, append_success_execute_record


def sanitize_standard_input_for_audit(raw_input: UnifiedStandardInput | dict[str, object]) -> dict[str, object]:
    """
    在审计持久化前移除标准输入中的敏感字段.

    Parameters
    ----------
    raw_input : UnifiedStandardInput | dict[str, object]
        原始标准输入对象或其字典快照。

    Returns
    -------
    dict[str, object]
        已脱敏的审计输入内容。
    """
    payload = raw_input.to_dict() if isinstance(raw_input, UnifiedStandardInput) else raw_input
    return sanitize_execution_input(payload)


def resolve_completion_event_status(output_status: ExecutionStatus) -> ExecutionEventStatus:
    """
    将执行输出状态映射为 execution_completed 事件状态.

    Parameters
    ----------
    output_status : ExecutionStatus
        执行器返回的状态值。

    Returns
    -------
    ExecutionEventStatus
        可写入 execution event 的事件状态。
    """
    if output_status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.NOOP}:
        return ExecutionEventStatus.SUCCESS
    if output_status in {ExecutionStatus.BLOCKED, ExecutionStatus.PARTIAL}:
        return ExecutionEventStatus.WARNING
    return ExecutionEventStatus.ERROR


async def append_execute_record_from_output(
    *,
    account: AccountContext,
    raw_input: dict[str, object],
    result: dict[str, object],
    output: UnifiedStandardOutput,
    execution_id: str | None,
    execution_kind: ExecutionKind,
) -> ExecuteRecord:
    """
    根据统一输出结果持久化执行记录.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    raw_input : dict[str, object]
        原始输入的字典快照。
    result : dict[str, object]
        执行结果的 JSON 化内容。
    output : UnifiedStandardOutput
        执行器返回的统一输出对象。
    execution_id : str | None
        当前 execution 标识。
    execution_kind : ExecutionKind
        本次执行类型（``rebalance`` / ``clear_positions``），写入成功记录供上层区分。

    Returns
    -------
    ExecuteRecord
        持久化后的执行记录。
    """
    result["execution_kind"] = execution_kind.value
    if isinstance(result.get("inputs"), dict):
        result["inputs"] = sanitize_standard_input_for_audit(result["inputs"])
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
