"""按执行后端策略执行调仓与清仓。"""

import asyncio
from dataclasses import dataclass
from typing import cast

import loguru

from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
)
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.termination import ExecutionTerminated
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.error_details import exception_details, json_error_value
from axile.server.execution import lifecycle as execution_lifecycle
from axile.server.execution.dispatch import ExecutionBackendKind, resolve_execution_backend_kind
from axile.server.execution.execution_records_output import (
    append_execute_record_from_output,
    resolve_completion_event_status,
)
from axile.server.execution.records import append_error_execute_record
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.execution_audit import append_execution_artifact, append_execution_event


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
    按后端分发策略执行一次调仓。

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
    backend_kind = resolve_execution_backend_kind(request.account.trade_channel)
    if backend_kind == ExecutionBackendKind.PROCESS:
        return await _run_rebalance_via_worker_process(request)
    return await _run_rebalance_inline(request)


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
    # worker / inline 两条路径都先收敛到同一份 result 字典，再落统一执行记录。
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
        msg = "调仓执行失败，具体原因未确认"
        await _append_rebalance_error_record(request, msg, exc)
        request.logger.opt(exception=exc).error("{}", msg)
        raise


async def _capture_before_account_snapshot(
    executor: AbstractExecutor,
    logger: "loguru.Logger",
) -> dict[str, object] | None:
    """执行前拉取一次账户快照作为对账基线.

    Parameters
    ----------
    executor : AbstractExecutor
        已就绪的执行器实例。
    logger : loguru.Logger
        当前执行绑定的日志器。

    Returns
    -------
    dict[str, object] | None
        账户快照的 JSON 化内容；读取失败时返回 ``None``（由下游按 ``unavailable`` 处理），
        不影响执行主线。
    """
    try:
        assets = await asyncio.to_thread(executor.get_account_assets)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"执行前账户快照读取失败，将标记为 unavailable: {exc}")
        return None
    return cast("dict[str, object]", assets.model_dump(mode="json"))


async def _append_before_snapshot_artifact(
    execution_id: str,
    before_account_assets: dict[str, object] | None,
) -> None:
    """写入执行前账户快照附件，供对账取前基线与脊柱渲染.

    Parameters
    ----------
    execution_id : str
        当前执行标识。
    before_account_assets : dict[str, object] | None
        执行前账户快照的 JSON 化内容；``None`` 表示读取不可用，来源标记为 ``unavailable``。
    """
    before_source = (
        cast("str", before_account_assets.get("source", "real")) if before_account_assets is not None else "unavailable"
    )
    await append_execution_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE,
        content={
            "account_assets": before_account_assets or {},
            "source": before_source,
        },
    )


async def _run_rebalance_inline(request: RebalanceBackendRequest) -> ExecuteRecord:
    """在当前进程内执行调仓。"""
    executor: AbstractExecutor | None = None
    try:
        executor = cast(
            "AbstractExecutor",
            await execution_lifecycle.prepare_executor_runtime(
                request.account,
                execution_id=request.execution_id,
                audit_context=cast("dict[str, object]", request.standard_input.extra["audit"]),
            ),
        )
        before_account_assets = await _capture_before_account_snapshot(executor, request.logger)
        await _append_rebalance_start_audit(request, executor, before_account_assets)
        request.logger.info(f"开始交易 trigger={request.trigger_source}")
        output = cast(
            "UnifiedStandardOutput",
            await asyncio.to_thread(
                lambda: executor.execute(
                    request.standard_input,
                    cleanup=request.cleanup,
                    retain_runtime=True,
                    **(
                        {"notification_snapshot": request.notification_snapshot}
                        if request.notification_snapshot
                        else {}
                    ),
                )
            ),
        )
        record = await _persist_rebalance_completion(request, executor, output, before_account_assets)
        request.logger.info(f"交易完成 record_id={record.id} success={record.is_success}")
        return record
    except ExecutionTerminated:
        request.logger.warning(f"交易被终止 trigger={request.trigger_source}")
        raise
    except Exception as exc:
        await _record_rebalance_inline_failure(request, executor, exc)
        raise
    finally:
        await execution_lifecycle.cleanup_executor_runtime(executor)


