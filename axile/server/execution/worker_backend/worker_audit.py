"""补写 worker 执行前后所需的审计事件与附件.

worker 进程会在真实执行前后把关键状态转为持久化审计记录，确保
主进程即使只收到最终响应，也能回放这次执行的输入、摘要和失败
原因。
"""

from __future__ import annotations

from typing import cast

from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.session_closed import session_closed_scope
from axile.server.business_facts.artifacts import make_artifact_reference
from axile.server.business_facts.assets import input_asset_fact_id
from axile.server.db.models.account import AccountContext
from axile.server.execution.audit_batch import (
    POST_EXECUTE_AUDIT_LIMIT,
    PRE_EXECUTE_AUDIT_LIMIT,
    ExecutionAuditBatch,
    append_execution_audit_batch_sync,
)
from axile.server.execution.environment import execution_environment
from axile.server.execution.execution_records_output import resolve_completion_event_status
from axile.server.execution.execution_summaries import (
    build_execution_outcome_details,
    build_execution_summary_from_symbol_results,
    build_symbol_reconciliation,
    count_orders_from_symbol_results,
)
from axile.server.execution.input_persistence import (
    persist_post_execute_assets_sync,
    persist_pre_execute_input_sync,
)
from axile.server.execution.worker_backend.protocol import WorkerBackendErrorPayload
from axile.server.execution_audit import append_execution_event_sync, build_execution_event


def _event_seq(executor: object) -> int:
    """读取执行器提供的下一条审计序号.

    Parameters
    ----------
    executor : object
        当前请求绑定的执行器实例。

    Returns
    -------
    int
        执行器侧维护的下一条审计序号；未实现时退化为 ``0``。
    """
    next_audit_seq = getattr(executor, "next_audit_seq", None)
    if callable(next_audit_seq):
        return cast(int, next_audit_seq())
    return 0


def _add_before_snapshot_artifact(
    batch: ExecutionAuditBatch,
    execution_id: str,
    input_id: str,
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
        cast(str, before_account_assets.get("source", "real")) if before_account_assets is not None else "unavailable"
    )
    batch.add_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE,
        schema_version=3 if before_account_assets is not None else 1,
        content=(
            {
                **make_artifact_reference(
                    view="assets_before", input_id=input_id, asset_id=input_asset_fact_id(input_id)
                ),
                "source": before_source,
            }
            if before_account_assets is not None
            else {"account_assets": {}, "source": "unavailable"}
        ),
    )


def _add_environment(
    batch: ExecutionAuditBatch, execution_id: str, executor: object, channel: str, algorithm: str
) -> None:
    """在写事务外采集环境并入批；元数据采集失败不阻断交易。"""
    from loguru import logger

    try:
        batch.add_artifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT,
            content=execution_environment(executor, channel, algorithm),
        )
    except Exception:
        logger.exception("执行环境快照采集失败")


def _append_trade_pre_execute_audit(
    *,
    account: AccountContext,
    execution_id: str | None,
    algorithm_name: str,
    trigger_source: str,
    audit_input: dict[str, object],
    standard_input: UnifiedStandardInput,
    executor: object,
    before_account_assets: dict[str, object] | None = None,
) -> None:
    """补写调仓命令的开始审计与输入快照（含执行前账户快照）.

    调仓路径会在真实下单前先持久化输入、目标仓位和规则快照，
    这样即使执行中途异常，也能复原这次命令原本打算做什么。
    """
    if not execution_id:
        return

    input_id = persist_pre_execute_input_sync(
        account_id=cast(int, account.id),
        execution_id=execution_id,
        payload=audit_input,
        before_account_assets=before_account_assets,
    )
    audit_id = input_id
    batch = ExecutionAuditBatch(max_entries=PRE_EXECUTE_AUDIT_LIMIT)
    _add_environment(batch, execution_id, executor, account.trade_channel.value, algorithm_name)
    batch.add_event(
        build_execution_event(
            execution_id=execution_id,
            account_id=cast(int, account.id),
            channel=account.trade_channel,
            algorithm=algorithm_name,
            event_type=ExecutionEventType.EXECUTION_STARTED,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_STARTED",
            seq=_event_seq(executor),
            details={
                "debug": {
                    "trigger_source": trigger_source,
                    "execution_kind": "rebalance",
                }
            },
        )
    )
    batch.add_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.STANDARD_INPUT,
        schema_version=3,
        content=make_artifact_reference(view="input", input_id=input_id),
    )
    batch.add_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
        schema_version=3,
        content=make_artifact_reference(view="targets", input_id=audit_id),
    )
    batch.add_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.TRADE_RULES_SNAPSHOT,
        schema_version=3,
        content=make_artifact_reference(view="rules", input_id=audit_id),
    )
    _add_before_snapshot_artifact(batch, execution_id, input_id, before_account_assets)
    batch.add_event(
        build_execution_event(
            execution_id=execution_id,
            account_id=cast(int, account.id),
            channel=account.trade_channel,
            algorithm=algorithm_name,
            event_type=ExecutionEventType.INPUT_SNAPSHOTTED,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.INPUT,
            reason_code="COMMON.INPUT_SNAPSHOTTED",
            seq=_event_seq(executor),
            details={
                "debug": {
                    "symbol_count": len(standard_input.curr_target),
                }
            },
        )
    )
    batch.add_event(
        build_execution_event(
            execution_id=execution_id,
            account_id=cast(int, account.id),
            channel=account.trade_channel,
            algorithm=algorithm_name,
            event_type=ExecutionEventType.TARGET_COMPUTED,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.INPUT,
            reason_code="COMMON.TARGET_COMPUTED",
            seq=_event_seq(executor),
            details={
                "decision": {
                    **make_artifact_reference(view="targets", input_id=audit_id, select_fields=["curr_target"]),
                }
            },
        )
    )
    append_execution_audit_batch_sync(batch)


