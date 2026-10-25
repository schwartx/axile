"""账户资产快照读取与主动刷新路由."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status
from loguru import logger
from starlette.concurrency import run_in_threadpool

from axile.server._native import _core
from axile.server.account_assets import query_account_assets
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.asset_observations import is_asset_observation
from axile.server.business_facts.assets import save_manual_asset_snapshot
from axile.server.core.db import analysis_store
from axile.server.db.models import (
    AccountAssetSnapshotPublic,
)
from axile.server.error_details import exception_details
from axile.server.execution.registry import (
    clear_account_asset_refresh,
    try_register_account_asset_refresh,
)
from axile.server.execution.resident_connection import AccountConnectionUnavailable

router = APIRouter()


@router.post("/{account_id}/assets/refresh", response_model=AccountAssetSnapshotPublic)
async def refresh_account_assets(account_id: int) -> AccountAssetSnapshotPublic:
    """从交易渠道查询并持久化账户最新资产快照."""
    account = await _get_account_or_404(account_id)
    if not try_register_account_asset_refresh(account_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="账户正在执行或刷新资产，请稍后再试",
        )

    try:
        assets = await query_account_assets(account)
        if not is_asset_observation(assets.model_dump()):
            raise ValueError("渠道未返回可信账户资产")
        payload = assets.model_dump(mode="json")
        snapshot = await save_manual_asset_snapshot(account_id, payload)
        return AccountAssetSnapshotPublic.model_validate({**snapshot, "assets": payload})
    except AccountConnectionUnavailable as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": exc.execution_error, "error_scope": exc.error_scope},
            headers=headers,
        ) from exc
    except TimeoutError as exc:
        logger.warning(f"账户资产刷新超时: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=exception_details(exc, "账户权益查询超时，请稍后重试"),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"账户资产刷新失败: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exception_details(exc, "账户权益查询失败"),
        ) from exc
    finally:
        clear_account_asset_refresh(account_id)


@router.get("/{account_id}/asset_snapshots")
async def list_account_asset_snapshots(
    account_id: int,
    skip: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=100, ge=1, le=500),
) -> Response:
    """从一个原生读事务直接返回资产快照页。"""
    try:
        content = await run_in_threadpool(_core.asset_snapshots, analysis_store(), account_id, skip, limit)
        return Response(content=content, media_type="application/json")
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        raise HTTPException(503, detail={"code": error.args[0], "message": error.args[1]}) from error