async def _append_rebalance_start_audit(
    request: RebalanceBackendRequest,
    executor: AbstractExecutor,
    before_account_assets: dict[str, object] | None,
) -> None:
    """写入调仓开始前的审计事件与输入快照（含执行前账户快照）。"""
    if request.execution_id is None:
        return

    execution_id = request.execution_id
    algorithm = request.account.algorithm.get("method", "SINGLE-MAKER")
    await append_execution_event(
        execution_id=execution_id,
        account_id=cast("int", request.account.id),
        channel=request.account.trade_channel,
        algorithm=algorithm,
        event_type=ExecutionEventType.EXECUTION_STARTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_STARTED",
        seq=executor.next_audit_seq(),
        details={"debug": {"trigger_source": request.trigger_source, "execution_kind": "rebalance"}},
    )
    await append_execution_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.STANDARD_INPUT,
        content={"input": request.audit_input},
    )
    await append_execution_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
        schema_version=2,
        content={
            "curr_target": request.curr_target,
            "strategy_target": request.audit_input.get("strategy_target", {}),
            "account_target": request.curr_target,
            "sizing_context": request.audit_input.get("sizing_context", {}),
            "last_target": request.last_target,
        },
    )
    await append_execution_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.TRADE_RULES_SNAPSHOT,
        content={"trade_rules": request.account.trade_rules},
    )
    await _append_before_snapshot_artifact(execution_id, before_account_assets)
    await append_execution_event(
        execution_id=execution_id,
        account_id=cast("int", request.account.id),
        channel=request.account.trade_channel,
        algorithm=algorithm,
        event_type=ExecutionEventType.INPUT_SNAPSHOTTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.INPUT,
        reason_code="COMMON.INPUT_SNAPSHOTTED",
        seq=executor.next_audit_seq(),
        details={"debug": {"symbol_count": len(request.curr_target)}},
    )
    await append_execution_event(
        execution_id=execution_id,
        account_id=cast("int", request.account.id),
        channel=request.account.trade_channel,
        algorithm=algorithm,
        event_type=ExecutionEventType.TARGET_COMPUTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.INPUT,
        reason_code="COMMON.TARGET_COMPUTED",
        seq=executor.next_audit_seq(),
        details={"decision": {"curr_target": request.curr_target}},
    )


async def _persist_rebalance_completion(
    request: RebalanceBackendRequest,
    executor: AbstractExecutor,
    output: UnifiedStandardOutput,
    before_account_assets: dict[str, object] | None,
) -> ExecuteRecord:
    """持久化调仓结果，并补写完成事件与逐只对账。"""
    record, result = await _append_output_record(
        account=request.account,
        raw_input=request.standard_input_dict,
        output=output,
        execution_id=request.execution_id,
        execution_kind=ExecutionKind.REBALANCE,
    )
    if request.execution_id:
        # 先保住用户可查询的执行记录，再补 execution 产物与完成事件。
        await execution_lifecycle.append_execution_result_artifacts(request.execution_id, result, before_account_assets)

    if request.execution_id:
        await append_execution_event(
            execution_id=request.execution_id,
            account_id=cast("int", request.account.id),
            channel=request.account.trade_channel,
            algorithm=request.account.algorithm.get("method", "SINGLE-MAKER"),
            event_type=ExecutionEventType.EXECUTION_COMPLETED,
            status=resolve_completion_event_status(output.status),
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_COMPLETED",
            seq=executor.next_audit_seq(),
            details=execution_lifecycle.build_rebalance_completed_details(
                record=record,
                output_success=output.success,
                output_status=output.status.value,
                result=result,
            ),
        )
    return record


async def _record_rebalance_inline_failure(
    request: RebalanceBackendRequest,
    executor: AbstractExecutor | None,
    error: Exception,
) -> None:
    """记录调仓 inline 路径的失败审计与错误记录。"""
    msg = execution_error_message(error, "调仓执行")
    if request.execution_id:
        # inline 路径失败时 runtime 仍在当前进程内，可直接沿用 executor 的 audit seq 记失败事件。
        await append_execution_event(
            execution_id=request.execution_id,
            account_id=cast("int", request.account.id),
            channel=request.account.trade_channel,
            algorithm=request.account.algorithm.get("method", "SINGLE-MAKER"),
            event_type=ExecutionEventType.EXECUTION_FAILED,
            status=ExecutionEventStatus.ERROR,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_FAILED",
            seq=executor.next_audit_seq() if executor is not None else 0,
            # 阶段说明与技术原文一起进入既有事件。
            details=_failure_details(error, msg, request.trigger_source),
        )
    await _append_rebalance_error_record(request, msg, error)
    request.logger.opt(exception=error).error("{}", msg)


