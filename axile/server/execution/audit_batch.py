"""固定生命周期阶段的显式、有界审计批次；不收集执行中的 runtime 事件。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass

from loguru import logger

from axile.domain.execution import ExecutionArtifactType, ExecutionEventType
from axile.server.db.models import ExecutionArtifact, ExecutionEvent, now_str
from axile.server.execution_audit import (
    SyncAuditContextError,
    _notify_live_hub,
    _run_coroutine_sync,
)
from axile.server.native_store import native_operation

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
        artifact = ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=artifact_type,
            content=content,
            schema_version=schema_version,
            created_at=now_str(),
        )
        artifact.content = content
        self._add(artifact)

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


def _batch_request(batch: ExecutionAuditBatch) -> dict[str, object]:
    """坏载荷只标记对应行，其余行一次交给原生事务。"""
    entries = []
    for entry in batch:
        try:
            row = entry.model_dump(mode="python")
            json.dumps(row, ensure_ascii=False, allow_nan=False)
            entries.append(
                {
                    "kind": "event" if isinstance(entry, ExecutionEvent) else "artifact",
                    "row": row,
                }
            )
        except Exception as exc:
            entries.append({"kind": "invalid", "error": str(exc)})
    return {"entries": entries}


async def _persist_batch(batch: ExecutionAuditBatch) -> _CommittedBatch:
    """只等待持久化回执，返回不含 payload 的已提交通知描述。"""
    if not batch:
        return _CommittedBatch()
    try:
        receipt = await native_operation("append_execution_audit_batch", _batch_request(batch))
        for failure in receipt["failures"]:
            logger.error("写入批次审计行失败: {}", failure["error"])
        return _CommittedBatch(
            receipt["count"],
            tuple(
                _Notification(**{**row, "event_type": ExecutionEventType(row["event_type"])})
                for row in receipt["notifications"]
            ),
        )
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
    except SyncAuditContextError:
        raise
    except Exception as exc:
        logger.error("同步提交执行审计批次失败，已放弃（审计尽力而为）: {}", exc)
        return 0
    return committed.publish()
