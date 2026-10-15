"""独立委托管道在业务阻塞、响应超时后的回归。"""

import asyncio
from multiprocessing import Pipe
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from axile.server.execution.worker_backend.manager import WorkerBackendManager
from axile.server.execution.worker_backend.orders import OrdersChannel
from tests.unit.server._execution_test_support import build_account


def test_orders_channel_works_while_business_request_lock_is_held(monkeypatch):
    parent, child = Pipe()
    channel = OrdersChannel(parent)
    commands = []

    def respond():
        request = child.recv()
        commands.append(request)
        child.send({"request_id": request["request_id"], "result": {"data": []}})

    thread = Thread(target=respond)
    thread.start()
    busy = Lock()
    busy.acquire()
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_get_or_create_worker", lambda _: SimpleNamespace(orders=channel, request_lock=busy))

    def bind(_handle, request):
        request.connection_binding = {"account_id": 1, "config_key": "current-config", "instance_id": "current-worker"}

    monkeypatch.setattr(manager, "_bind_connection_request", bind)
    try:
        response = asyncio.run(manager.order_command(build_account(), {"command": "query"}))
        assert response == {"data": []}
        assert busy.locked()
        assert len(commands) == 1
        assert commands[0]["connection_binding"]["instance_id"] == "current-worker"
    finally:
        busy.release()
        channel.close()
        child.close()
        thread.join(timeout=1)


def test_channel_timeout_drains_old_response_before_next_request_without_replaying_cancel():
    parent, child = Pipe()
    channel = OrdersChannel(parent)
    received = Event()
    release = Event()
    commands = []

    def respond():
        old = child.recv()
        commands.append(old["command"])
        received.set()
        release.wait(2)
        child.send({"request_id": old["request_id"], "result": {"state": "submitted"}})
        new = child.recv()
        commands.append(new["command"])
        child.send({"request_id": new["request_id"], "result": {"data": []}})

    thread = Thread(target=respond)
    thread.start()
    try:
        with pytest.raises(TimeoutError):
            channel.request({"command": "cancel"}, timeout=0.01)
        assert received.wait(1)
        release.set()
        assert channel.request({"command": "query"}, timeout=1) == {"data": []}
        assert commands == ["cancel", "query"]
    finally:
        release.set()
        channel.close()
        child.close()
        thread.join(timeout=2)


@pytest.mark.parametrize("activity", ["idle", "request", "running", "queued"])
def test_order_timeout_retires_only_idle_worker(monkeypatch, activity):
    from axile.server import connection_state
    from axile.server.execution import registry

    manager = WorkerBackendManager()
    handle = SimpleNamespace(account_id=1, request_lock=Lock(), connection_binding=None)
    ended, retired = [], []
    monkeypatch.setattr(connection_state, "end_connection_instance", lambda *args: ended.append(args))
    monkeypatch.setattr(manager, "_force_drop_worker", lambda *args: retired.append(args))
    monkeypatch.setattr(registry, "get_running_execution_id", lambda _: "active" if activity == "running" else None)
    monkeypatch.setattr(registry, "get_queued_execution_id", lambda _: "queued" if activity == "queued" else None)
    if activity == "request":
        handle.request_lock.acquire()
    try:
        manager._retire_idle_order_timeout(handle, TimeoutError("result unknown"))
        assert bool(retired) == (activity == "idle")
        assert bool(ended) == (activity == "idle")
        assert handle.request_lock.locked() == (activity == "request")
    finally:
        if handle.request_lock.locked():
            handle.request_lock.release()
        manager.close()


@pytest.mark.parametrize("status,budget", [("uninitialized", 90), ("connecting", 90), ("ready", 30)])
def test_query_timeout_budget_distinguishes_cold_preparation(monkeypatch, status, budget):
    from axile.server.db.models.account_connection import AccountConnectionPublic

    monkeypatch.setattr(
        WorkerBackendManager, "_current_session", staticmethod(lambda _: AccountConnectionPublic(status=status))
    )
    assert WorkerBackendManager._query_timeout(build_account()) == budget
