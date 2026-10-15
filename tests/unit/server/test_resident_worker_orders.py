"""独立委托通道共享常驻会话，断线后不会由页面轮询重新登录。"""

from contextlib import nullcontext

import pytest

from axile.common.trade_channel import TradeChannel
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.execution.worker_backend import orders, worker_state
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_resident_connection import Executor


def setup_worker(monkeypatch):
    account = build_account(id=7, trade_channel=TradeChannel.GM).snapshot()
    state = worker_state._WorkerBackendState()
    created, initialized, closed, commands = [], [], [], []

    def create(*args, **kwargs):
        assert kwargs == {"initialize": False}
        executor = Executor()
        created.append(executor)
        return executor

    def initialize(state, executor):
        initialized.append(executor)
        executor.ready = True

    monkeypatch.setattr(worker_state, "create_executor_instance", create)
    monkeypatch.setattr(worker_state, "_initialize_worker_connection", initialize)
    monkeypatch.setattr(worker_state, "_close_state_executor", lambda state: closed.append(state.executor))
    monkeypatch.setattr(worker_state, "_prepare_executor", lambda **kwargs: None)
    monkeypatch.setattr(orders, "order_control_scope", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        orders, "run_order_command", lambda executor, payload: commands.append(payload["command"]) or {"data": []}
    )
    payload = {"account": account.model_dump(mode="json"), "command": "query"}
    return account, state, payload, created, initialized, closed, commands


def test_cold_order_queries_wait_for_explicit_connection(monkeypatch):
    account, state, payload, created, initialized, closed, commands = setup_worker(monkeypatch)
    for _ in range(3):
        with pytest.raises(AccountConnectionUnavailable):
            orders._command(state, payload)
    assert not created and not initialized and not commands
    worker_state._resolve_prepared_executor(
        state=state, account=account, execution_id=None, audit_context={}, force_reconnect=True
    )
    for _ in range(3):
        assert orders._command(state, payload) == {"data": []}
    assert len(created) == len(initialized) == 1
    assert closed == []
    assert commands == ["query"] * 3


def test_broken_order_session_only_recovers_through_prepare(monkeypatch):
    account, state, payload, created, initialized, closed, commands = setup_worker(monkeypatch)
    worker_state._resolve_prepared_executor(
        state=state, account=account, execution_id=None, audit_context={}, force_reconnect=True
    )
    orders._command(state, payload)
    created[0].ready = False
    for _ in range(3):
        with pytest.raises(AccountConnectionUnavailable):
            orders._command(state, payload)
    assert commands == ["query"]
    assert len(created) == len(initialized) == 1
    worker_state._resolve_prepared_executor(
        state=state, account=account, execution_id=None, audit_context={}, force_reconnect=True
    )
    assert len(created) == len(initialized) == 2
    assert closed == [created[0]]
    orders._command(state, payload)
    assert commands == ["query", "query"]


def test_broken_session_is_never_replaced_while_execution_is_active(monkeypatch):
    account, state, payload, created, initialized, closed, commands = setup_worker(monkeypatch)
    worker_state._resolve_prepared_executor(
        state=state, account=account, execution_id=None, audit_context={}, force_reconnect=True
    )
    orders._command(state, payload)
    state.request_active = True
    created[0].ready = False
    with pytest.raises(AccountConnectionUnavailable, match="执行期间"):
        orders._command(state, payload)
    assert len(created) == len(initialized) == 1
    assert closed == []
    assert commands == ["query"]


def test_initial_failure_is_not_replayed_by_polling(monkeypatch):
    account, state, payload, created, initialized, _, commands = setup_worker(monkeypatch)

    def fail(state, executor):
        initialized.append(executor)
        raise TimeoutError("GetEncryptedToken timeout")

    monkeypatch.setattr(worker_state, "_initialize_worker_connection", fail)
    with pytest.raises(TimeoutError):
        worker_state._resolve_prepared_executor(
            state=state, account=account, execution_id=None, audit_context={}, force_reconnect=True
        )
    for _ in range(3):
        with pytest.raises(AccountConnectionUnavailable):
            orders._command(state, payload)
    assert len(created) == len(initialized) == 1
    assert commands == []


def test_manual_disconnect_from_previous_service_blocks_cold_queries_and_preparation(monkeypatch):
    from dataclasses import asdict

    from axile.server.connection_state import ConnectionBinding, connection_config_key

    account, state, payload, created, initialized, _, commands = setup_worker(monkeypatch)
    payload["connection_binding"] = asdict(
        ConnectionBinding(
            7,
            connection_config_key(account.trade_channel, account.account_config),
            "new-service",
            True,
        )
    )
    with pytest.raises(AccountConnectionUnavailable, match="主动断开"):
        orders._command(state, payload)
    with pytest.raises(AccountConnectionUnavailable, match="主动断开"):
        worker_state._resolve_prepared_executor(state=state, account=account, execution_id=None, audit_context={})
    assert not created and not initialized and not commands
    worker_state._resolve_prepared_executor(
        state=state, account=account, execution_id=None, audit_context={}, force_reconnect=True
    )
    assert len(created) == len(initialized) == 1
    assert not state.disconnected
