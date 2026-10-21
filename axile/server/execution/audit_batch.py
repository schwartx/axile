"""固定生命周期阶段的显式、有界审计批次；不收集执行中的 runtime 事件。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from axile.domain.execution import ExecutionArtifactType, ExecutionEventType
from axile.server.core.db import SessionLocal
from axile.server.db.models import ExecutionArtifact, ExecutionEvent, now_str
from axile.server.execution_audit import (
    _is_duplicate_integrity_error,
    _notify_live_hub,
    _run_coroutine_sync,
)

PRE_EXECUTE_AUDIT_LIMIT = 8
POST_EXECUTE_AUDIT_LIMIT = 3


class ExecutionAuditBatch:
    """保存一个固定阶段的审计行引用；入批不表示已经持久化。"""

    def __init__(self, *, max_entries: int) -> None:
        """设置批次硬上限，任何阶段最多保留八条记录。"""
        if not 1 <= max_entries <= PRE_EXECUTE_AUDIT_LIMIT:
            raise ValueError("审计批次上限必须在 1 到 8 之间")
        self.max_entries = max_entries
        self._entries: list[ExecutionEvent | ExecutionArtifact] = []

    def __len__(self) -> int:
        """返回已入批的行数。"""
        return len(self._entries)

    def __iter__(self) -> Iterator[ExecutionEvent | ExecutionArtifact]:
        """遍历既有行引用，不复制 payload 或暴露可扩展队列。"""
        return iter(self._entries)

    def add_event(self, event: ExecutionEvent) -> None:
        """入批共享构造器生成的事件，不复制 details 或承诺写入成功。"""
        self._add(event)

    def add_artifact(
        self,
        *,
        execution_id: str,
        artifact_type: ExecutionArtifactType,
        content: dict[str, object],
        schema_version: int = 1,
    ) -> None:
        """入批附件并直接复用 payload 引用，不额外序列化。"""
        self._add(
            ExecutionArtifact(
                execution_id=execution_id,
                artifact_type=artifact_type,
                content=content,
                schema_version=schema_version,
                created_at=now_str(),
            )
        )

    def _add(self, entry: ExecutionEvent | ExecutionArtifact) -> None:
        if len(self._entries) >= self.max_entries:
            raise ValueError(f"审计批次超过固定上限 {self.max_entries}")
        self._entries.append(entry)


@dataclass(frozen=True)
class _Notification:
    execution_id: str
    account_id: int
    event_type: ExecutionEventType
    symbol: str | None
    seq: int

    def publish(self) -> None:
        _notify_live_hub(
            execution_id=self.execution_id,
            account_id=self.account_id,
            event_type=self.event_type,
            symbol=self.symbol,
            seq=self.seq,
        )


@dataclass(frozen=True)
class _CommittedBatch:
    count: int = 0
    notifications: tuple[_Notification, ...] = ()

    def publish(self) -> int:
        for notification in self.notifications:
            notification.publish()
        return self.count


async def _begin_audit_transaction(session: AsyncSession) -> None:
    """显式开启 SQLite 外层事务，防止首次 RELEASE SAVEPOINT 提前提交。"""
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        raw_connection = await connection.get_raw_connection()
        driver_connection = raw_connection.driver_connection
        if driver_connection is None:
            raise RuntimeError("SQLite 驱动连接不可用")
        if not driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")


async def _append_isolated(session: AsyncSession, entry: ExecutionEvent | ExecutionArtifact) -> bool:
    """在独立 SAVEPOINT 中 flush 一条记录，失败不污染同批其余行。"""
    try:
        async with session.begin_nested():
            session.add(entry)
            await session.flush()
        return True
    except IntegrityError as exc:
        tokens = (
            ("event_uid", "uq_execution_event_event_uid")
            if isinstance(entry, ExecutionEvent)
            else ("execution_id", "artifact_type", "uq_execution_artifact_execution_type")
        )
        if _is_duplicate_integrity_error(exc, *tokens):
            logger.debug("跳过批次中重复的执行审计: {}", exc)
        else:
            logger.error("写入批次审计行失败: {}", exc)
    except Exception as exc:
        logger.error("写入批次审计行失败: {}", exc)
    return False


async def _persist_batch(batch: ExecutionAuditBatch) -> _CommittedBatch:
    """只负责事务，返回不含 payload 的已提交通知描述。"""
    if not batch:
        return _CommittedBatch()
    try:
        async with SessionLocal() as session:
            try:
                await _begin_audit_transaction(session)
                count = 0
                notifications: list[_Notification] = []
                for entry in batch:
                    if not await _append_isolated(session, entry):
                        continue
                    count += 1
                    if isinstance(entry, ExecutionEvent):
                        notifications.append(
                            _Notification(
                                execution_id=entry.execution_id,
                                account_id=entry.account_id,
                                event_type=entry.event_type,
                                symbol=entry.symbol,
                                seq=entry.seq,
                            )
                        )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return _CommittedBatch(count, tuple(notifications))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("提交执行审计批次失败，已放弃（审计尽力而为）: {}", exc)
        return _CommittedBatch()


async def append_execution_audit_batch(batch: ExecutionAuditBatch) -> int:
    """一次提交阶段审计，成功后通知；仅返回已提交行数，失败软返回零。"""
    committed = await _persist_batch(batch)
    return committed.publish()


def append_execution_audit_batch_sync(batch: ExecutionAuditBatch) -> int:
    """同步等待一次提交，并在调用方线程通知，不在临时 DB 线程广播。"""
    try:
        committed = _run_coroutine_sync(lambda: _persist_batch(batch))
    except Exception as exc:
        logger.error("同步提交执行审计批次失败，已放弃（审计尽力而为）: {}", exc)
        return 0
    return committed.publish()
