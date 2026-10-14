"""账户资产主动刷新路由测试."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.api.routes import account_assets as account_assets_routes
from axile.server.db.models import Account, AccountAssetSnapshot
from tests.unit.server._execution_test_support import build_account


class _Session:
    async def close(self) -> None:
        return None

    def __init__(self, account: Account | None) -> None:
        self.account = account
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model: object, account_id: int) -> Account | None:
        if self.account is not None and self.account.id == account_id:
            return self.account
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value: object) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_refresh_account_assets_persists_manual_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account(id=41)
    session = _Session(account)

    async def _query(_account: Account) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=600.0,
            total_asset=1000.0,
            market_value=400.0,
            positions=[],
        )

    monkeypatch.setattr(account_assets_routes, "query_account_assets", _query)
    response = asyncio.run(
        account_assets_routes.refresh_account_assets(session, 41)  # pyright: ignore[reportArgumentType]
    )

    assert response.account_id == 41
    assert response.source == "manual"
    assert response.assets["total_asset"] == 1000.0
    assert session.commits == 1
    assert len(session.added) == 1
    assert isinstance(session.added[0], AccountAssetSnapshot)


def test_refresh_account_assets_rejects_account_operation_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(build_account(id=42))
    monkeypatch.setattr(account_assets_routes, "try_register_account_asset_refresh", lambda _account_id: False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            account_assets_routes.refresh_account_assets(session, 42)  # pyright: ignore[reportArgumentType]
        )

    assert exc_info.value.status_code == 409
    assert session.added == []


def test_refresh_account_assets_failure_keeps_last_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(build_account(id=43))

    async def _query(_account: Account) -> UnifiedAccountAssets:
        raise RuntimeError("secret connection detail")

    monkeypatch.setattr(account_assets_routes, "query_account_assets", _query)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            account_assets_routes.refresh_account_assets(session, 43)  # pyright: ignore[reportArgumentType]
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["technical_detail"] == "secret connection detail"
    assert "secret connection detail" in exc_info.value.detail["traceback"]
    assert session.added == []
    assert session.commits == 0
    assert session.rollbacks == 1


def test_refresh_account_assets_timeout_returns_gateway_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(build_account(id=44))

    async def _query(_account: Account) -> UnifiedAccountAssets:
        raise TimeoutError("channel timeout detail")

    monkeypatch.setattr(account_assets_routes, "query_account_assets", _query)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            account_assets_routes.refresh_account_assets(session, 44)  # pyright: ignore[reportArgumentType]
        )

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail["technical_detail"] == "channel timeout detail"
    assert "TimeoutError" in exc_info.value.detail["traceback"]
    assert session.added == []
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.parametrize("source", ["assumed", "error", "unavailable"])
def test_untrusted_refresh_preserves_previous_snapshot(monkeypatch, source):
    session = _Session(build_account(id=48))

    async def query(_account):
        return UnifiedAccountAssets(available_cash=0, total_asset=0, market_value=0, source=source)

    monkeypatch.setattr(account_assets_routes, "query_account_assets", query)
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_assets_routes.refresh_account_assets(session, 48))
    assert error.value.status_code == 502
    assert session.added == []
    assert session.commits == 0
