"""合并目录管道后保留初始化进度、失败清理与 worker 重启边界。"""

import multiprocessing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.executor.instrument_catalog import InstrumentCatalog
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.resident_connection import disconnect_resident_executor, executor_config_signature
from axile.server.execution.worker_backend import worker as worker_entry
from axile.server.execution.worker_backend import worker_state
from axile.server.execution.worker_backend.catalog import CatalogSession
from axile.server.execution.worker_backend.manager import WorkerBackendManager, WorkerBackendTimeoutError
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendErrorPayload,
    WorkerBackendRequest,
    WorkerBackendResponse,
)
from tests.unit.server._execution_test_support import build_account

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("release_fails", [False, True])
def test_initialization_error_survives_catalog_end_and_explicit_cleanup_failures(monkeypatch, release_fails):
    original = RuntimeError("initialization failed")
    provider = Mock()
    provider.end.side_effect = BrokenPipeError("end failed")
    close = Mock(side_effect=RuntimeError("native close failed") if release_fails else None)
    executor = SimpleNamespace(_verify_connection=lambda: False, set_instrument_catalog_provider=Mock(), close=close)
    account = AccountSnapshot.model_validate(build_account().model_dump())
    state = worker_state._WorkerBackendState(
        executor=executor,
        account_id=account.id,
        config_signature=executor_config_signature(account),
        catalog_provider=provider,
    )
    monkeypatch.setattr(worker_state, "_prepare_executor", Mock())
    monkeypatch.setattr(worker_state, "initialize_executor_instance", Mock(side_effect=original))
    monkeypatch.setattr(worker_state, "_finalize_executor", Mock(side_effect=RuntimeError("audit flush failed")))
    with pytest.raises(RuntimeError) as caught:
        worker_state._resolve_prepared_executor(state=state, account=account, execution_id="test", audit_context={})
    assert caught.value is original
    assert state.connection_error is original
    provider.begin.assert_called_once()
    provider.end.assert_called_once()
    close.assert_not_called()
    with pytest.raises(RuntimeError, match="native close failed" if release_fails else "audit flush failed"):
        worker_state._close_state_executor(state)
    close.assert_called_once()
    assert state.requires_worker_restart
    assert state.connection_error is original


def test_close_without_original_error_reports_audit_failure_after_releasing(monkeypatch):
    original = RuntimeError("audit flush failed")
    executor = SimpleNamespace(close=Mock())
    monkeypatch.setattr(worker_state, "_finalize_executor", Mock(side_effect=original))
    with pytest.raises(RuntimeError) as caught:
        worker_state._close_executor(executor)
    assert caught.value is original
    executor.close.assert_called_once()


@pytest.mark.parametrize("reason", ["catalog", "cleanup"])
def test_worker_loop_reports_restart_separately_and_exits(monkeypatch, reason):
    request = WorkerBackendRequest("request", "prepare", {}, None, {})
    original = WorkerBackendErrorPayload("timeout_error", "original query timeout")
    connection = Mock()
    connection.recv.side_effect = [request, AssertionError("must not accept another request")]
    control = Mock()
    control.recv.side_effect = EOFError()
    catalog_connection = Mock()
    catalog = SimpleNamespace(failed=reason == "catalog", request_id="", stop=Mock())
    monkeypatch.setattr(worker_entry, "RemoteCatalogProvider", lambda _: catalog)

    def handle(req, state):
        state.requires_worker_restart = reason == "cleanup"
        return WorkerBackendResponse(req.request_id, "error", error=original)

    monkeypatch.setattr(worker_entry, "_handle_worker_request", handle)
    worker_entry.run_worker_backend_loop(connection, 1, control, catalog_connection)
    connection.send.assert_called_once()
    response = connection.send.call_args.args[0]
    assert response.error is original
    assert response.requires_worker_restart
    assert connection.recv.call_count == 1


def test_catalog_end_failure_retains_executor_until_explicit_disconnect(monkeypatch):
    provider = Mock()
    original = BrokenPipeError("end failed")
    provider.end.side_effect = original
    executor = SimpleNamespace(_verify_connection=lambda: False, set_instrument_catalog_provider=Mock())
    account = AccountSnapshot.model_validate(build_account().model_dump())
    state = worker_state._WorkerBackendState(
        executor=executor,
        account_id=account.id,
        config_signature=executor_config_signature(account),
        catalog_provider=provider,
    )
    monkeypatch.setattr(worker_state, "_prepare_executor", Mock())
    initialize = Mock()
    close = Mock()
    monkeypatch.setattr(worker_state, "initialize_executor_instance", initialize)
    monkeypatch.setattr(worker_state, "_close_executor", close)
    with pytest.raises(BrokenPipeError) as caught:
        worker_state._resolve_prepared_executor(state=state, account=account, execution_id="test", audit_context={})
    assert caught.value is original
    initialize.assert_called_once_with(executor)
    provider.begin.assert_called_once()
    provider.end.assert_called_once()
    assert state.executor is executor
    assert state.connection_error is original
    close.assert_not_called()
    disconnect_resident_executor(state, close=lambda _executor: worker_state._close_state_executor(state))
    close.assert_called_once_with(executor)
    assert state.executor is None
    assert state.disconnected


def test_initialization_progress_extends_only_current_request():
    parent, child = multiprocessing.Pipe()
    session = CatalogSession(parent, InstrumentCatalog())
    handle = SimpleNamespace(catalog=session)
    request = SimpleNamespace(request_id="current")
    try:
        session.request_id = "current"
        session.active = True
        session.started = 5
        session.updated = 100
        remaining = WorkerBackendManager._remaining_response_time(handle, request, 30, 120)
        assert remaining == 40
        with pytest.raises(WorkerBackendTimeoutError, match="无进展"):
            WorkerBackendManager._remaining_response_time(handle, request, 30, 160)
        session.active = False
        session.finished = 125
        assert WorkerBackendManager._remaining_response_time(handle, request, 30, 130) == 20
        request.request_id = "next"
        assert WorkerBackendManager._remaining_response_time(handle, request, 150, 130) == 20
    finally:
        session.close()
        child.close()
