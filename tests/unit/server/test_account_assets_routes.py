"""账户资产刷新在渠道调用前结束读取，查询成功后一次原生提交。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.api.routes import account_assets
from axile.server.execution.resident_connection import AccountConnectionUnavailable


@pytest.fixture
def route_dependencies(monkeypatch):
    account = SimpleNamespace(id=41)
    read = AsyncMock(return_value=account)
    save = AsyncMock(
        return_value=dict(id=7, account_id=41, source="manual", execution_id=None, created_at="clock-time")
    )
    clear = []
    monkeypatch.setattr(account_assets, "_get_account_or_404", read)
    monkeypatch.setattr(account_assets, "save_manual_asset_snapshot", save)
    monkeypatch.setattr(account_assets, "try_register_account_asset_refresh", lambda _id: True)
    monkeypatch.setattr(account_assets, "clear_account_asset_refresh", clear.append)
    return SimpleNamespace(account=account, read=read, save=save, clear=clear)


def test_refresh_account_assets_persists_manual_snapshot(monkeypatch, route_dependencies):
    returned = UnifiedAccountAssets(available_cash=600, total_asset=1000, market_value=400, positions=[])
    query = AsyncMock(return_value=returned)
    monkeypatch.setattr(account_assets, "query_account_assets", query)
    response = asyncio.run(account_assets.refresh_account_assets(41))
    assert response.id == 7
    assert response.account_id == 41
    assert response.source == "manual"
    assert response.created_at == "clock-time"
    assert response.assets == returned.model_dump(mode="json")
    route_dependencies.read.assert_awaited_once_with(41)
    query.assert_awaited_once_with(route_dependencies.account)
    route_dependencies.save.assert_awaited_once_with(41, returned.model_dump(mode="json"))
    assert route_dependencies.clear == [41]


def test_conflicting_refresh_does_not_query_or_write(monkeypatch, route_dependencies):
    query = AsyncMock()
    monkeypatch.setattr(account_assets, "query_account_assets", query)
    monkeypatch.setattr(account_assets, "try_register_account_asset_refresh", lambda _id: False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_assets.refresh_account_assets(41))
    assert error.value.status_code == 409
    query.assert_not_awaited()
    route_dependencies.save.assert_not_awaited()
    assert route_dependencies.clear == []


@pytest.mark.parametrize(
    "failure,expected",
    [(RuntimeError("channel detail"), 502), (TimeoutError("channel timeout"), 504)],
)
def test_query_failure_preserves_last_snapshot_and_releases_refresh(monkeypatch, route_dependencies, failure, expected):
    monkeypatch.setattr(account_assets, "query_account_assets", AsyncMock(side_effect=failure))
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_assets.refresh_account_assets(41))
    assert error.value.status_code == expected
    assert error.value.detail["technical_detail"] == str(failure)
    assert str(failure) in error.value.detail["traceback"]
    route_dependencies.save.assert_not_awaited()
    assert route_dependencies.clear == [41]


@pytest.mark.parametrize("source", ["assumed", "error", "unavailable"])
def test_untrusted_refresh_preserves_previous_snapshot(monkeypatch, route_dependencies, source):
    monkeypatch.setattr(
        account_assets,
        "query_account_assets",
        AsyncMock(
            return_value=UnifiedAccountAssets(
                available_cash=0,
                total_asset=0,
                market_value=0,
                source=source,
            )
        ),
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_assets.refresh_account_assets(41))
    assert error.value.status_code == 502
    route_dependencies.save.assert_not_awaited()
    assert route_dependencies.clear == [41]


def test_write_failure_releases_refresh(monkeypatch, route_dependencies):
    monkeypatch.setattr(
        account_assets,
        "query_account_assets",
        AsyncMock(return_value=UnifiedAccountAssets(available_cash=0, total_asset=0, market_value=0)),
    )
    route_dependencies.save.side_effect = ValueError("write failed")
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_assets.refresh_account_assets(41))
    assert error.value.status_code == 502
    route_dependencies.save.assert_awaited_once()
    assert route_dependencies.clear == [41]


def test_unavailable_connection_preserves_retry_header(monkeypatch, route_dependencies):
    failure = AccountConnectionUnavailable("connection unavailable", retry_after=2.1)
    monkeypatch.setattr(account_assets, "query_account_assets", AsyncMock(side_effect=failure))
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_assets.refresh_account_assets(41))
    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "3"}
    assert error.value.detail == {"message": failure.execution_error, "error_scope": failure.error_scope}
    route_dependencies.save.assert_not_awaited()
    assert route_dependencies.clear == [41]
