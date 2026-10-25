"""GM 健康检查和业务请求不能隐式登录或重建 SDK。"""

from types import SimpleNamespace

import pytest

from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig


def executor():
    return GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )


def test_deferred_constructor_and_repeated_health_checks_never_start_sdk(monkeypatch):
    monkeypatch.setattr(GMExecutor, "_ensure_strategy_bridge", lambda *args, **kwargs: pytest.fail("不应启动 SDK"))
    instance = executor()
    for _ in range(3):
        assert instance._verify_connection() is False


def test_health_requires_live_runtime_and_ready_trading_account():
    instance = executor()
    ready = [True]
    running = [True]
    instance._strategy_bridge = SimpleNamespace(is_running=lambda: running[0], is_ready=lambda: ready[0])
    assert instance._verify_connection()
    ready[0] = False
    assert not instance._verify_connection()
    ready[0], running[0] = True, False
    assert not instance._verify_connection()


def test_business_call_and_algorithm_subscription_do_not_reconnect(monkeypatch):
    instance = executor()
    monkeypatch.setattr(instance, "_ensure_strategy_bridge", lambda *args, **kwargs: pytest.fail("业务请求不能重连"))
    with pytest.raises(ConnectionError, match="未提交"):
        instance._call_bridge("get_unfinished_orders")
    with pytest.raises(ConnectionError):
        instance.initialize_websocket(["518880.SH"])


def test_native_disconnect_and_login_restore_ready_without_restarting_runtime(monkeypatch):
    from threading import Event
    from unittest.mock import Mock

    from axile.executor.gm.core import gm_strategy

    ready = Event()
    ready.set()
    context = SimpleNamespace(
        account_id="test",
        connection_monitor=Mock(),
        strategy_initialized=True,
        account_was_ready=True,
        account_checked=True,
        startup_failed=False,
        ready_event=ready,
        stats={},
        stop_event=Event(),
    )
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_emit_runtime_log", lambda *args: None)
    monkeypatch.setattr(gm_strategy, "_update_startup_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(gm_strategy, "report_gm_failure", lambda *args, **kwargs: None)
    gm_strategy.on_trade_data_disconnected(None)
    assert not ready.is_set()
    assert context.stats["connection_revision"] == 1
    gm_strategy.on_trade_data_connected(None)
    assert context.account_checked is False
    assert not ready.is_set()
    gm_strategy.on_account_status(
        None,
        {
            "account_id": "test",
            "status": {"state": gm_strategy.AccConnectionState.LOGGEDIN},
        },
    )
    assert ready.is_set()
    assert context.stats["connection_revision"] == 1
    assert not context.stop_event.is_set()
    assert not context.startup_failed
