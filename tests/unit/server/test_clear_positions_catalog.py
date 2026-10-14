"""清仓暂停后立即更新后续目录查询来源，不重建本次清仓会话。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from axile.server.api.routes import account_execution
from axile.server.execution import ctp_channels, supplements
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from tests.unit.server._execution_test_support import build_account


@pytest.mark.parametrize("channel", ["ctp", "tq"])
@pytest.mark.parametrize("enqueue_fails", [False, True])
def test_clear_disables_public_catalog_source_before_enqueuing(monkeypatch, channel, enqueue_fails):
    account = build_account(id=1, trade_channel=channel)
    manager = WorkerBackendManager()
    manager.configure_catalog_account(account)
    prepare, drop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "prepare_account", prepare)
    monkeypatch.setattr(manager, "drop_account", drop)
    monkeypatch.setattr(ctp_channels, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(account_execution, "_get_account_or_404", AsyncMock(return_value=account))
    monkeypatch.setattr(
        account_execution,
        "_account_route_module",
        lambda: SimpleNamespace(_build_empty_positions_algorithm=lambda **_kwargs: {"method": "SINGLE-MAKER"}),
    )
    monkeypatch.setattr(supplements, "cancel_groups_in_session", AsyncMock())
    monkeypatch.setattr(account_execution, "delete_job", Mock())
    session = SimpleNamespace(add=Mock(), commit=AsyncMock())

    async def enqueue(_account_id, **_kwargs):
        session.commit.assert_awaited_once()
        assert account.is_started is False
        assert manager._catalog_accounts[1][1] is False
        if enqueue_fails:
            raise RuntimeError("queue unavailable")
        return SimpleNamespace(execution_id="clear-catalog", outcome="created")

    monkeypatch.setattr(account_execution, "enqueue_empty_positions", enqueue)
    try:
        if enqueue_fails:
            with pytest.raises(HTTPException) as caught:
                asyncio.run(account_execution.empty_all_positions(session, Mock(), 1))
            assert caught.value.status_code == 500
        else:
            result = asyncio.run(account_execution.empty_all_positions(session, Mock(), 1))
            assert result.execution_id == "clear-catalog"
        assert manager._catalog_accounts[1][1] is False
        prepare.assert_not_awaited()
        drop.assert_not_awaited()
    finally:
        manager.close()
