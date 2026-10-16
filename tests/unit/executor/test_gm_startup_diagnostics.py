"""启动初始快照不抢先归因，SDK 明确错误可跨线程传播。"""

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gm.api._errors import GmError

from axile.executor.connection_state import ConnectionMonitor
from axile.executor.gm.common import AccConnectionState
from axile.executor.gm.connection import GMAccountNotFoundError, GMAccountStartupError
from axile.executor.gm.core import gm_strategy
from axile.executor.gm.core.strategy_bridge import GMAuthenticationError, GMStrategyBridge, _normalize_startup_error


@pytest.fixture
def runtime(monkeypatch):
    observations, failures = [], []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    context = SimpleNamespace(
        account_id="test-account",
        ready_event=Event(),
        stop_event=Event(),
        connection_monitor=monitor,
        strategy_initialized=True,
        account_was_ready=False,
        account_checked=False,
        startup_failed=False,
        startup_state={},
        stats={"errors": 0},
        startup_failure_handler=failures.append,
    )
    stop_timer, stop_runtime = Mock(), Mock()
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_emit_runtime_log", lambda *args: None)
    monkeypatch.setattr(gm_strategy, "_update_startup_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(gm_strategy, "_stop_bridge_timer", stop_timer)
    monkeypatch.setattr(gm_strategy, "_request_soft_runtime_stop", stop_runtime)
    query = Mock(return_value={"state": AccConnectionState.DISCONNECTED})
    monkeypatch.setattr(gm_strategy.GMApiBridge, "get_account_status", query)
    return context, observations, failures, query, stop_timer, stop_runtime


def test_initial_disconnected_snapshot_waits_for_login_callback_without_requerying(runtime):
    context, _, failures, query, stop_timer, stop_runtime = runtime
    assert not gm_strategy._check_account_startup(context)
    assert not gm_strategy._check_account_startup(context)
    query.assert_called_once_with("test-account")
    assert context.startup_state["account_state"] == 5
    assert failures == []
    gm_strategy.on_account_status(None, {"account_id": "test-account", "status": {"state": 5}})
    assert failures == []
    gm_strategy.on_account_status(None, {"account_id": "test-account", "status": {"state": 3}})
    assert context.ready_event.is_set()
    assert context.account_was_ready
    stop_timer.assert_not_called()
    stop_runtime.assert_not_called()


def test_missing_account_status_reports_account_not_found(runtime):
    context, observations, failures, query, _, _ = runtime
    query.return_value = None
    assert not gm_strategy._check_account_startup(context)
    assert isinstance(failures[0], GMAccountNotFoundError)
    assert observations[-1].label == "账户不存在"
    assert _normalize_startup_error(failures[0]).execution_error == "账户不存在"


@pytest.mark.parametrize(("code", "label"), [("1000", "Token 无效"), ("1026", "令牌更新失败")])
def test_sdk_callback_supplies_real_failure_after_disconnected_snapshot(runtime, code, label):
    context, observations, failures, _, stop_timer, stop_runtime = runtime
    assert not gm_strategy._check_account_startup(context)
    gm_strategy.on_error(None, code, "SDK 原始错误".encode())
    assert context.startup_failed
    assert not context.ready_event.is_set()
    assert len(failures) == 1
    error = failures[0]
    assert isinstance(error, GmError)
    assert error.status == int(code)
    assert error.message == "SDK 原始错误"
    wrapped = _normalize_startup_error(error)
    assert isinstance(wrapped, GMAuthenticationError) == (code == "1000")
    context.connection_monitor.failed(wrapped)
    assert observations[-1].label == label
    assert observations[-1].error["status"] == int(code)
    assert "SDK 原始错误" in observations[-1].error["technical_detail"]
    assert "具体原因未确认" not in observations[-1].message
    if code == "1000":
        assert observations[-1].message == "GM token 无效或已失效"
    stop_timer.assert_called_once()
    stop_runtime.assert_called_once()


@pytest.mark.parametrize("status", [1000, 1026])
def test_sdk_query_error_is_not_downgraded_to_generic_account_read_failure(runtime, status):
    context, observations, failures, query, _, _ = runtime
    query.side_effect = GmError(status, "SDK 查询错误", "get_account_status")
    assert not gm_strategy._check_account_startup(context)
    assert failures[0].status == status
    assert observations[-1].label == ("Token 无效" if status == 1000 else "令牌更新失败")


@pytest.mark.parametrize("code", ["1011", "invalid-code"])
def test_other_callbacks_do_not_become_authentication_failures(runtime, code):
    context, observations, failures, _, _, stop_runtime = runtime
    gm_strategy.on_error(None, code, "invalid token text without authentication code")
    assert not context.startup_failed
    assert failures == []
    assert not observations
    stop_runtime.assert_not_called()


def test_callback_failure_after_ready_blocks_access_without_restarting_runtime(runtime):
    context, observations, failures, _, _, stop_runtime = runtime
    context.account_was_ready = True
    context.ready_event.set()
    gm_strategy.on_error(None, "1026", "更新令牌错误")
    assert not context.ready_event.is_set()
    assert observations[-1].label == "令牌更新失败"
    assert context.stats["connection_revision"] == 1
    assert not context.startup_failed
    assert failures == []
    stop_runtime.assert_not_called()


def test_explicit_account_error_keeps_known_state_without_claiming_invalid_token(runtime):
    context, observations, failures, query, _, _ = runtime
    query.return_value = {"state": AccConnectionState.ERROR}
    assert not gm_strategy._check_account_startup(context)
    assert isinstance(failures[0], GMAccountStartupError)
    wrapped = _normalize_startup_error(failures[0])
    assert "状态码 6" in wrapped.execution_error
    assert "SDK 未提供失败原因" in wrapped.execution_error
    assert "Token" not in wrapped.execution_error
    context.connection_monitor.failed(wrapped)
    assert observations[-1].label == "账户状态异常"


def test_shutdown_exception_does_not_replace_callback_authentication_failure(monkeypatch):
    bridge = GMStrategyBridge(token="synthetic", account_id="synthetic", callback_dispatcher=Mock())
    error = GmError(1000, "invalid token from callback", "on_error")

    def execute(_strategy_file):
        bridge._account_startup_failed(error)
        raise RuntimeError("shutdown failure")

    monkeypatch.setattr(bridge, "_execute_strategy", execute)
    bridge._run_strategy_loop()
    assert bridge._startup_error is error
    assert isinstance(_normalize_startup_error(bridge._startup_error), GMAuthenticationError)


def test_disconnected_without_sdk_error_uses_existing_deadline_and_reports_last_state(monkeypatch):
    from axile.executor.gm.core import strategy_bridge

    bridge = GMStrategyBridge(token="synthetic", account_id="synthetic", callback_dispatcher=Mock())
    observations = []
    bridge.connection_monitor.bind(observations.append)
    bridge._startup_state["account_state"] = 5
    thread = SimpleNamespace(start=lambda: None, is_alive=lambda: False)
    monkeypatch.setattr(strategy_bridge.threading, "Thread", lambda **kwargs: thread)
    monkeypatch.setattr(bridge, "_wait_for_startup", lambda timeout: None)
    monkeypatch.setattr(bridge, "stop", lambda: None)
    assert bridge.start(timeout=0) is False
    assert isinstance(bridge._startup_error, TimeoutError)
    assert observations[-1].label == "连接超时"
    assert "最后状态为已断开" in observations[-1].message
    assert "SDK 未提供明确失败原因" in observations[-1].message
    assert "Token 无效" not in observations[-1].message
