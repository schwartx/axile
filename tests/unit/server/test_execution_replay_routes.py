"""回放请求只读、显式选择录制段并校验业务归属。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_crud, execution_replay
from axile.server.db.models.account import Account, AccountUpdate
from axile.server.db.models.account_runtime_sync import AccountRuntimeSyncPublic


def test_window_requires_recording_and_preserves_fixed_selection(monkeypatch, tmp_path):
    calls = []

    async def status(execution_id):
        return {"account_id": 7}

    def read(root, account_id, execution_id, request):
        calls.append((root, account_id, execution_id, request))
        return {"recording_id": request["recording_id"], "messages": [], "complete": True}

    monkeypatch.setattr(execution_replay, "get_execution_status", status)
    monkeypatch.setattr(execution_replay, "replay_root", lambda: tmp_path)
    monkeypatch.setattr(execution_replay.service, "replay_window", read)
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        path = "/account/executions/execution/replay/window"
        assert client.get(path, params={"start_us": 1, "end_us": 2}).status_code == 422
        assert not calls
        response = client.get(path, params={"recording_id": "rec-A", "start_us": 1, "end_us": 2, "symbols": "A,B"})
        assert response.status_code == 200
        assert response.json()["account_id"] == 7
        assert calls == [
            (tmp_path, 7, "execution", {"recording_id": "rec-A", "start_us": 1, "end_us": 2, "symbols": ["A", "B"]})
        ]
        assert client.get(path, params={"recording_id": "../other", "start_us": 1, "end_us": 2}).status_code == 422


def test_manifest_rejects_foreign_catalog_without_account_or_channel_queries(monkeypatch):
    async def status(execution_id):
        return {"account_id": 7}

    monkeypatch.setattr(execution_replay, "get_execution_status", status)
    monkeypatch.setattr(execution_replay, "get_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(execution_replay.service, "replay_manifest", lambda *args: {"account_id": 8})
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        assert client.get("/account/executions/execution/replay").status_code == 409


def test_native_budget_failure_has_actionable_code_without_local_path(monkeypatch):
    async def status(execution_id):
        return {"account_id": 7}

    def read(*args):
        raise _core.NativeError("REPLAY_BUDGET", "/private/account/secret recording exceeds scan budget")

    monkeypatch.setattr(execution_replay, "get_execution_status", status)
    monkeypatch.setattr(execution_replay, "get_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(execution_replay.service, "replay_manifest", read)
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        response = client.get("/account/executions/execution/replay")
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "REPLAY_BUDGET"
        assert "/private/" not in response.text


def test_manifest_ignores_later_unassociated_partial(monkeypatch, tmp_path):
    from tests.conftest import require_native_store

    core = require_native_store("ExecutionReplayWriter", "execution_replay_manifest")
    writer = core.ExecutionReplayWriter(str(tmp_path), 7, "current", '{"schema_version":1}')
    writer.append(
        json.dumps(
            {
                "schema_version": 1,
                "recording_id": "current",
                "capture_seq": 1,
                "time_us": 100,
                "topic": "/execution",
                "data": {"execution_id": "recorded", "phase": "started"},
            }
        )
    )

    async def status(_):
        return {"account_id": 7}

    async def intent(execution_id):
        finished_at = {
            "earlier": "1970-01-01T00:00:00.000099Z",
            "same-time": "1970-01-01T00:00:00.000100Z",
            "unknown-time": None,
            "recorded": "1970-01-01T00:00:00.000099Z",
        }[execution_id]
        return SimpleNamespace(account_id=7, started_at=None, finished_at=finished_at)

    monkeypatch.setattr(execution_replay, "get_execution_status", status)
    monkeypatch.setattr(execution_replay, "get_intent", intent)
    monkeypatch.setattr(execution_replay, "replay_root", lambda: tmp_path)
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        path = "/account/executions/{}/replay"
        earlier = client.get(path.format("earlier"))
        assert earlier.status_code == 200, earlier.text
        assert earlier.json()["availability"] == "not_recorded"
        assert earlier.json()["recordings"] == []
        for execution_id in ("same-time", "unknown-time"):
            ambiguous = client.get(path.format(execution_id))
            assert ambiguous.status_code == 409, ambiguous.text
            assert ambiguous.json()["detail"]["code"] == "REPLAY_INCOMPLETE"
        recorded = client.get(path.format("recorded"))
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["availability"] == "partial"
        assert [item["recording_id"] for item in recorded.json()["recordings"]] == ["current"]
    writer.finish()


@pytest.mark.parametrize("acknowledged", [True, False])
@pytest.mark.parametrize("recording_only", [True, False])
def test_recording_patch_keeps_acknowledgement_separate_from_trading_sync(monkeypatch, acknowledged, recording_only):
    account = Account(id=7, name="test", market="future", trade_channel="ctp", brokerage="test")
    account.runtime_sync = AccountRuntimeSyncPublic(status="synchronized", revision=1, attempts=1)
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


def test_native_database_recording_and_http_window_roundtrip(monkeypatch, native_database):
    """使用真实业务库、MCAP 和只读 HTTP；不调用任何当前渠道。"""
    from axile.server.core import db
    from axile.server.execution.worker_backend import replay as bridge
    from tests.conftest import require_native_store

    core = require_native_store("ExecutionReplayWriter", "execution_replay_manifest", "execution_replay_window")
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    account = Account(name="replay-only", market="future", trade_channel="ctp", brokerage="test")
    saved = core.account_create(
        str(native_database),
        json.dumps(
            {"account": account.model_dump(exclude={"id", "replay_config_version"}), "now": "2026-10-30T00:00:00Z"}
        ),
    )
    account_id = saved["account"]["id"]
    assert saved["account"]["replay_recording_enabled"] is False
    execution_id = "replay-http-native"
    core.persist_execution_state(
        str(native_database),
        json.dumps(
            {
                "account_id": account_id,
                "execution_id": execution_id,
                "is_success": 1,
                "created_at": "2026-10-30T00:00:00Z",
                "input_id": "replay-http-input",
                "result_input_id": "replay-http-result",
                "raw_input": {},
                "raw_result": {"status": "SUCCEEDED", "task_status": "SUCCEEDED", "symbol_results": {}},
            }
        ),
    )

    def no_channel(*args):
        raise AssertionError("historical replay must not inspect the current channel")

    monkeypatch.setattr(bridge, "get_channel", no_channel)
    recording_id, timestamp = "api-native-recording", 1_793_318_400_000_000
    context = {
        "schema_version": 1,
        "channel": "external-neutral",
        "instrument": {"X": {"tick_size": 1, "quantity_unit": "lot"}},
        "streams": {},
    }
    writer = core.ExecutionReplayWriter(str(bridge.replay_root()), account_id, recording_id, json.dumps(context))
    trade_key = f"neutral:{account_id}:X:T1"
    trade = {
        "execution_id": execution_id,
        "symbol": "X",
        "order_id": "A",
        "trade_key": trade_key,
        "identity_quality": "stable",
        "quantity": 2,
        "price": 100,
    }
    observations = [
        ("/execution", {"execution_id": execution_id, "phase": "start", "symbols": ["X"]}),
        ("/quote/X", {"symbol": "X", "bids": [{"price": 100, "quantity": 5}], "asks": []}),
        ("/trade", trade),
        ("/trade", trade | {"source": "reconciliation"}),
        ("/execution", {"execution_id": execution_id, "phase": "end"}),
    ]
    for index, (topic, data) in enumerate(observations, 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "recording_id": recording_id,
                    "capture_seq": index,
                    "time_us": timestamp + index,
                    "topic": topic,
                    "data": data,
                }
            )
        )
    writer.finish()
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        path = f"/account/executions/{execution_id}/replay"
        manifest = client.get(path)
        assert manifest.status_code == 200, manifest.text
        assert manifest.json()["account_id"] == account_id
        assert [item["recording_id"] for item in manifest.json()["recordings"]] == [recording_id]
        params = {"recording_id": recording_id, "start_us": timestamp + 4, "end_us": timestamp + 6, "symbols": "X"}
        response = client.get(path + "/window", params=params)
        assert response.status_code == 200, response.text
        window = response.json()
        assert window["recording_id"] == recording_id and window["complete"] is True
        assert [item["capture_seq"] for item in window["messages"]] == [4, 5]
        accounting = window["state_before_start"]["trade_accounting"]
        assert accounting["own_seen_keys"] == [trade_key]
        assert accounting["execution_totals"][execution_id]["by_symbol"]["X"]["quantity"] == 2
        assert client.get(path + "/window", params=params | {"recording_id": "foreign-recording"}).status_code == 404


@pytest.mark.parametrize("started_at, expected", [("2026-11-02T12:03:52.000269Z", 1793621032000269), (None, None)])
def test_manifest_positions_at_selected_execution_start(monkeypatch, tmp_path, started_at, expected):
    from types import SimpleNamespace

    async def status(_):
        return {"account_id": 7}

    async def intent(_):
        return SimpleNamespace(account_id=7, started_at=started_at, finished_at="2026-11-02T12:04:00.123456Z")

    monkeypatch.setattr(execution_replay, "get_execution_status", status)
    monkeypatch.setattr(execution_replay, "get_intent", intent)
    monkeypatch.setattr(execution_replay, "replay_root", lambda: tmp_path)
    monkeypatch.setattr(execution_replay.service, "replay_manifest", lambda *args: {"account_id": 7, "recordings": []})
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        response = client.get("/account/executions/selected/replay")
        assert response.status_code == 200
        assert response.json()["execution_start_us"] == expected
        assert response.json()["execution_end_us"] == 1793621040123456


def test_manifest_rejects_intent_from_another_account(monkeypatch, tmp_path):
    from types import SimpleNamespace

    async def status(_):
        return {"account_id": 7}

    async def intent(_):
        return SimpleNamespace(account_id=8, started_at=None)

    monkeypatch.setattr(execution_replay, "get_execution_status", status)
    monkeypatch.setattr(execution_replay, "get_intent", intent)
    monkeypatch.setattr(execution_replay, "replay_root", lambda: tmp_path)
    monkeypatch.setattr(execution_replay.service, "replay_manifest", lambda *args: {"account_id": 7, "recordings": []})
    app = FastAPI()
    app.include_router(execution_replay.router, prefix="/account")
    with TestClient(app) as client:
        assert client.get("/account/executions/selected/replay").status_code == 409
