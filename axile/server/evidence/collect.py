"""在单个显式读事务中按主键采集证据，避免跨页漂移。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import LargeBinary, and_, case, cast, func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from axile.executor.account_control.context import control_context_kind, legacy_operation_source
from axile.executor.account_control.models import AccountControlContextKind
from axile.server.business_facts.artifacts import (
    BusinessFactReferenceError,
    parse_artifact_reference,
    resolve_artifact_content,
    resolve_event_details,
)
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
    snapshot: EvidenceSnapshot,
    key: str,
    execution_id: str | None,
    reason: str,
    budget: ExportBudget,
    *,
    context_kind: str = "execution",
    source: str = "unknown",
) -> None:
    """账户操作独立收集，未知历史材料仍保留在执行集合中。"""
    kind = control_context_kind(context_kind, key)
    operations = kind == AccountControlContextKind.ACCOUNT_OPERATION and key not in snapshot.executions
    selected = snapshot.account_operations if operations else snapshot.executions
    if key not in selected:
        if not operations and len(snapshot.executions) >= budget.limits.executions:
            raise EvidenceError(
                "执行数量超过导出上限，请按执行清单分批导出",
                code="EVIDENCE_TOO_MANY_EXECUTIONS",
                status=413,
            )
        selected[key] = snapshot.account_operations.pop(key, None) or ExecutionEvidence(
            key=key, execution_id=execution_id
        )
        selected[key].context_kind = kind.value
    elif selected[key].execution_id != execution_id:
        raise EvidenceError("历史记录与执行标识冲突", code="EVIDENCE_IDENTIFIER_CONFLICT", status=409)
    if not operations and kind == AccountControlContextKind.EXECUTION:
        selected[key].context_kind = kind.value
    reasons = selected[key].selection_reasons
    if reason not in reasons:
        reasons.append(reason)
    if source not in selected[key].sources:
        selected[key].sources.append(source)


def _select_control(snapshot: EvidenceSnapshot, row: Any, reason: str, budget: ExportBudget) -> None:
    """按持久化类型发现上下文，历史来源只识别完整的系统生成 ID。"""
    source = row.source
    if source == "unknown" and row.context_kind == "unknown":
        legacy = legacy_operation_source(row.execution_id)
        source = legacy.value if legacy is not None else source
    _select_execution(
        snapshot, row.execution_id, row.execution_id, reason, budget, context_kind=row.context_kind, source=source
    )


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
        _select_control(snapshot, row, "control_event_in_window", budget)
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
        _select_control(snapshot, row, "control_session_overlaps_window", budget)
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
            _select_control(snapshot, row, "counter_bucket_context_time_assumed", budget)


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
    """分批读取两类上下文，避免大量查询 ID 超过数据库参数数量限制。"""
    contexts = {item.execution_id: item for item in snapshot.iter_contexts() if item.execution_id}
    ids = list(contexts)
    for start in range(0, len(ids), PAGE_SIZE):
        batch = ids[start : start + PAGE_SIZE]
        await _load_context_batch(session, snapshot, contexts, batch, budget)
    _classify_loaded_contexts(snapshot, budget)


async def _load_context_batch(
    session: AsyncSession,
    snapshot: EvidenceSnapshot,
    contexts: dict[str, ExecutionEvidence],
    ids: list[str],
    budget: ExportBudget,
) -> None:
    """所有权冲突检查覆盖账户操作，附件也不能借操作上下文跨账户读取。"""
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
            item = contexts[row.execution_id]
            if field_name in {"intent", "record"}:
                setattr(item, field_name, data)
            else:
                getattr(item, field_name).append(data)
            count += 1
        name = str(model.__tablename__)
        snapshot.source_counts[name] = snapshot.source_counts.get(name, 0) + count


def _classify_loaded_contexts(snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
    """真实业务材料优先于保留前缀，单次按 ID 导出也采用相同分类。"""
    for item in list(snapshot.iter_contexts()):
        business = (
            item.intent is not None
            or item.record is not None
            or item.input_id is not None
            or bool(item.events or item.artifacts)
        )
        rows = [*item.controls, *item.counters, *item.sessions]
        kinds = {control_context_kind(row.get("context_kind", "unknown"), item.key) for row in rows}
        operation = not business and kinds == {AccountControlContextKind.ACCOUNT_OPERATION}
        if business:
            item.context_kind = "execution"
        elif operation:
            item.context_kind = "account_operation"
        elif kinds == {AccountControlContextKind.EXECUTION}:
            item.context_kind = "execution"
        else:
            item.context_kind = "unknown"
        for row in rows:
            source = row.get("source", "unknown")
            if source not in item.sources:
                item.sources.append(source)
        if operation:
            snapshot.executions.pop(item.key, None)
            snapshot.account_operations[item.key] = item
        elif item.key in snapshot.account_operations:
            snapshot.account_operations.pop(item.key)
            snapshot.executions[item.key] = item
    if len(snapshot.executions) > budget.limits.executions:
        raise EvidenceError(
            "执行数量超过导出上限，请按执行清单分批导出",
            code="EVIDENCE_TOO_MANY_EXECUTIONS",
            status=413,
        )


async def _preflight_fact_sources(
    session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget, sources: tuple[tuple[Any, Any], ...]
) -> None:
    """在解码任一事实 JSON 前，对完整关系图的物理行数和原始字节计费。"""
    for table, predicate in sources:
        if table.name == str(ExecuteRecord.__tablename__):
            # 执行头已经由 _pages 读取并计费；事实表注册表也包含这个头。
            continue
        budget.check()
        raw_size: Any = sum(func.coalesce(func.length(cast(column, LargeBinary)), 0) for column in table.columns)
        statement = select(func.count(), func.coalesce(func.sum(raw_size), 0)).select_from(table).where(predicate)
        rows, size = (await session.execute(statement)).one()
        budget.check(rows=rows, size=size)
        snapshot.source_counts[table.name] = snapshot.source_counts.get(table.name, 0) + rows


def _charge_assembled(budget: ExportBudget, data: Any) -> None:
    """组装视图也占用内存；重复引用的展开不能绕开全局字节预算。"""
    budget.check(size=len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()))


def _fact_references(item: ExecutionEvidence) -> Iterator[Mapping[str, object]]:
    """只枚举声明过的附件和事件引用位置，不猜测未知扩展中的内容。"""
    for artifact in item.artifacts:
        reference = parse_artifact_reference(artifact["content"])
        if reference is not None:
            yield reference
    for event in item.events:
        details = event.get("details")
        decision = details.get("decision") if isinstance(details, dict) else None
        if isinstance(decision, dict):
            reference = parse_artifact_reference(decision)
            if reference is not None:
                yield reference


async def _hydrate_context_views(
    session: AsyncSession, item: ExecutionEvidence, records: dict[int, Any], account_id: int, budget: ExportBudget
) -> None:
    from axile.server.business_facts.assets import read_input_asset_observations
    from axile.server.business_facts.repository import read_execution_detail, read_execution_input

    if item.record is not None:
        item.record = await read_execution_detail(session, records[item.record["id"]])
        _charge_assembled(budget, item.record)
        item.input = item.record["input"]
    elif item.input_id is not None:
        item.input = await read_execution_input(session, item.input_id)
        _charge_assembled(budget, item.input)
    if item.input_id is not None:
        item.asset_observations = [
            {
                "input_id": item.input_id,
                "fact_path": row["path"],
                **{key: value for key, value in row.items() if key != "path"},
            }
            for row in await read_input_asset_observations(session, item.input_id)
        ]
        _charge_assembled(budget, item.asset_observations)
    for artifact in item.artifacts:
        if parse_artifact_reference(artifact["content"]) is not None:
            artifact["content"] = await resolve_artifact_content(session, artifact, account_id=account_id)
            _charge_assembled(budget, artifact["content"])
    for event in item.events:
        resolved = await resolve_event_details(session, event, account_id=account_id)
        if resolved is not event["details"]:
            event["details"] = resolved
            _charge_assembled(budget, resolved)


async def _validate_asset_sources(
    session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget, record_ids: tuple[int, ...]
) -> str | None:
    """先计费绑定身份行，再沿标量所有权链验证，之后才允许跟随资产引用。"""
    from axile.server.business_facts.assets import asset_binding, validate_result_asset_ownership

    if not record_ids:
        return None
    await _preflight_fact_sources(
        session, snapshot, budget, ((asset_binding, asset_binding.c.record_id.in_(record_ids)),)
    )
    for record_id in record_ids:
        await validate_result_asset_ownership(session, record_id)
    return asset_binding.name


async def _preflight_notification_contexts(
    session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget, records: dict[int, Any]
) -> None:
    """通知捕获去重后，冷读所引用的 outbox 同样必须先按物理体积计费。"""
    from axile.server.business_facts.extensions import execution_extension
    from axile.server.db.models.supplement import NotificationEvent

    if not records:
        return
    extra = execution_extension.c.extra
    marker_exists = case(
        (
            func.json_valid(extra) == 1,
            func.json_type(extra, '$.server_notification_context."$axile_notification_context"').is_not(None),
        ),
        else_=False,
    )
    referenced_ids = (
        (
            await session.execute(
                select(execution_extension.c.record_id).where(
                    execution_extension.c.record_id.in_(tuple(records)), marker_exists
                )
            )
        )
        .scalars()
        .all()
    )
    event_ids = tuple(
        f"execution:{records[record_id].execution_id}"
        for record_id in referenced_ids
        if records[record_id].execution_id is not None
    )
    if event_ids:
        table: Any = getattr(NotificationEvent, "__table__")
        owners = await session.execute(select(table.c.account_id, table.c.event_type).where(table.c.id.in_(event_ids)))
        if any(account_id != snapshot.account["id"] or kind != "execution.finished" for account_id, kind in owners):
            raise BusinessFactReferenceError("冻结通知引用与执行账户不一致")
        await _preflight_fact_sources(session, snapshot, budget, ((table, table.c.id.in_(event_ids)),))


async def _hydrate_business_facts(session: AsyncSession, snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
    """在同一读事务内解析完整事实及不可变附件引用，不创建缺失的审计材料。"""
    from axile.server.business_facts.repository import (
        execution_fact_sources,
        find_execution_input,
    )

    records: dict[int, Any] = {}
    inputs: set[str] = set()
    assets: set[str] = set()
    for item in snapshot.iter_contexts():
        if item.record is not None:
            record = await session.get(ExecuteRecord, item.record["id"])
            if record is None or record.id is None or record.account_id != snapshot.account["id"]:
                raise BusinessFactReferenceError("执行业务事实的账户归属冲突")
            records[record.id] = record
            item.input_id = record.input_id
            inputs.update(value for value in (record.input_id, record.result_input_id) if value is not None)
        elif item.execution_id is not None:
            item.input_id = await find_execution_input(
                session, account_id=snapshot.account["id"], execution_id=item.execution_id
            )
            if item.input_id is not None:
                inputs.add(item.input_id)
        for reference in _fact_references(item):
            if "input_id" in reference:
                inputs.add(str(reference["input_id"]))
            if "asset_id" in reference:
                assets.add(str(reference["asset_id"]))
    record_ids = tuple(records)
    charged_bindings = await _validate_asset_sources(session, snapshot, budget, record_ids)
    sources = tuple(
        (table, and_(predicate, table.c.record_id.not_in(record_ids)) if table.name == charged_bindings else predicate)
        for table, predicate in execution_fact_sources(
            record_ids=record_ids, input_ids=tuple(inputs), asset_ids=tuple(assets)
        )
    )
    await _preflight_fact_sources(session, snapshot, budget, sources)
    await _preflight_notification_contexts(session, snapshot, budget, records)
    for item in snapshot.iter_contexts():
        await _hydrate_context_views(session, item, records, snapshot.account["id"], budget)
    _classify_loaded_contexts(snapshot, budget)


async def collect_evidence(
    session: AsyncSession,
    budget: ExportBudget,
    *,
    account_id: int | None = None,
    execution_id: str | None = None,
    execution_ids: list[str] | None = None,
    window: EvidenceWindow | None = None,
) -> EvidenceSnapshot:
    """冻结只读证据；独立会话须尚未开启事务，不接触交易进程。"""
    if execution_ids is not None:
        if execution_id is not None or window is not None or account_id is None:
            raise EvidenceError("执行清单须绑定账户且不能与其他选择混用", code="EVIDENCE_INVALID_SELECTION")
        if not execution_ids or len(execution_ids) > budget.limits.executions:
            raise EvidenceError("执行清单超过导出上限", code="EVIDENCE_TOO_MANY_EXECUTIONS", status=413)
        if any(not isinstance(key, str) or not key.strip() for key in execution_ids) or len(set(execution_ids)) != len(
            execution_ids
        ):
            raise EvidenceError("执行清单须为不重复的非空标识", code="EVIDENCE_INVALID_SELECTION")
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
            selected_execution_ids=list(execution_ids) if execution_ids is not None else None,
            source_counts={str(model.__tablename__): 0 for model, _ in _SOURCES},
        )
        if execution_id is not None:
            _select_execution(snapshot, execution_id, execution_id, "explicit_execution", budget)
        elif execution_ids is not None:
            for key in execution_ids:
                budget.check()
                if await _owner(session, key) != account_id:
                    raise EvidenceError("执行不存在或不属于所选账户", code="EVIDENCE_NOT_FOUND", status=404)
                _select_execution(snapshot, key, key, "explicit_execution_ids", budget)
        else:
            await _discover(session, snapshot, budget)
        await _load_sources(session, snapshot, budget)
        for item in snapshot.executions.values():
            if item.execution_id is None:
                record_id = int(item.key.removeprefix("legacy-record-"))
                record = await session.get(ExecuteRecord, record_id)
                item.record = record.model_dump(mode="json", warnings=False) if record is not None else None
                snapshot.source_counts["executerecord"] = snapshot.source_counts.get("executerecord", 0) + 1
        try:
            await _hydrate_business_facts(session, snapshot, budget)
        except (BusinessFactReferenceError, ValueError, TypeError) as exc:
            raise EvidenceError("执行业务事实不完整或引用冲突", code="EVIDENCE_INVALID_DATA", status=409) from exc
        budget.check()
        return snapshot
    finally:
        await session.rollback()
