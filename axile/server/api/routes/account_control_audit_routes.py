"""账户控制审计相关路由。"""

from __future__ import annotations

from collections.abc import Awaitable

from fastapi import APIRouter, HTTPException, Query, Response

from axile.server import account_control_audit
from axile.server._native import _core
from axile.server.db.models import (
    AccountControlAuditExecutionListPublic,
    AccountControlAuditSummaryPublic,
    AccountControlEventListPublic,
)

router = APIRouter()


async def _audit_response(query: Awaitable[bytes]) -> Response:
    try:
        return Response(content=await query, media_type="application/json")
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except _core.NativeError as exc:
        if exc.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from exc
        if exc.args[0] == "INVALID_REQUEST":
            raise HTTPException(422, detail=exc.args[1]) from exc
        raise


@router.get("/{account_id}/control/audit/summary", response_model=list[AccountControlAuditSummaryPublic])
async def account_control_audit_summary(account_id: int, control_date: str = Query(...)) -> Response:
    """查询账户在指定 control_date 下的账户控制审计汇总。"""
    return await _audit_response(
        account_control_audit.query_account_control_audit_summary(account_id=account_id, control_date=control_date)
    )


@router.get("/{account_id}/control/audit/executions", response_model=AccountControlAuditExecutionListPublic)
async def account_control_audit_executions(
    account_id: int,
    control_date: str = Query(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(account_control_audit.EXECUTION_LIST_DEFAULT_LIMIT, ge=1, le=500),
) -> Response:
    """查询控制上下文，类型及来源区分交易执行与账户操作。"""
    return await _audit_response(
        account_control_audit.query_account_control_audit_executions(
            account_id=account_id, control_date=control_date, skip=skip, limit=limit
        )
    )


@router.get("/executions/{execution_id}/control/events", response_model=AccountControlEventListPublic)
async def execution_control_events(
    execution_id: str,
    symbol: str | None = None,
    operation: str | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(account_control_audit.EXECUTION_EVENTS_DEFAULT_LIMIT, ge=1, le=500),
) -> Response:
    """查询 execution 控制事件流，返回 created_at 与 occurred_at_ms。"""
    return await _audit_response(
        account_control_audit.query_account_control_execution_events(
            execution_id=execution_id, symbol=symbol, operation=operation, skip=skip, limit=limit
        )
    )
