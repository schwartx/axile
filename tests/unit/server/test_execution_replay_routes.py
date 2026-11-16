"""回放请求只读、显式选择录制段并校验业务归属。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Response

from axile.server.api.routes import account_crud
from axile.server.db.models.account import Account, AccountUpdate
from axile.server.db.models.account_runtime_sync import AccountRuntimeSyncState


@pytest.mark.parametrize("acknowledged", [True, False])
@pytest.mark.parametrize("recording_only", [True, False])
def test_recording_patch_keeps_acknowledgement_separate_from_trading_sync(monkeypatch, acknowledged, recording_only):
    account = Account(id=7, name="test", market="future", trade_channel="ctp", brokerage="test")
    account.runtime_sync = AccountRuntimeSyncState(status="synchronized", revision=1, attempts=1)
    stored = account.model_dump(exclude={"replay_config_version"}) | {
        "replay_recording_enabled": True,
        "runtime_sync": {"status": "pending", "revision": 2, "attempts": 1},
    }
    applied = []
    reconciled = []
    returned_sync = []

    async def get_account(_):
        return account

    async def save(*args):
        return {"account": stored, "runtime_changed": False}

    async def configure(snapshot):
        applied.append((snapshot.replay_recording_enabled, snapshot.replay_config_version))
        return {
            "config_version": snapshot.replay_config_version,
            "effective_version": snapshot.replay_config_version if acknowledged else None,
        }

    async def native(name, request):
        if name == "runtime_sync_begin_attempt":
            return {"revision": 2, "attempt_id": 1}
        assert name == "runtime_sync_finish_attempt"
        return {"status": "synchronized", "revision": 2, "attempts": 2}

    async def response(account, sync):
        returned_sync.append(sync)
        return account

    async def reconcile(_sched, account_id):
        assert not recording_only, "recording-only PATCH must not reconcile trading runtime"
        reconciled.append(account_id)
        return {"status": "synchronized", "revision": 2, "attempts": 2}

    async def reap(_account_id):
        assert not recording_only

    from axile.server.execution import notification_outbox, supplements
    from axile.server.execution.worker_backend import manager

    monkeypatch.setattr(account_crud, "_get_account_or_404", get_account)
    monkeypatch.setattr(account_crud, "_account_operation", save)
    monkeypatch.setattr(account_crud, "_account_response", response)
    monkeypatch.setattr(account_crud, "_validate_channel_account_config", lambda *args: {})
    monkeypatch.setattr(account_crud, "_check_algorithm_channel_compat", lambda *args: None)
    monkeypatch.setattr(account_crud, "_reconcile_committed_runtime", reconcile)
    monkeypatch.setattr(account_crud, "native_operation", native)
    monkeypatch.setattr(manager, "get_worker_backend_manager", lambda: SimpleNamespace(configure_replay=configure))
    monkeypatch.setattr(supplements, "reap_cancelled_queue", reap)
    monkeypatch.setattr(notification_outbox, "wake_notifications", lambda _: None)
    response = Response()
    patch = {"replay_recording_enabled": True} | ({} if recording_only else {"remark": "updated"})
    asyncio.run(account_crud._update_account(object(), 7, AccountUpdate(**patch), response))
    assert applied == [(True, 2)]
    assert response.status_code == (200 if acknowledged else 202)
    assert reconciled == ([] if recording_only else [7])
    assert returned_sync[0]["status"] == "synchronized"
