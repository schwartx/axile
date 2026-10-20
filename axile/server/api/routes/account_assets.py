"""账户资产快照读取与主动刷新路由."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import desc, func, select

from axile.server.account_assets import query_account_assets
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.asset_observations import is_asset_observation, valid_asset_snapshot_condition
from axile.server.business_facts.assets import read_snapshots_assets, write_snapshot_assets
from axile.server.db.models import (
    AccountAssetSnapshot,
    AccountAssetSnapshotListPublic,
    AccountAssetSnapshotPublic,
)
from axile.server.error_details import exception_details
from axile.server.execution.registry import (
    clear_account_asset_refresh,
    try_register_account_asset_refresh,
)
from axile.server.execution.resident_connection import AccountConnectionUnavailable

router = APIRouter()


def _snapshot_public(snapshot: AccountAssetSnapshot, assets: dict[str, Any]) -> AccountAssetSnapshotPublic:
    """显式组装资产事实，不让 ORM 属性隐式重建整份观测。"""
    return AccountAssetSnapshotPublic(
        id=snapshot.id,
        account_id=snapshot.account_id,
        assets=assets,
        source=cast("Any", snapshot.source),
        execution_id=snapshot.execution_id,
        created_at=snapshot.created_at,
    )


@router.post("/{account_id}/assets/refresh", response_model=AccountAssetSnapshotPublic)
async def refresh_account_assets(session: SessionDep, account_id: int) -> AccountAssetSnapshotPublic:
    """从交易渠道查询并持久化账户最新资产快照."""
    account = await _get_account_or_404(session, account_id)
    if not try_register_account_asset_refresh(account_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="账户正在执行或刷新资产，请稍后再试",
        )

    try:
        # close 会释放读事务并分离账户；返回后保存快照使用新的事务。
        await session.close()
        assets = await query_account_assets(account)
        if not is_asset_observation(assets.model_dump()):
            raise ValueError("渠道未返回可信账户资产")
        snapshot = AccountAssetSnapshot(
            account_id=account_id,
            source="manual",
        )
        payload = assets.model_dump(mode="json")
        await write_snapshot_assets(session, snapshot, payload)
        await session.commit()
        await session.refresh(snapshot)
        return _snapshot_public(snapshot, payload)
    except AccountConnectionUnavailable as exc:
        await session.rollback()
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": exc.execution_error, "error_scope": exc.error_scope},
            headers=headers,
        ) from exc
    except TimeoutError as exc:
        await session.rollback()
        logger.warning(f"账户资产刷新超时: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=exception_details(exc, "账户权益查询超时，请稍后重试"),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        logger.exception(f"账户资产刷新失败: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exception_details(exc, "账户权益查询失败"),
        ) from exc
    finally:
        clear_account_asset_refresh(account_id)


@router.get("/{account_id}/asset_snapshots", response_model=AccountAssetSnapshotListPublic)
async def list_account_asset_snapshots(
    session: SessionDep,
    account_id: int,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> AccountAssetSnapshotListPublic:
    """分页读取账户资产快照（最新在前）."""
    await _get_account_or_404(session, account_id)
    async with session.begin_nested():
        return await _read_asset_snapshot_page(session, account_id, skip, limit)


async def _read_asset_snapshot_page(
    session: AsyncSession, account_id: int, skip: int, limit: int
) -> AccountAssetSnapshotListPublic:
    """让快照页头和事实明细使用同一数据库读快照。"""
    count = await session.scalar(
        select(func.count())
        .select_from(AccountAssetSnapshot)
        .where(AccountAssetSnapshot.account_id == account_id, valid_asset_snapshot_condition())
    )
    statement = (
        select(AccountAssetSnapshot)
        .where(AccountAssetSnapshot.account_id == account_id, valid_asset_snapshot_condition())
        .order_by(desc(AccountAssetSnapshot.id))
        .offset(skip)
        .limit(limit)
    )
    rows = (await session.execute(statement)).scalars().all()
    assets = await read_snapshots_assets(session, rows)
    return AccountAssetSnapshotListPublic(
        data=[_snapshot_public(row, assets[cast("int", row.id)]) for row in rows],
        count=int(count or 0),
    )
