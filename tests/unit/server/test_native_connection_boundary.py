"""连接持久化边界与只读订阅，SQLite 原子语义在 Rust 模块测试。"""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.executor.connection_state import ConnectionObservation
from axile.server import connection_events, connection_runtime, connection_state, orders_live
from axile.server.db.models.account import Account
from axile.server.db.models.account_connection import AccountConnectionState
from axile.server.db.models.account_order import AccountOrdersPublic


@pytest.fixture(autouse=True)
def isolate_runtime():
    connection_runtime.clear_sessions()
    yield
    connection_runtime.clear_sessions()


def account():
    return Account(
        id=1,
        name="connection-boundary",
        market="期货",
        trade_channel="ctp",
        brokerage="ctp",
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        account_config={"password": "private-password"},
        feishu_key="private-webhook",
    )


def binding(value=None):
    value = value or account()
    return connection_state.ConnectionBinding(
        value.id, connection_state.connection_config_key(value.trade_channel, value.account_config), "worker"
    )


def test_worker_collector_keeps_history_in_memory_and_main_service_crosses_once(monkeypatch):
    calls = []

    def native(name, request):
        calls.append((name, request))
        return {"applied": True}

    monkeypatch.setattr(connection_state, "native_operation_sync", native)
    current = binding()
    collector = connection_state.ConnectionCollector(current)
    collector.observe(ConnectionObservation("error", "failure", "original", {"technical_detail": "evidence"}))
    collector.observe(ConnectionObservation("ok", "access", kind="access"))
    collector.observe_session(ConnectionObservation("ok", "ready"))
    assert calls == []
    connection_runtime.register_session(current, lambda: True)
    connection_state.consume_connection_snapshot(collector.snapshot())
    assert len(calls) == 1 and calls[0][0] == "connection_observe"
    request = calls[0][1]
    assert request["binding"] == asdict(current)
    assert request["history"]["last_success_at"] == "access"
    assert request["history"]["last_failure"]["error"]["technical_detail"] == "evidence"
    assert connection_runtime.current_session(1, current.config_key).status == "ready"
    assert "private-password" not in str(request)


def test_begin_returns_committed_binding_and_no_binding_after_rejection(monkeypatch):
    value = account()
    calls = []

    def native(name, request):
        calls.append((name, request))
        return request | {"manually_disconnected": True}

    monkeypatch.setattr(connection_state, "native_operation_sync", native)
    current = connection_state.begin_connection_instance(value)
    assert current.manually_disconnected
    assert current.instance_id and calls[0][0] == "connection_begin"
    assert "private-password" not in str(calls)
    monkeypatch.setattr(connection_state, "native_operation_sync", lambda *args: None)
    assert connection_state.begin_connection_instance(value) is None
    assert connection_state.begin_connection_instance(SimpleNamespace(id=None)) is None


def test_rejected_observation_has_no_database_change_notification(monkeypatch):
    changed = []
    monkeypatch.setattr(connection_events.connection_hub, "changed", changed.append)
    monkeypatch.setattr(connection_state, "native_operation_sync", lambda *args: {"applied": False})
    connection_state.record_connection_observation(binding(), 1, ConnectionObservation("ok", "late"))
    assert changed == []


def test_end_revokes_runtime_before_native_failure_and_retains_original_evidence(monkeypatch):
    current = binding()
    connection_runtime.register_session(current, lambda: True)
    calls = []

    def native(name, request):
        calls.append((name, request))
        assert not connection_runtime.matches_session(current)
        raise OSError("database unavailable")

    monkeypatch.setattr(connection_state, "native_operation_sync", native)
    connection_state.end_connection_instance(current, "worker timeout", TimeoutError("original timeout"))
    assert connection_runtime.current_session(1, current.config_key).status == "unavailable"
    assert calls[0][0] == "connection_end"
    assert calls[0][1]["last_failure"]["error"]["technical_detail"] == "original timeout"


