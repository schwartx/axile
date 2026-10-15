"""通过账户常驻 worker 执行调仓与清仓。"""

from dataclasses import dataclass
from typing import cast

import loguru

from axile.domain.execution import (
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
)
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.termination import ExecutionTerminated
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.error_details import exception_details, json_error_value
from axile.server.execution.execution_records_output import (
    append_execute_record_from_output,
)
from axile.server.execution.records import append_error_execute_record
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.execution_audit import append_execution_event


@dataclass(frozen=True)
class RebalanceBackendRequest:
    """调仓后端执行所需的完整上下文。"""

    account: AccountContext
    standard_input: UnifiedStandardInput
    standard_input_dict: dict[str, object]
    audit_input: dict[str, object]
    execution_id: str | None
    trigger_source: str
    cleanup: bool
    curr_target: dict[str, float]
    last_target: dict[str, object]
    logger: "loguru.Logger"
    notification_snapshot: dict[str, object] | None = None


@dataclass(frozen=True)
class ClearPositionsBackendRequest:
    """清仓后端执行所需的完整上下文。"""

    account: AccountContext
    resolved_algorithm: dict[str, object]
    empty_kwargs: dict[str, object]
    audit_input: dict[str, object]
    execution_id: str
    trigger_source: str
    logger: "loguru.Logger"
    notification_snapshot: dict[str, object] | None = None


async def run_rebalance_via_backend(*, request: RebalanceBackendRequest) -> ExecuteRecord:
    """
    通过账户常驻 worker 执行一次调仓。

    Parameters
    ----------
    request : RebalanceBackendRequest
        本次调仓执行需要的完整上下文。

    Returns
    -------
    ExecuteRecord
        本次调仓对应的执行记录。
    """
    if request.execution_id is not None:
        from axile.server.execution.intents import promote_intent_to_running

        await promote_intent_to_running(request.execution_id, cast("int", request.account.id))
    return await _run_rebalance_via_worker_process(request)


def _dump_output_result(output: UnifiedStandardOutput | None) -> dict[str, object]:
    """将标准输出转换为可持久化的结果字典。"""
    return cast("dict[str, object]", output.model_dump(mode="json")) if output else {}


async def _append_output_record(
    *,
    account: AccountContext,
    raw_input: dict[str, object],
    output: UnifiedStandardOutput,
    execution_id: str | None,
    execution_kind: ExecutionKind,
) -> tuple[ExecuteRecord, dict[str, object]]:
    """统一序列化输出并写入执行记录。"""
    result = _dump_output_result(output)
    record = await append_execute_record_from_output(
        account=account,
        raw_input=raw_input,
        result=result,
        output=output,
        execution_id=execution_id,
        execution_kind=execution_kind,
    )
    return record, result


async def _run_rebalance_via_worker_process(request: RebalanceBackendRequest) -> ExecuteRecord:
    """通过 worker 进程执行调仓。"""
    try:
        output, normalized_symbol_fields = await get_worker_backend_manager().execute_trade(
            account=request.account,
            standard_input=request.standard_input,
            standard_input_dict=request.standard_input_dict,
            audit_input=request.audit_input,
            execution_id=request.execution_id,
            trigger_source=request.trigger_source,
            cleanup=request.cleanup,
            **({"notification_snapshot": request.notification_snapshot} if request.notification_snapshot else {}),
        )
        raw_input = dict(request.standard_input_dict)
        if normalized_symbol_fields is not None:
            raw_input.update(normalized_symbol_fields)
        record, _ = await _append_output_record(
            account=request.account,
            raw_input=raw_input,
            output=output,
            execution_id=request.execution_id,
            execution_kind=ExecutionKind.REBALANCE,
        )
        request.logger.info(f"交易完成 record_id={record.id} success={record.is_success}")
        return record
    except ExecutionTerminated:
        request.logger.warning(f"交易被终止 trigger={request.trigger_source}")
        raise
    except Exception as exc:
        # 摘要说明执行阶段；原文与实际 traceback 单独留存。
        msg = execution_error_message(exc, "调仓执行")
        await _append_rebalance_error_record(request, msg, exc)
        request.logger.opt(exception=exc).error("{}", msg)
        raise


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


