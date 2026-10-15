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
from axile.server.db.models.account import AccountContext
from axile.server.execution.environment import execution_environment
from axile.server.execution.execution_records_output import resolve_completion_event_status
from axile.server.execution.execution_summaries import (
    build_execution_outcome_details,
    build_execution_summary_from_symbol_results,
    build_symbol_reconciliation,
    count_orders_from_symbol_results,
)
from axile.server.execution.worker_backend.protocol import WorkerBackendErrorPayload
from axile.server.execution_audit import (
    append_execution_artifact_sync,
    append_execution_event_sync,
)


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


def _append_before_snapshot_artifact_sync(
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
        cast(str, before_account_assets.get("source", "real")) if before_account_assets is not None else "unavailable"
    )
    append_execution_artifact_sync(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE,
        content={
            "account_assets": before_account_assets or {},
            "source": before_source,
        },
    )


def _append_environment(execution_id: str, executor: object, channel: str, algorithm: str) -> None:
    """在真实执行前保存环境；元数据采集失败不阻断交易。"""
    from loguru import logger

    try:
        append_execution_artifact_sync(
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

    _append_environment(execution_id, executor, account.trade_channel.value, algorithm_name)
    append_execution_event_sync(
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
    append_execution_artifact_sync(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.STANDARD_INPUT,
        content={"input": audit_input},
    )
    append_execution_artifact_sync(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
        schema_version=2,
        content={
            "curr_target": standard_input.curr_target,
            "strategy_target": audit_input.get("strategy_target", {}),
            "account_target": standard_input.curr_target,
            "sizing_context": audit_input.get("sizing_context", {}),
            "last_target": standard_input.last_target,
        },
    )
    append_execution_artifact_sync(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.TRADE_RULES_SNAPSHOT,
        content={"trade_rules": standard_input.trade_rules},
    )
    _append_before_snapshot_artifact_sync(execution_id, before_account_assets)
    append_execution_event_sync(
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
    append_execution_event_sync(
        execution_id=execution_id,
        account_id=cast(int, account.id),
        channel=account.trade_channel,
        algorithm=algorithm_name,
        event_type=ExecutionEventType.TARGET_COMPUTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.INPUT,
        reason_code="COMMON.TARGET_COMPUTED",
        seq=_event_seq(executor),
        details={"decision": {"curr_target": standard_input.curr_target}},
    )


def _append_empty_positions_pre_execute_audit(
    *,
    account: AccountContext,
    execution_id: str | None,
    algorithm_name: str,
    executor: object,
    before_account_assets: dict[str, object] | None = None,
) -> None:
    """补写清仓命令的开始审计事件与执行前账户快照."""
    if not execution_id:
        return

    _append_environment(execution_id, executor, account.trade_channel.value, algorithm_name)
    append_execution_event_sync(
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
    _append_before_snapshot_artifact_sync(execution_id, before_account_assets)


def _append_success_audit(
    *,
    account: AccountContext,
    execution_id: str | None,
    algorithm_name: str,
    output: UnifiedStandardOutput,
    result: dict[str, object],
    executor: object,
    include_trade_rule_snapshots: bool,
    audit_input: dict[str, object] | None = None,
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

    # 清仓路径不会像调仓路径那样提前写完整输入快照，所以这里补回标准输入附件。
    if audit_input is not None and not include_trade_rule_snapshots:
        append_execution_artifact_sync(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.STANDARD_INPUT,
            content={"input": audit_input},
        )

    append_execution_artifact_sync(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT,
        content={
            "account_assets": result.get("account_assets", {}),
            "orders_count": count_orders_from_symbol_results(result),
        },
    )
    append_execution_artifact_sync(
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
    append_execution_event_sync(
        execution_id=execution_id,
        account_id=cast(int, account.id),
        channel=account.trade_channel,
        algorithm=algorithm_name,
        event_type=ExecutionEventType.EXECUTION_COMPLETED,
        status=resolve_completion_event_status(output.status),
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
