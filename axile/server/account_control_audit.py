"""账户控制审计查询服务."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel, select

from axile.executor.account_control.context import control_context_kind
from axile.executor.account_control.models import AccountControlDecision
from axile.server.db.models import AccountControlEvent

EXECUTION_LIST_DEFAULT_LIMIT = 100
EXECUTION_EVENTS_DEFAULT_LIMIT = 200
AUDIT_QUERY_MAX_LIMIT = 500


class AccountControlAuditSummaryRow(SQLModel):
    """账户日审计汇总行."""

    operation: str
    decision: AccountControlDecision
    outcome: str
    count: int


class AccountControlAuditExecutionRow(SQLModel):
    """账户日 execution 聚合行."""

    execution_id: str
    context_kind: str = "unknown"
    sources: list[str] = Field(default_factory=list)
    event_count: int
    first_event_at: str
    last_event_at: str


class AccountControlAuditExecutionPage(SQLModel):
    """账户日 execution 分页结果."""

    data: list[AccountControlAuditExecutionRow]
    count: int


class AccountControlAuditEventPage(SQLModel):
    """execution 事件流分页结果."""

    data: list[AccountControlEvent]
    count: int


def _normalize_pagination(*, skip: int, limit: int, default_limit: int) -> tuple[int, int]:
    if skip < 0:
        raise ValueError("skip 必须大于等于 0")

    normalized_limit = default_limit if limit <= 0 else limit
    if normalized_limit > AUDIT_QUERY_MAX_LIMIT:
        raise ValueError(f"limit 不能超过 {AUDIT_QUERY_MAX_LIMIT}")
    return skip, normalized_limit


async def query_account_control_audit_summary(
    session: AsyncSession,
    *,
    account_id: int,
    control_date: str,
) -> list[AccountControlAuditSummaryRow]:
    """查询账户在指定 control_date 下的账户控制审计汇总."""
    stmt = (
        select(
            AccountControlEvent.operation,
            AccountControlEvent.decision,
            AccountControlEvent.outcome,
            func.count(AccountControlEvent.id).label("count"),
        )
        .where(
            AccountControlEvent.account_id == account_id,
            AccountControlEvent.control_date == control_date,
        )
        .group_by(
            AccountControlEvent.operation,
            AccountControlEvent.decision,
            AccountControlEvent.outcome,
        )
        .order_by(
            AccountControlEvent.operation,
            AccountControlEvent.decision,
            AccountControlEvent.outcome,
        )
    )
    rows = (await session.execute(stmt)).all()
    return [
        AccountControlAuditSummaryRow(
            operation=operation,
            decision=decision,
            outcome=outcome,
            count=count,
        )
        for operation, decision, outcome, count in rows
    ]


async def query_account_control_audit_executions(
    session: AsyncSession,
    *,
    account_id: int,
    control_date: str,
    skip: int = 0,
    limit: int = EXECUTION_LIST_DEFAULT_LIMIT,
) -> AccountControlAuditExecutionPage:
    """查询账户日视角下涉及的 execution 列表."""
    skip, limit = _normalize_pagination(
        skip=skip,
        limit=limit,
        default_limit=EXECUTION_LIST_DEFAULT_LIMIT,
    )
    conditions = (
        AccountControlEvent.account_id == account_id,
        AccountControlEvent.control_date == control_date,
    )
    count_stmt = (
        select(func.count(func.distinct(AccountControlEvent.execution_id)))
        .select_from(AccountControlEvent)
        .where(*conditions)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    first_event_at = func.min(AccountControlEvent.created_at).label("first_event_at")
    last_event_at = func.max(AccountControlEvent.created_at).label("last_event_at")
    event_count = func.count(AccountControlEvent.id).label("event_count")
    stmt = (
        select(
            AccountControlEvent.execution_id,
            event_count,
            first_event_at,
            last_event_at,
        )
        .where(*conditions)
        .group_by(AccountControlEvent.execution_id)
        .order_by(desc(last_event_at), desc(AccountControlEvent.execution_id))
        .offset(skip)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    descriptions = await _context_descriptions(session, conditions, [row[0] for row in rows])
    return AccountControlAuditExecutionPage(
        data=[
            AccountControlAuditExecutionRow(
                execution_id=execution_id,
                context_kind=descriptions[execution_id][0],
                sources=descriptions[execution_id][1],
                event_count=count,
                first_event_at=first_at,
                last_event_at=last_at,
            )
            for execution_id, count, first_at, last_at in rows
        ],
        count=total,
    )


async def _context_descriptions(
    session: AsyncSession, conditions: tuple[Any, ...], ids: list[str]
) -> dict[str, tuple[str, list[str]]]:
    """只读取本页上下文的类型及来源，保持原有分页数量和排序。"""
    if not ids:
        return {}
    statement = (
        select(AccountControlEvent.execution_id, AccountControlEvent.context_kind, AccountControlEvent.source)
        .where(*conditions, AccountControlEvent.execution_id.in_(ids))
        .distinct()
    )
    values = (await session.execute(statement)).all()
    kinds: dict[str, set[str]] = {key: set() for key in ids}
    sources: dict[str, set[str]] = {key: set() for key in ids}
    for key, kind, source in values:
        kinds[key].add(control_context_kind(kind, key).value)
        sources[key].add(source)
    return {key: (next(iter(kinds[key])) if len(kinds[key]) == 1 else "unknown", sorted(sources[key])) for key in ids}


async def query_account_control_execution_events(
    session: AsyncSession,
    *,
    execution_id: str,
    symbol: str | None = None,
    operation: str | None = None,
    skip: int = 0,
    limit: int = EXECUTION_EVENTS_DEFAULT_LIMIT,
) -> AccountControlAuditEventPage:
    """查询某次 execution 的账户控制事件流."""
    skip, limit = _normalize_pagination(
        skip=skip,
        limit=limit,
        default_limit=EXECUTION_EVENTS_DEFAULT_LIMIT,
    )
    conditions: list[Any] = [AccountControlEvent.execution_id == execution_id]
    if symbol is not None:
        conditions.append(AccountControlEvent.symbol == symbol)
    if operation is not None:
        conditions.append(AccountControlEvent.operation == operation)

    count_stmt = select(func.count()).select_from(AccountControlEvent).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(AccountControlEvent)
        .where(*conditions)
        .order_by(AccountControlEvent.seq, AccountControlEvent.id)
        .offset(skip)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return AccountControlAuditEventPage(data=rows, count=total)