def test_recovery_clears_runtime_before_one_native_stage(monkeypatch):
    current = binding()
    connection_runtime.register_session(current, lambda: True)

    async def native(name, request):
        assert not connection_runtime.matches_session(current)
        assert (name, request) == ("connection_recover", {})
        return {"updated": 1}

    monkeypatch.setattr(connection_state, "native_operation", native)
    asyncio.run(connection_state.recover_connection_sessions())


def test_public_history_does_not_advertise_a_live_session_or_credentials():
    value = account()
    value.connection_state = AccountConnectionState(
        account_id=value.id,
        config_key=binding(value).config_key,
        status="ok",
        observed_at="old-success",
        last_success_at="last-access",
        manually_disconnected=True,
        last_failure={"observed_at": "failed", "message": "old failure", "error": {"technical_detail": "original"}},
    )
    detail = connection_state.public_connection(value, detail=True)
    assert detail.status == "disconnected" and not detail.worker_alive
    assert detail.last_success_at == "last-access" and detail.last_failure.message == "old failure"
    assert detail.error is None and detail.presentation is None
    assert "private-password" not in detail.model_dump_json()
    assert "private-webhook" not in detail.model_dump_json()
    value.account_config = {"password": "replacement"}
    assert connection_state.public_connection(value).status == "uninitialized"


def test_public_session_batch_is_detached_and_preserves_expiry_and_cooldown(monkeypatch):
    moment = [0.0]
    alive = [True]
    monkeypatch.setattr(connection_runtime, "clock_monotonic", lambda: moment[0])
    monkeypatch.setattr(connection_runtime, "clock_now_iso", lambda: "now")
    current = binding()
    connection_runtime.register_session(current, lambda: alive[0])
    connection_runtime.observe_session(
        current, 1, ConnectionObservation("cooldown", "failed", "original", {"retry_after_seconds": 30})
    )
    before = asdict(connection_runtime._sessions[1])
    version = connection_events.connection_hub.version
    views = connection_runtime.public_session_views()
    assert asdict(connection_runtime._sessions[1]) == before | {"view_status": "unavailable", "updated_at": "now"}
    assert connection_events.connection_hub.version == version
    assert views["1"]["config_key"] == current.config_key
    assert views["1"]["connection"]["status"] == "unavailable"
    assert views["1"]["connection"]["retry_after_seconds"] == 30
    assert "original" in str(views) and "private-password" not in str(views)
    views["1"]["connection"]["message"] = "caller changed"
    assert connection_runtime._sessions[1].observation.message == "original"
    moment[0] = 6
    assert connection_runtime.public_session_views()["1"]["connection"]["status"] == "unknown"
    alive[0] = False
    assert connection_runtime.public_session_views()["1"]["connection"]["status"] == "disconnected"


def test_connection_and_orders_streams_use_native_account_scan_and_filter_credentials(monkeypatch):
    value = account()
    scan = AsyncMock(side_effect=[[value.model_dump(mode="json")], [value.model_dump(mode="json")], []])
    monkeypatch.setattr(connection_events, "native_operation", scan)
    # orders_frames imports the shared native facade at its call boundary.
    from axile.server import account_orders, native_store

    monkeypatch.setattr(native_store, "native_operation", scan)
    query = AsyncMock(return_value=AccountOrdersPublic(data=[], count=0, complete=True, sampled_at="now"))
    monkeypatch.setattr(account_orders, "query_account_orders", query)
    connections = asyncio.run(connection_events.connection_frames({2, 1}))
    orders = asyncio.run(orders_live.orders_frames({1}))
    assert asyncio.run(connection_events.connection_frames(set())) == []
    assert scan.await_args_list[0].args == ("connection_accounts", {"account_ids": [1, 2]})
    assert scan.await_args_list[1].args == ("connection_accounts", {"account_ids": [1]})
    assert scan.await_args_list[2].args == ("connection_accounts", {"account_ids": []})
    assert connections[0]["connection"]["status"] == "uninitialized"
    assert orders[0]["snapshot"]["complete"]
    assert query.await_args.args[0].account_config == value.account_config
    assert "private-password" not in str(connections + orders)
    assert "private-webhook" not in str(connections + orders)
