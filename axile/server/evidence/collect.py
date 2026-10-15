"""在单个显式读事务中按主键采集证据，避免跨页漂移。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from sqlalchemy import LargeBinary, and_, cast, func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from axile.server.db.models import Account, AccountControlCounterDelta, AccountControlEvent, ExecuteRecord
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.db.models.execution import ExecutionArtifact, ExecutionEvent
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.evidence.models import (
    EvidenceError,
    EvidenceSnapshot,
    EvidenceWindow,
    ExecutionEvidence,
    ExportBudget,
    parse_time,
)

PAGE_SIZE = 500
_OWNED_MODELS: tuple[Any, ...] = (
    ExecutionIntent,
    ExecuteRecord,
    ExecutionEvent,
    AccountControlEvent,
    AccountControlCounterDelta,
    AccountControlSession,
)
_SOURCES: tuple[tuple[Any, str], ...] = (
    (ExecutionIntent, "intent"),
    (ExecuteRecord, "record"),
    (ExecutionEvent, "events"),
    (ExecutionArtifact, "artifacts"),
    (AccountControlEvent, "controls"),
    (AccountControlCounterDelta, "counters"),
    (AccountControlSession, "sessions"),
)


async def _pages(session: AsyncSession, model: Any, conditions: list[Any], budget: ExportBudget) -> AsyncIterator[Any]:
    """用主键游标完整读取；全局预算涵盖候选扫描和证据体积。"""
    cursor = 0
    while True:
        budget.check()
        # 在 ORM 解码 JSON 大列前检查本页原始体积，避免一次预取数百个超大附件。
        raw_size: Any = sum(
            func.coalesce(func.length(cast(column, LargeBinary)), 0) for column in model.__table__.columns
        )
        page_stmt = (
            select(model.id, raw_size.label("raw_bytes"))
            .where(*conditions, model.id > cursor)
            .order_by(model.id)
            .limit(PAGE_SIZE)
        )
        page = (await session.execute(page_stmt)).all()
        if not page:
            return
        if sum(row.raw_bytes for row in page) > budget.limits.bytes - budget.bytes:
            raise EvidenceError("证据量超过导出上限，请缩小范围", code="EVIDENCE_TOO_LARGE", status=413)
        if len(page) > budget.limits.rows - budget.rows:
            raise EvidenceError("证据行数超过导出上限，请缩小范围", code="EVIDENCE_TOO_LARGE", status=413)
        stmt = select(model).where(*conditions, model.id.in_([row.id for row in page])).order_by(model.id)
        rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            # Text 列的历史 enum 值由 ORM 回放为字符串，保留原值而非强制重校验。
            budget.check(
                rows=1, size=len(json.dumps(row.model_dump(mode="json", warnings=False), ensure_ascii=False).encode())
            )
            yield row
        if len(rows) < PAGE_SIZE:
            return
        cursor = rows[-1].id


def _coarse(window: EvidenceWindow, column: Any) -> Any:
    """以宽松日期条件粗筛，精确边界由 Python 时区解释决定。"""
    lower = (window.start - timedelta(days=2)).isoformat()[:19]
    upper = (window.end + timedelta(days=2)).isoformat()[:19]
    return or_(and_(column >= lower, column < upper), func.julianday(column).is_(None))


def _select_execution(
    snapshot: EvidenceSnapshot, key: str, execution_id: str | None, reason: str, budget: ExportBudget
) -> None:
    """合并多种发现来源，且保留没有 execution_id 的旧记录。"""
    if key not in snapshot.executions:
        if len(snapshot.executions) >= budget.limits.executions:
            raise EvidenceError("执行数量超过导出上限，请缩小日期范围", code="EVIDENCE_TOO_MANY_EXECUTIONS", status=413)
        snapshot.executions[key] = ExecutionEvidence(key=key, execution_id=execution_id)
    elif snapshot.executions[key].execution_id != execution_id:
        raise EvidenceError("历史记录与执行标识冲突", code="EVIDENCE_IDENTIFIER_CONFLICT", status=409)
    reasons = snapshot.executions[key].selection_reasons
    if reason not in reasons:
        reasons.append(reason)


async def _discover_intents(session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
    """发现与窗口重叠的已受理执行，包括一直排队或仍在运行的执行。"""
    window = snapshot.window
    assert window is not None
    coarse_start = (window.start - timedelta(days=2)).isoformat()[:19]
    coarse_end = (window.end + timedelta(days=2)).isoformat()[:19]
    conditions = [
        ExecutionIntent.account_id == snapshot.account["id"],
        or_(col(ExecutionIntent.created_at) < coarse_end, func.julianday(ExecutionIntent.created_at).is_(None)),
        or_(
            col(ExecutionIntent.finished_at).is_(None),
            col(ExecutionIntent.finished_at) >= coarse_start,
            func.julianday(ExecutionIntent.finished_at).is_(None),
        ),
    ]
    async for row in _pages(session, ExecutionIntent, conditions, budget):
        start = parse_time(row.created_at, window.legacy_zone)
        finish = parse_time(row.finished_at, window.legacy_zone)
        if start is None or (row.finished_at is not None and finish is None):
            reason = "intent_time_unknown"
        elif start < window.end and (finish is None or finish >= window.start):
            reason = "intent_overlaps_window"
        else:
            continue
        _select_execution(snapshot, row.execution_id, row.execution_id, reason, budget)


async def _discover(session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
    """联合意图、结果和两类审计发现执行，不能只从结果账取列表。"""
    await _discover_intents(session, snapshot, budget)
    window = snapshot.window
    assert window is not None
    for model, column, reason in (
        (ExecuteRecord, ExecuteRecord.created_at, "record_created_in_window"),
        (ExecutionEvent, ExecutionEvent.ts_local_created, "execution_event_in_window"),
    ):
        async for row in _pages(
            session, model, [model.account_id == snapshot.account["id"], _coarse(window, column)], budget
        ):
            raw_time = row.created_at if model is ExecuteRecord else row.ts_local_created
            inside = window.contains(raw_time)
            if inside is False:
                continue
            key = row.execution_id or f"legacy-record-{row.id}"
            _select_execution(snapshot, key, row.execution_id, reason if inside else "record_time_unknown", budget)
    conditions = [
        AccountControlEvent.account_id == snapshot.account["id"],
        AccountControlEvent.occurred_at_ms >= int(window.start.timestamp() * 1000),
        AccountControlEvent.occurred_at_ms < int(window.end.timestamp() * 1000),
    ]
    async for row in _pages(session, AccountControlEvent, conditions, budget):
        _select_execution(snapshot, row.execution_id, row.execution_id, "control_event_in_window", budget)
    await _discover_control_context(session, snapshot, budget)


async def _discover_control_context(session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
    """审计事件丢失时，仍可从流控会话和计数桶发现上下文。"""
    window = snapshot.window
    assert window is not None
    conditions = [
        AccountControlSession.account_id == snapshot.account["id"],
        AccountControlSession.started_at_ms < int(window.end.timestamp() * 1000),
        or_(
            col(AccountControlSession.finished_at_ms).is_(None),
            col(AccountControlSession.finished_at_ms) >= int(window.start.timestamp() * 1000),
        ),
    ]
    async for row in _pages(session, AccountControlSession, conditions, budget):
        _select_execution(snapshot, row.execution_id, row.execution_id, "control_session_overlaps_window", budget)
    conditions = [
        AccountControlCounterDelta.account_id == snapshot.account["id"],
        _coarse(window, AccountControlCounterDelta.bucket_start),
    ]
    async for row in _pages(session, AccountControlCounterDelta, conditions, budget):
        start = parse_time(row.bucket_start, window.legacy_zone)
        bucket = getattr(row.bucket_type, "value", row.bucket_type)
        duration = timedelta(days=1) if bucket == "day" else timedelta(minutes=1)
        if start is None or (start < window.end and start + duration > window.start):
            # 计数桶不含逐笔操作时间，也不冻结时区，不能冒充窗口内逐笔事实。
            _select_execution(
                snapshot, row.execution_id, row.execution_id, "counter_bucket_context_time_assumed", budget
            )


async def _owner(session: AsyncSession, execution_id: str) -> int:
    """验证执行所有权；仅有附件但无法关联账户时不猜测归属。"""
    owners: set[int] = set()
    for model in _OWNED_MODELS:
        rows = await session.execute(
            select(model.account_id).where(model.execution_id == execution_id).distinct().limit(2)
        )
        owners.update(rows.scalars().all())
    if not owners:
        raise EvidenceError("执行不存在或无法确定所属账户", code="EVIDENCE_NOT_FOUND", status=404)
    if len(owners) != 1:
        raise EvidenceError("执行材料的账户归属冲突", code="EVIDENCE_OWNER_CONFLICT", status=409)
    return owners.pop()


async def _load_sources(session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
    """一次发现集合对应的完整上下文，按账户再次约束所有有归属字段的表。"""
    ids = [item.execution_id for item in snapshot.executions.values() if item.execution_id]
    if not ids:
        return
    # 附件本身没有 account_id；在读取附件前必须排除跨账户 execution_id 冲突。
    for model in _OWNED_MODELS:
        owners = await session.execute(
            select(model.account_id)
            .where(model.execution_id.in_(ids), model.account_id != snapshot.account["id"])
            .limit(1)
        )
        if owners.first() is not None:
            raise EvidenceError("执行材料的账户归属冲突", code="EVIDENCE_OWNER_CONFLICT", status=409)
    for model, field_name in _SOURCES:
        conditions = [model.execution_id.in_(ids)]
        if model is not ExecutionArtifact:
            conditions.append(model.account_id == snapshot.account["id"])
        count = 0
        async for row in _pages(session, model, conditions, budget):
            data = row.model_dump(mode="json", warnings=False)
            if model is AccountControlEvent:
                data["metadata"] = data.pop("metadata_", {})
            item = snapshot.executions[row.execution_id]
            if field_name in {"intent", "record"}:
                setattr(item, field_name, data)
            else:
                getattr(item, field_name).append(data)
            count += 1
        snapshot.source_counts[str(model.__tablename__)] = count


async def collect_evidence(
    session: AsyncSession,
    budget: ExportBudget,
    *,
    account_id: int | None = None,
    execution_id: str | None = None,
    window: EvidenceWindow | None = None,
) -> EvidenceSnapshot:
    """冻结只读证据；独立会话须尚未开启事务，不接触交易进程。"""
    # aiosqlite 的传统事务模式不会为 SELECT 发 BEGIN，必须显式开启读快照。
    await session.execute(text("BEGIN"))
    try:
        if execution_id is not None:
            account_id = await _owner(session, execution_id)
        row = (
            await session.execute(
                select(Account.id, Account.name, Account.trade_channel).where(Account.id == account_id)
            )
        ).one_or_none()
        if row is None:
            raise EvidenceError("账户不存在", code="EVIDENCE_ACCOUNT_NOT_FOUND", status=404)
        snapshot = EvidenceSnapshot(
            account={"id": row.id, "name": row.name, "channel": getattr(row.trade_channel, "value", row.trade_channel)},
            executions={},
            window=window,
            source_counts={str(model.__tablename__): 0 for model, _ in _SOURCES},
        )
        if execution_id is not None:
            _select_execution(snapshot, execution_id, execution_id, "explicit_execution", budget)
        else:
            await _discover(session, snapshot, budget)
        await _load_sources(session, snapshot, budget)
        for item in snapshot.executions.values():
            if item.execution_id is None:
                record_id = int(item.key.removeprefix("legacy-record-"))
                record = await session.get(ExecuteRecord, record_id)
                item.record = record.model_dump(mode="json", warnings=False) if record is not None else None
                snapshot.source_counts["executerecord"] = snapshot.source_counts.get("executerecord", 0) + 1
        budget.check()
        return snapshot
    finally:
        await session.rollback()