def _failure_details(error: Exception, message: str, trigger_source: str) -> dict[str, object]:
    """保留渠道结构化分类和恢复时间；诊断原文完整进入事件。"""
    details: dict[str, object] = {
        **exception_details(error),
        "error": message,
        "debug": {"trigger_source": trigger_source},
    }
    diagnostics = getattr(error, "diagnostics", None)
    if isinstance(diagnostics, list):
        details["error_type"] = type(error).__name__
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
    按后端分发策略执行一次清仓。

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
    backend_kind = resolve_execution_backend_kind(request.account.trade_channel)
    if backend_kind == ExecutionBackendKind.PROCESS:
        return await _run_clear_positions_via_worker_process(request)
    return await _run_clear_positions_inline(request)


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


async def _run_clear_positions_inline(request: ClearPositionsBackendRequest) -> ExecuteRecord:
    """在当前进程内执行清仓。"""
    executor: AbstractExecutor | None = None
    try:
        executor = cast(
            "AbstractExecutor",
            await execution_lifecycle.prepare_executor_runtime(
                request.account,
                execution_id=request.execution_id,
                audit_context=cast(
                    "dict[str, object]", cast("dict[str, object]", request.empty_kwargs["extra"])["audit"]
                ),
            ),
        )
        before_account_assets = await _capture_before_account_snapshot(executor, request.logger)
        await _append_clear_positions_start_audit(request, executor, before_account_assets)
        request.logger.info(
            f"名称={request.account.name} | 渠道={request.account.trade_channel} | 开始清仓 | "
            f"execution_id={request.execution_id} | trigger={request.trigger_source}"
        )
        output = cast(
            "UnifiedStandardOutput",
            await asyncio.to_thread(
                lambda: executor.empty_positions(
                    cleanup=True,
                    retain_runtime=True,
                    **(
                        {"notification_snapshot": request.notification_snapshot}
                        if request.notification_snapshot
                        else {}
                    ),
                    **request.empty_kwargs,
                )
            ),
        )
        record = await _persist_clear_positions_completion(request, executor, output, before_account_assets)
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
        await _record_clear_positions_failure(request, error=exc, executor=executor)
        raise ValueError(execution_error_message(exc, "清仓执行")) from exc
    finally:
        await execution_lifecycle.cleanup_executor_runtime(executor)


async def _append_clear_positions_start_audit(
    request: ClearPositionsBackendRequest,
    executor: AbstractExecutor,
    before_account_assets: dict[str, object] | None,
) -> None:
    """写入清仓开始前的 execution_started 事件与执行前账户快照。"""
    await append_execution_event(
        execution_id=request.execution_id,
        account_id=cast("int", request.account.id),
        channel=request.account.trade_channel,
        algorithm=cast("str", request.resolved_algorithm.get("method", "SINGLE-MAKER")),
        event_type=ExecutionEventType.EXECUTION_STARTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_STARTED",
        seq=executor.next_audit_seq(),
        details={"debug": {"trigger_source": request.trigger_source, "execution_kind": "clear_positions"}},
    )
    await _append_before_snapshot_artifact(request.execution_id, before_account_assets)


async def _persist_clear_positions_completion(
    request: ClearPositionsBackendRequest,
    executor: AbstractExecutor,
    output: UnifiedStandardOutput,
    before_account_assets: dict[str, object] | None,
) -> ExecuteRecord:
    """持久化清仓结果，并补写完成事件与产物。"""
    record, result = await _append_output_record(
        account=request.account,
        raw_input={},
        output=output,
        execution_id=request.execution_id,
        execution_kind=ExecutionKind.CLEAR_POSITIONS,
    )
    # 清仓路径的标准输入只在完成后落审计，因为输入来自 empty_kwargs 的即时构造结果。
    await append_execution_artifact(
        execution_id=request.execution_id,
        artifact_type=ExecutionArtifactType.STANDARD_INPUT,
        content={"input": request.audit_input},
    )
    await execution_lifecycle.append_execution_result_artifacts(request.execution_id, result, before_account_assets)
    await append_execution_event(
        execution_id=request.execution_id,
        account_id=cast("int", request.account.id),
        channel=request.account.trade_channel,
        algorithm=cast("str", request.resolved_algorithm.get("method", "SINGLE-MAKER")),
        event_type=ExecutionEventType.EXECUTION_COMPLETED,
        status=resolve_completion_event_status(output.status),
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_COMPLETED",
        seq=executor.next_audit_seq(),
        details=execution_lifecycle.build_clear_positions_completed_details(
            record=record,
            output_success=output.success,
            output_status=output.status.value,
        ),
    )
    return record


async def _record_clear_positions_failure(
    request: ClearPositionsBackendRequest,
    error: Exception,
    executor: AbstractExecutor | None = None,
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
        # worker 侧有可能在 executor 尚未完成 prepare 前就失败，此时退回到 seq=0。
        seq=0 if executor is None else executor.next_audit_seq(),
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
