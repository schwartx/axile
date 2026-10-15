"""账户控制审计相关路由."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from axile.server import account_control_audit
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models import (
    AccountControlAuditExecutionListPublic,
    AccountControlAuditExecutionPublic,
    AccountControlAuditSummaryPublic,
    AccountControlEventListPublic,
    AccountControlEventPublic,
)

router = APIRouter()


def _build_account_control_event_public(row: object) -> AccountControlEventPublic:
    """将数据库事件模型转换为账户控制审计公开模型."""
    return AccountControlEventPublic(
        id=getattr(row, "id"),
        account_id=getattr(row, "account_id"),
        control_date=getattr(row, "control_date"),
        execution_id=getattr(row, "execution_id"),
        context_kind=getattr(row, "context_kind", "unknown"),
        source=getattr(row, "source", "unknown"),
        seq=getattr(row, "seq"),
        channel=getattr(row, "channel"),
        operation=getattr(row, "operation"),
        symbol=getattr(row, "symbol"),
        metadata=getattr(row, "metadata_"),
        decision=getattr(row, "decision"),
        counted=getattr(row, "counted"),
        outcome=getattr(row, "outcome"),
        event_uid=getattr(row, "event_uid"),
        created_at=getattr(row, "created_at"),
        occurred_at_ms=getattr(row, "occurred_at_ms"),
    )


@router.get(
    "/{account_id}/control/audit/summary",
    response_model=list[AccountControlAuditSummaryPublic],
)
async def account_control_audit_summary(
    session: SessionDep,
    account_id: int,
    control_date: str = Query(...),
) -> list[AccountControlAuditSummaryPublic]:
    """查询账户在指定 control_date 下的账户控制审计汇总."""
    await _get_account_or_404(session, account_id)

    rows = await account_control_audit.query_account_control_audit_summary(
        session,
        account_id=account_id,
        control_date=control_date,
    )
    return [AccountControlAuditSummaryPublic.model_validate(row) for row in rows]


@router.get(
    "/{account_id}/control/audit/executions",
    response_model=AccountControlAuditExecutionListPublic,
)
async def account_control_audit_executions(
    session: SessionDep,
    account_id: int,
    control_date: str = Query(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(account_control_audit.EXECUTION_LIST_DEFAULT_LIMIT, ge=1, le=500),
) -> AccountControlAuditExecutionListPublic:
    """查询指定日期的控制上下文，类型及来源区分交易执行与账户操作。"""
    await _get_account_or_404(session, account_id)

    try:
        page = await account_control_audit.query_account_control_audit_executions(
            session,
            account_id=account_id,
            control_date=control_date,
            skip=skip,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return AccountControlAuditExecutionListPublic(
        data=[AccountControlAuditExecutionPublic.model_validate(row) for row in page.data],
        count=page.count,
    )


@router.get(
    "/executions/{execution_id}/control/events",
    response_model=AccountControlEventListPublic,
)
async def execution_control_events(
    session: SessionDep,
    execution_id: str,
    symbol: str | None = None,
    operation: str | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(account_control_audit.EXECUTION_EVENTS_DEFAULT_LIMIT, ge=1, le=500),
) -> AccountControlEventListPublic:
    """查询指定 execution 的账户控制事件流，返回 `created_at` 与 `occurred_at_ms`."""
    try:
        page = await account_control_audit.query_account_control_execution_events(
            session,
            execution_id=execution_id,
            symbol=symbol,
            operation=operation,
            skip=skip,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return AccountControlEventListPublic(
        data=[_build_account_control_event_public(row) for row in page.data],
        count=page.count,
    )
