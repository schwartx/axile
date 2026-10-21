"""
执行记录持久化辅助函数.

按执行场景统一封装成功、失败与 terminated 三类 ``ExecuteRecord`` 的落库逻辑。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, cast

import loguru
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.session_closed import session_closed_scope
from axile.executor.termination import TERMINATION_TRIGGER_OPERATOR
from axile.server.analysis_history import AnalysisRecord
from axile.server.analysis_observations import write_observation_fact
from axile.server.analysis_source import write_source_facts
from axile.server.business_facts.artifacts import compact_execution_summary
from axile.server.business_facts.assets import ensure_execution_asset_snapshot
from axile.server.business_facts.head import execution_head_values
from axile.server.business_facts.repository import store_execution_facts
from axile.server.core.db import SessionLocal
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.execution.input_persistence import sanitize_execution_input

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _WriteSessionProtocol(Protocol):
    """描述执行记录写入时需要的最小 session 能力."""

    async def __aenter__(self) -> "_WriteSessionProtocol":
        """进入异步会话上下文."""

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """退出异步会话上下文."""

    def add(self, obj: object) -> None:
        """将对象加入当前会话."""

    async def flush(self) -> None:
        """写入待提交对象以取得执行记录 ID。"""

    async def commit(self) -> None:
        """提交当前事务."""

    async def rollback(self) -> None:
        """回滚当前事务."""

    async def scalar(self, statement: object) -> object | None:
        """执行语句并返回首行首列值."""

    async def refresh(self, obj: object) -> None:
        """刷新指定对象."""


type SessionFactory = Callable[[], _WriteSessionProtocol]


async def _persist_execute_record(
    *,
    account_id: int,
    execution_id: str | None,
    raw_input: dict[str, object] | None,
    raw_result: dict[str, object],
    is_success: int,
    session_factory: SessionFactory,
) -> ExecuteRecord:
    """将执行记录写入数据库并返回刷新后的记录对象."""
    persisted_result = dict(raw_result)
    result_input = persisted_result.get("inputs")
    if isinstance(result_input, Mapping):
        persisted_result["inputs"] = sanitize_execution_input(result_input)
    async with session_factory() as session:
        record = ExecuteRecord(
            account_id=account_id,
            execution_id=execution_id,
            is_success=is_success,
            **execution_head_values(persisted_result, is_success=is_success),
        )
        session.add(record)
        try:
            # 业务明细与终态、分析事实同一事务提交，原生读取不会看到半份结果。
            await session.flush()
            canonical_input = await store_execution_facts(
                cast("AsyncSession", session),
                record,
                raw_input=None if raw_input is None else sanitize_execution_input(raw_input),
                raw_result=persisted_result,
            )
            await ensure_execution_asset_snapshot(cast("AsyncSession", session), record)
            await compact_execution_summary(cast("AsyncSession", session), record, result=persisted_result)
            await session.flush()
            analysis_record = AnalysisRecord(
                id=cast("int", record.id),
                account_id=record.account_id,
                execution_id=record.execution_id,
                created_at=record.created_at,
                is_success=record.is_success,
                input=canonical_input,
                result=persisted_result,
            )
            await write_observation_fact(cast("AsyncSession", session), analysis_record)
            await write_source_facts(cast("AsyncSession", session), analysis_record)
            await session.commit()
        except IntegrityError:
            # 同一执行的两条收尾路径（如终止确认与脚本失败）可能并发落库，
            # ``execution_id`` 唯一约束下以首条为准；否则冲突会把终止接口
            # 之类的调用方打穿成 IntegrityError。
            await session.rollback()
            if execution_id is None:
                raise
            existing = cast(
                "ExecuteRecord | None",
                await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == execution_id)),
            )
            if existing is None:
                raise
            if existing.account_id != account_id:
                raise ValueError("执行标识属于其他账户") from None
            loguru.logger.warning(
                "执行记录已存在，保留首条 | execution_id={} record_id={}",
                execution_id,
                existing.id,
            )
            return existing
        await session.refresh(record)
        return record


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
    session_factory: SessionFactory = SessionLocal,
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
    session_factory : SessionFactory, optional
        创建数据库写会话的工厂函数。

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
        session_factory=session_factory,
    )


async def append_success_execute_record(
    account: AccountContext,
    raw_input: dict[str, object],
    result: dict[str, object],
    execution_id: str | None = None,
    execution_kind: ExecutionKind | None = None,
    session_factory: SessionFactory = SessionLocal,
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
    session_factory : SessionFactory, optional
        创建数据库写会话的工厂函数。

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
        session_factory=session_factory,
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
    session_factory: SessionFactory = SessionLocal,
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
    session_factory : SessionFactory, optional
        创建数据库写会话的工厂函数。

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
        session_factory=session_factory,
    )
