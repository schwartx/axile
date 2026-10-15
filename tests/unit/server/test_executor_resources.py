"""账户 worker 的请求清理和最终连接释放。"""

import asyncio
from types import SimpleNamespace

import pytest

from axile.server import account_assets
from axile.server.execution.resources import close_executor
from axile.server.execution.worker_backend import worker, worker_state
from axile.server.execution.worker_backend.manager import WorkerBackendTimeoutError
from tests.unit.server._execution_test_support import build_account


@pytest.mark.parametrize("failure", [None, "flush", "guard", "runtime"])
def test_worker_finalization_clears_all_request_bindings_and_retains_connection(monkeypatch, failure):
    calls = []

    def action(name):
        calls.append(name)
        if name == failure:
            raise RuntimeError(name)

    async def flush(executor):
        action("flush")

    executor = SimpleNamespace(
        set_account_control_guard=lambda value: action("guard"),
        set_termination_controller=lambda value: action("termination"),
        set_audit_sink=lambda value: action("sink"),
        set_audit_context=lambda value: action("context"),
        clear_execution_runtime=lambda: action("runtime"),
        stop=lambda: pytest.fail("请求收尾不能关闭常驻连接"),
    )
    monkeypatch.setattr(worker_state, "flush_account_control_records", flush)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            worker_state._finalize_executor(executor)
    else:
        worker_state._finalize_executor(executor)
    assert calls == ["flush", "guard", "termination", "sink", "context", "runtime"]


def test_failed_request_cleanup_retires_worker_without_masking_result(monkeypatch):
    state = worker_state._WorkerBackendState(executor=object())

    def fail(executor):
        raise RuntimeError("flush failed")

    monkeypatch.setattr(worker, "_finalize_executor", fail)
    worker._finalize_with_orders(None, state)
    assert state.requires_worker_restart


def test_worker_closes_transport_when_finalization_fails(monkeypatch):
    calls = []

    def fail(executor):
        calls.append("finalize")
        raise RuntimeError("flush failed")

    monkeypatch.setattr(worker_state, "_finalize_executor", fail)
    executor = SimpleNamespace(stop=lambda: calls.append("stop"))
    with pytest.raises(RuntimeError, match="flush failed"):
        worker_state._close_executor(executor)
    assert calls == ["finalize", "stop"]


@pytest.mark.parametrize("failure", [False, True])
def test_asset_query_uses_worker_without_releasing_session(monkeypatch, failure):
    original = RuntimeError("asset query failed")
    calls = []

    class Manager:
        async def get_account_assets(self, account):
            calls.append(account.id)
            if failure:
                raise original
            return "assets"

    monkeypatch.setattr(account_assets, "get_worker_backend_manager", Manager)
    if failure:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(account_assets.query_account_assets(build_account(id=7)))
        assert caught.value is original
    else:
        assert asyncio.run(account_assets.query_account_assets(build_account(id=7))) == "assets"
    assert calls == [7]


def test_asset_timeout_preserves_public_error(monkeypatch):
    class Manager:
        async def get_account_assets(self, account):
            raise WorkerBackendTimeoutError("worker timeout")

    monkeypatch.setattr(account_assets, "get_worker_backend_manager", Manager)
    with pytest.raises(TimeoutError, match="账户资产查询超时"):
        asyncio.run(account_assets.query_account_assets(build_account()))


def test_close_falls_back_to_close_when_stop_is_unavailable():
    closed = []
    close_executor(SimpleNamespace(close=lambda: closed.append(True)))
    assert closed == [True]