async def _append_rebalance_error_record(request: RebalanceBackendRequest, msg: str, error: Exception) -> None:
    """为调仓失败路径持久化错误执行记录。"""
    await append_error_execute_record(
        account_id=request.account.id,
        raw_input=request.standard_input_dict,
        msg=msg,
        raw_result=exception_details(error),
        execution_id=request.execution_id,
    )


async def run_clear_positions_via_backend(
    *,
    request: ClearPositionsBackendRequest,
) -> ExecuteRecord:
    """
    通过账户常驻 worker 执行一次清仓。

    Parameters
    ----------
    request : ClearPositionsBackendRequest
        本次清仓执行需要的完整上下文。

    Returns
    -------
    ExecuteRecord
        本次清仓对应的执行记录。
    """
    if request.execution_id:
        from axile.server.execution.intents import promote_intent_to_running

        await promote_intent_to_running(request.execution_id, cast("int", request.account.id))
    return await _run_clear_positions_via_worker_process(request)


async def _run_clear_positions_via_worker_process(request: ClearPositionsBackendRequest) -> ExecuteRecord:
    """通过 worker 进程执行清仓。"""
    try:
        output = await get_worker_backend_manager().empty_positions(
            account=request.account,
            empty_kwargs=request.empty_kwargs,
            audit_input=request.audit_input,
            execution_id=request.execution_id,
            **({"notification_snapshot": request.notification_snapshot} if request.notification_snapshot else {}),
        )
        record, _ = await _append_output_record(
            account=request.account,
            raw_input={},
            output=output,
            execution_id=request.execution_id,
            execution_kind=ExecutionKind.CLEAR_POSITIONS,
        )
        request.logger.info(
            f"名称={request.account.name} | 渠道={request.account.trade_channel} | 清仓完成 | "
            f"execution_id={request.execution_id} | record_id={record.id} | success={record.is_success}"
        )
        return record
    except ExecutionTerminated:
        request.logger.warning(
            f"名称={request.account.name} | 渠道={request.account.trade_channel} | 清仓被终止 | "
            f"execution_id={request.execution_id} | trigger={request.trigger_source}"
        )
        raise
    except Exception as exc:
        await _record_clear_positions_failure(request, error=exc)
        raise ValueError(execution_error_message(exc, "清仓执行")) from exc


async def _record_clear_positions_failure(
    request: ClearPositionsBackendRequest,
    error: Exception,
) -> None:
    """记录清仓失败路径的审计事件与错误执行记录。"""
    msg = execution_error_message(error, "清仓执行")
    await append_execution_event(
        execution_id=request.execution_id,
        account_id=cast("int", request.account.id),
        channel=request.account.trade_channel,
        algorithm=cast("str", request.resolved_algorithm.get("method", "SINGLE-MAKER")),
        event_type=ExecutionEventType.EXECUTION_FAILED,
        status=ExecutionEventStatus.ERROR,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_FAILED",
        # worker 尚未准备成功时，由主进程保留失败证据。
        seq=0,
        # 阶段说明与技术原文一起进入既有事件。
        details=_failure_details(error, msg, request.trigger_source),
    )
    await _append_clear_positions_error_record(request, msg, error)
    request.logger.opt(exception=error).error("{}", msg)


async def _append_clear_positions_error_record(
    request: ClearPositionsBackendRequest,
    msg: str,
    error: Exception,
) -> None:
    """为清仓失败路径持久化错误执行记录。"""
    await append_error_execute_record(
        account_id=request.account.id,
        msg=msg,
        raw_result=exception_details(error),
        execution_id=request.execution_id,
    )