def _append_empty_positions_pre_execute_audit(
    *,
    account: AccountContext,
    execution_id: str | None,
    algorithm_name: str,
    executor: object,
    audit_input: dict[str, object],
    before_account_assets: dict[str, object] | None = None,
) -> None:
    """补写清仓命令的开始审计事件与执行前账户快照."""
    if not execution_id:
        return

    input_id = persist_pre_execute_input_sync(
        account_id=cast(int, account.id),
        execution_id=execution_id,
        payload=audit_input,
        before_account_assets=before_account_assets,
    )
    batch = ExecutionAuditBatch(max_entries=PRE_EXECUTE_AUDIT_LIMIT)
    batch.add_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.STANDARD_INPUT,
        schema_version=3,
        content=make_artifact_reference(view="input", input_id=input_id),
    )
    _add_environment(batch, execution_id, executor, account.trade_channel.value, algorithm_name)
    batch.add_event(
        build_execution_event(
            execution_id=execution_id,
            account_id=cast(int, account.id),
            channel=account.trade_channel,
            algorithm=algorithm_name,
            event_type=ExecutionEventType.EXECUTION_STARTED,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_STARTED",
            seq=_event_seq(executor),
            details={
                "debug": {
                    "trigger_source": "empty_positions",
                    "execution_kind": "clear_positions",
                }
            },
        )
    )
    _add_before_snapshot_artifact(batch, execution_id, input_id, before_account_assets)
    append_execution_audit_batch_sync(batch)


def _append_success_audit(
    *,
    account: AccountContext,
    execution_id: str | None,
    algorithm_name: str,
    output: UnifiedStandardOutput,
    result: dict[str, object],
    executor: object,
    before_account_assets: dict[str, object] | None = None,
) -> None:
    """补写成功结果相关的摘要、账户快照、逐只对账与完成事件.

    Notes
    -----
    这里优先基于最终 `result` 字典生成摘要，而不是重新从执行器
    对象读取状态，目的是保证持久化审计与返回给主进程的结果视图
    一致。
    """
    if not execution_id:
        return

    after_reference = persist_post_execute_assets_sync(
        account_id=cast(int, account.id), execution_id=execution_id, payload=result
    )
    batch = ExecutionAuditBatch(max_entries=POST_EXECUTE_AUDIT_LIMIT)
    if after_reference is not None:
        input_id, asset_id = after_reference
        batch.add_artifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT,
            schema_version=3,
            content={
                **make_artifact_reference(view="assets_after", input_id=input_id, asset_id=asset_id),
                "orders_count": count_orders_from_symbol_results(result),
            },
        )
    batch.add_artifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
        schema_version=2,
        content={
            **build_execution_outcome_details(result),
            "summary": build_execution_summary_from_symbol_results(result),
            "success": result.get("success", True),
            "execution_time": result.get("execution_time"),
            "reconciliation": build_symbol_reconciliation(result, before_account_assets),
        },
    )
    batch.add_event(
        build_execution_event(
            execution_id=execution_id,
            account_id=cast(int, account.id),
            channel=account.trade_channel,
            algorithm=algorithm_name,
            event_type=ExecutionEventType.EXECUTION_COMPLETED,
            status=(
                ExecutionEventStatus.INFO
                if session_closed_scope(result) == "all"
                else resolve_completion_event_status(output.status)
            ),
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_COMPLETED",
            seq=_event_seq(executor),
            details={
                "debug": {
                    "success": output.success,
                    "execution_status": output.status.value,
                },
                "order": {
                    "orders_count": count_orders_from_symbol_results(result),
                },
            },
        )
    )
    append_execution_audit_batch_sync(batch)


def _append_failed_audit(
    *,
    account: AccountContext,
    execution_id: str | None,
    algorithm_name: str,
    executor: object | None,
    error: WorkerBackendErrorPayload,
    trigger_source: str,
) -> None:
    """补写 worker 命令失败事件.

    当执行器尚未成功构造时，失败事件会退化为序号 ``0``，保证
    请求级失败仍然可被审计而不是直接丢失。
    """
    if not execution_id:
        return

    append_execution_event_sync(
        execution_id=execution_id,
        account_id=cast(int, account.id),
        channel=account.trade_channel,
        algorithm=algorithm_name,
        event_type=ExecutionEventType.EXECUTION_FAILED,
        status=ExecutionEventStatus.ERROR,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_FAILED",
        seq=0 if executor is None else _event_seq(executor),
        details={
            "error": error.message,
            "error_type": error.exception_type or error.type,
            "diagnostics": error.diagnostics,
            "retry_after": error.retry_after,
            "reason_code": error.reason_code,
            "error_id": error.error_id,
            "technical_detail": getattr(error, "technical_detail", None),
            "traceback": getattr(error, "traceback", None),
            "debug": {
                "error": {
                    "type": error.type,
                    "message": error.message,
                    "retryable": error.retryable,
                },
                "trigger_source": trigger_source,
            },
        },
    )
