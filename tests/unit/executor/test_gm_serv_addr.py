"""GM serv_addr 直连模式测试。"""

from __future__ import annotations

import queue
import sys
import threading
import time
from types import ModuleType
from typing import Any

import pytest


def _install_gm_stubs() -> None:
    gm_module = sys.modules.setdefault("gm", ModuleType("gm"))
    api_module = sys.modules.setdefault("gm.api", ModuleType("gm.api"))
    basic_module = sys.modules.setdefault("gm.api.basic", ModuleType("gm.api.basic"))
    csdk_module = sys.modules.setdefault("gm.csdk", ModuleType("gm.csdk"))
    c_sdk_module = sys.modules.setdefault("gm.csdk.c_sdk", ModuleType("gm.csdk.c_sdk"))
    model_module = sys.modules.setdefault("gm.model", ModuleType("gm.model"))
    storage_module = sys.modules.setdefault("gm.model.storage", ModuleType("gm.model.storage"))
    pb_module = sys.modules.setdefault("gm.pb", ModuleType("gm.pb"))
    account_pb2_module = sys.modules.setdefault("gm.pb.account_pb2", ModuleType("gm.pb.account_pb2"))
    tradegw_pb2_module = sys.modules.setdefault(
        "gm.pb.tradegw_service_pb2",
        ModuleType("gm.pb.tradegw_service_pb2"),
    )

    api_module.MODE_LIVE = 1
    api_module.OrderSide_Buy = 1
    api_module.OrderSide_Sell = 2
    api_module.OrderType_Limit = 1
    api_module.OrderType_Market = 2
    api_module.PositionEffect_Close = 2
    api_module.PositionEffect_Open = 1
    api_module.current = lambda *_args, **_kwargs: None
    api_module.get_cash = lambda *_args, **_kwargs: None
    api_module.get_execution_reports = lambda *_args, **_kwargs: []
    api_module.get_orders = lambda *_args, **_kwargs: []
    api_module.get_position = lambda *_args, **_kwargs: None
    api_module.get_unfinished_orders = lambda *_args, **_kwargs: []
    api_module.order_cancel = lambda *_args, **_kwargs: None
    api_module.order_volume = lambda *_args, **_kwargs: []
    api_module.run = lambda **_kwargs: None
    api_module.set_account_id = lambda *_args, **_kwargs: None
    api_module.set_serv_addr = lambda *_args, **_kwargs: None
    api_module.set_token = lambda *_args, **_kwargs: None
    api_module.subscribe = lambda *_args, **_kwargs: None
    api_module.timer = lambda *_args, **_kwargs: {"timer_id": 10001, "status": 0}
    api_module.timer_stop = lambda *_args, **_kwargs: True
    basic_module.running = True
    basic_module._py_gmi_unsubscribe_all = lambda: None

    c_sdk_module.c_status_fail = lambda *_args, **_kwargs: False
    c_sdk_module.py_gmi_get_account_status = lambda *_args, **_kwargs: (0, None)
    c_sdk_module.TickLikeDict2 = type("TickLikeDict2", (), {})

    class DictLikeAccountStatus(dict[str, Any]):
        pass

    class DictLikeExecRpt(dict[str, Any]):
        pass

    class DictLikeOrder(dict[str, Any]):
        pass

    class Context:
        pass

    class AccountStatuses:
        data: list[object] = []

        def ParseFromString(self, _result: bytes) -> None:
            return None

    class GetAccountStatusesReq:
        def __init__(self) -> None:
            self.account_ids: list[str] = []

        def SerializeToString(self) -> bytes:
            return b""

    account_pb2_module.AccountStatuses = AccountStatuses
    tradegw_pb2_module.GetAccountStatusesReq = GetAccountStatusesReq

    gm_module.api = api_module
    api_module.basic = basic_module
    gm_module.csdk = csdk_module
    gm_module.model = model_module
    gm_module.pb = pb_module
    csdk_module.c_sdk = c_sdk_module
    model_module.DictLikeAccountStatus = DictLikeAccountStatus
    model_module.DictLikeExecRpt = DictLikeExecRpt
    model_module.DictLikeOrder = DictLikeOrder
    model_module.storage = storage_module
    storage_module.Context = Context
    pb_module.account_pb2 = account_pb2_module
    pb_module.tradegw_service_pb2 = tradegw_pb2_module


_install_gm_stubs()

from axile.executor.gm import gm_execute as gm_execute_module
from axile.executor.gm.core import api_bridge as gm_api_bridge_module
from axile.executor.gm.core import gm_strategy as gm_strategy_module
from axile.executor.gm.core.bridge_context import (
    clear_gm_strategy_runtime_context,
    get_gm_strategy_runtime_context,
    install_gm_strategy_runtime_context,
)
from axile.executor.gm.core.strategy_bridge import GMAuthenticationError, GMStrategyBridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig


def _build_executor() -> GMExecutor:
    executor = GMExecutor.__new__(GMExecutor)
    executor.account_id = None
    executor.just_started = False
    executor._callback_dispatcher = object()
    executor._strategy_bridge = None
    executor._bridge_lease = None
    executor._callback_monitoring = False
    executor._gm_config = None
    executor._subscribe_symbols = []
    return executor


def test_initialize_connection_uses_serv_addr_without_starting_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serv_addr 模式下初始化仅保存配置，不应直调 gm.api 或拉起终端。"""
    executor = _build_executor()
    config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    start_calls: list[str] = []

    monkeypatch.setattr(
        executor,
        "_start_gm_desktop_if_not",
        lambda terminal_path: start_calls.append(terminal_path) or True,
    )

    executor._initialize_connection(config)

    assert start_calls == []
    assert executor.just_started is False
    assert executor.account_id == "account-id"
    assert executor._gm_config == config


def test_initialize_connection_keeps_terminal_path_flow_when_serv_addr_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """terminal_path 模式下仅保留本地终端启动，不再直调 gm.api。"""
    executor = _build_executor()
    config = GMAccountConfig(
        connection_mode="terminal",
        account_id="account-id",
        token="token",
        terminal_path="C:\\GM",
    )
    monkeypatch.setattr(
        executor,
        "_start_gm_desktop_if_not",
        lambda terminal_path: terminal_path == "C:\\GM",
    )

    executor._initialize_connection(config)

    assert executor.just_started is True


def test_verify_connection_skips_status_rpc_in_serv_addr_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """bridge-only 模式下连接校验应走 bridge，而不是账户状态 RPC。"""
    executor = _build_executor()
    executor._gm_config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    bridge_calls: list[float] = []

    monkeypatch.setattr(
        executor,
        "_ensure_strategy_bridge",
        lambda *, timeout=30.0, subscribe_symbols=None: bridge_calls.append(timeout) or object(),
    )

    assert executor._verify_connection() is True
    assert bridge_calls == [30.0]


def test_verify_connection_uses_full_bridge_startup_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行前连接校验不应比 bridge 正常启动路径更苛刻。"""
    executor = _build_executor()
    executor._gm_config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    bridge_calls: list[float] = []

    monkeypatch.setattr(
        executor,
        "_ensure_strategy_bridge",
        lambda *, timeout=30.0, subscribe_symbols=None: bridge_calls.append(timeout) or object(),
    )

    assert executor._verify_connection() is True
    assert bridge_calls == [30.0]


def test_strategy_bridge_execute_strategy_passes_serv_addr_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StrategyBridge 在 serv_addr 模式下应显式传入服务地址。"""
    api_module = sys.modules["gm.api"]
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(api_module, "set_token", lambda token: calls.append(("set_token", token)))
    monkeypatch.setattr(api_module, "set_serv_addr", lambda addr: calls.append(("set_serv_addr", addr)))
    monkeypatch.setattr(api_module, "run", lambda **kwargs: calls.append(("run", kwargs)))

    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
        serv_addr="127.0.0.1:7001",
    )

    bridge._execute_strategy("gm_strategy.py")

    assert calls[0] == ("set_token", "token")
    assert calls[1] == ("set_serv_addr", "127.0.0.1:7001")
    assert calls[2][0] == "run"
    assert calls[2][1]["serv_addr"] == "127.0.0.1:7001"


def test_strategy_bridge_execute_strategy_omits_serv_addr_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置 serv_addr 时，StrategyBridge 应保持原有 run 参数。"""
    api_module = sys.modules["gm.api"]
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(api_module, "set_token", lambda token: calls.append(("set_token", token)))
    monkeypatch.setattr(api_module, "set_serv_addr", lambda addr: calls.append(("set_serv_addr", addr)))
    monkeypatch.setattr(api_module, "run", lambda **kwargs: calls.append(("run", kwargs)))

    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )

    bridge._execute_strategy("gm_strategy.py")

    assert calls[0] == ("set_token", "token")
    assert len(calls) == 2
    assert calls[1][0] == "run"
    assert set(calls[1][1]) == {"strategy_id", "filename", "mode", "token"}
    assert calls[1][1]["filename"] == "gm_strategy.py"
    assert calls[1][1]["token"] == "token"


def test_strategy_bridge_execute_strategy_resets_gm_running_flag_before_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """软停后再次启动前，应先把 gm.api.basic.running 复位为 True。"""
    api_module = sys.modules["gm.api"]
    basic_module = sys.modules["gm.api.basic"]
    run_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(api_module, "set_token", lambda token: None)
    monkeypatch.setattr(api_module, "run", lambda **kwargs: run_calls.append(kwargs))
    monkeypatch.setattr(basic_module, "running", False, raising=False)

    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )

    bridge._execute_strategy("gm_strategy.py")

    assert basic_module.running is True
    assert len(run_calls) == 1


def test_strategy_bridge_execute_strategy_clears_runtime_context_after_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """策略文件执行完成后应清理共享 runtime context。"""
    api_module = sys.modules["gm.api"]
    monkeypatch.setattr(api_module, "set_token", lambda token: None)
    monkeypatch.setattr(api_module, "run", lambda **kwargs: None)

    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )

    bridge._execute_strategy("gm_strategy.py")

    assert get_gm_strategy_runtime_context() is None


def test_strategy_bridge_call_round_trips_request_result() -> None:
    """bridge.call() 应通过请求队列等待 bridge 线程返回结果。"""
    from axile.executor.gm.core.api_bridge import GetCashRequest

    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )
    bridge._running = True

    def _worker() -> None:
        request = bridge._request_queue.get(timeout=1.0)
        assert isinstance(request.request, GetCashRequest)
        assert request.request.account_id == "account-id"
        request.future.set_result({"available": 123.0})

    worker = threading.Thread(target=_worker)
    worker.start()
    try:
        result = bridge.call(GetCashRequest(account_id="account-id"), timeout=1.0)
    finally:
        worker.join(timeout=1.0)

    assert result == {"available": 123.0}


def test_strategy_bridge_request_symbols_uses_bridge_call_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from axile.executor.gm.core.api_bridge import GMSubscribeSymbolsRequest

    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
        subscribe_symbols=["SHSE.600000"],
    )
    bridge._running = True
    submit_args: list[tuple[object, float | None]] = []

    monkeypatch.setattr(
        bridge,
        "_submit_request",
        lambda request, timeout=30.0: submit_args.append((request, timeout)) or ["SZSE.000001"],
    )

    added_symbols = bridge.request_symbols(["SHSE.600000", "SZSE.000001"], timeout=7.0)

    assert added_symbols == ["SZSE.000001"]
    assert len(submit_args) == 1
    assert isinstance(submit_args[0][0], GMSubscribeSymbolsRequest)
    assert submit_args[0][0].symbols == ["SZSE.000001"]
    assert submit_args[0][1] == 7.0
    assert bridge._subscribe_symbols == ["SHSE.600000", "SZSE.000001"]


def test_strategy_bridge_start_accepts_late_ready_signal_with_grace_window() -> None:
    """bridge 线程存活且 ready 略晚到达时，不应被 timeout 误杀。"""
    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )

    def _late_ready() -> None:
        time.sleep(0.03)
        bridge._ready_event.set()
        while not bridge._stop_event.is_set():
            time.sleep(0.001)

    bridge._run_strategy_loop = _late_ready  # type: ignore[method-assign]

    assert bridge.start(timeout=0.01) is True
    assert bridge.is_running() is True

    bridge.stop()


def test_strategy_bridge_start_timeout_logs_startup_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动超时时日志应带上当前 startup phase，便于定位卡点。"""
    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )
    errors: list[str] = []

    def _stuck_before_ready() -> None:
        bridge._startup_state["phase"] = "trade_connected"
        while not bridge._stop_event.is_set():
            time.sleep(0.001)

    bridge._run_strategy_loop = _stuck_before_ready  # type: ignore[method-assign]
    monkeypatch.setattr(gm_execute_module.logger, "error", lambda message: errors.append(str(message)))

    assert bridge.start(timeout=0.01) is False
    assert any("phase=trade_connected" in message for message in errors)


def test_strategy_bridge_invalid_token_fails_without_waiting_full_timeout() -> None:
    """SDK 已明确拒绝 token 时应立即抛出安全错误，不能伪装成启动超时."""
    bridge = GMStrategyBridge(
        token="secret-token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )

    class _InvalidTokenError(RuntimeError):
        status = 1000

        def __str__(self) -> str:
            return '{"status": 1000, "message": "错误或无效的token", "function": "run"}'

    def _raise_invalid_token(_strategy_file: str) -> None:
        raise _InvalidTokenError

    bridge._execute_strategy = _raise_invalid_token  # type: ignore[method-assign]
    started_at = time.monotonic()

    with pytest.raises(GMAuthenticationError, match="GM token 无效或已失效"):
        bridge.start(timeout=5.0)

    assert time.monotonic() - started_at < 0.5


@pytest.mark.parametrize("message", ['{"status": 1000}', "token invalid", "错误或无效的token"])
def test_gm_unstructured_text_does_not_become_authentication_error(message):
    from axile.executor.gm.core.strategy_bridge import _normalize_startup_error

    original = RuntimeError(message)
    normalized = _normalize_startup_error(original)
    assert not isinstance(normalized, GMAuthenticationError)
    assert normalized.execution_error == "GM 启动失败，具体原因未确认"
    assert str(original) == message


def test_strategy_bridge_stop_accepts_late_thread_exit_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop 时线程若在宽限窗口内自然退出，不应误报 5 秒超时警告。"""
    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )
    bridge._running = True

    join_calls: list[float] = []
    warnings: list[str] = []

    class _FakeThread:
        def __init__(self) -> None:
            self._is_alive_checks = 0

        def is_alive(self) -> bool:
            self._is_alive_checks += 1
            return self._is_alive_checks < 3

        def join(self, timeout: float | None = None) -> None:
            join_calls.append(float(timeout or 0.0))

    bridge._thread = _FakeThread()  # type: ignore[assignment]

    monkeypatch.setattr(bridge, "_fail_pending_requests", lambda error: None)
    monkeypatch.setattr(gm_execute_module.logger, "warning", lambda message: warnings.append(str(message)))

    bridge.stop()

    assert join_calls == [5.0, 2.0]
    assert warnings == []
    assert bridge._thread is None


def test_strategy_bridge_stop_requests_gm_runtime_shutdown_via_soft_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop 应通过 soft stop 关闭 GM runtime，而不是调用 gm.api.stop()。"""
    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )
    bridge._running = True

    join_calls: list[float] = []
    stop_calls: list[str] = []
    basic_module = sys.modules["gm.api.basic"]

    class _FakeThread:
        def __init__(self) -> None:
            self._is_alive_checks = 0

        def is_alive(self) -> bool:
            self._is_alive_checks += 1
            return self._is_alive_checks == 1

        def join(self, timeout: float | None = None) -> None:
            join_calls.append(float(timeout or 0.0))

    def _unsubscribe_all() -> None:
        stop_calls.append("unsubscribe")

    bridge._thread = _FakeThread()  # type: ignore[assignment]
    monkeypatch.setattr(bridge, "_fail_pending_requests", lambda error: None)
    monkeypatch.setattr(basic_module, "_py_gmi_unsubscribe_all", _unsubscribe_all, raising=False)
    monkeypatch.setattr(basic_module, "running", True, raising=False)

    bridge.stop()

    assert stop_calls == ["unsubscribe"]
    assert basic_module.running is False
    assert join_calls == [5.0]
    assert bridge._thread is None


def test_strategy_bridge_runtime_stop_request_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复请求 GM runtime soft stop 时，不应重复退订。"""
    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )
    basic_module = sys.modules["gm.api.basic"]
    stop_calls: list[str] = []

    def _unsubscribe_all() -> None:
        stop_calls.append("unsubscribe")

    monkeypatch.setattr(basic_module, "_py_gmi_unsubscribe_all", _unsubscribe_all, raising=False)
    monkeypatch.setattr(basic_module, "running", True, raising=False)

    bridge._request_gm_runtime_stop()
    bridge._request_gm_runtime_stop()

    assert stop_calls == ["unsubscribe"]
    assert basic_module.running is False


def test_strategy_bridge_stop_warning_includes_runtime_state_and_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop 超时告警应带上 GM runtime 状态和线程栈摘要。"""
    bridge = GMStrategyBridge(
        token="token",
        account_id="account-id",
        callback_dispatcher=object(),  # type: ignore[arg-type]
    )
    bridge._running = True

    warnings: list[str] = []

    class _FakeThread:
        name = "GMStrategyBridge"
        ident = 12345

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            return None

    def _frame_probe() -> Any:
        return sys._getframe()

    frame = _frame_probe()

    bridge._thread = _FakeThread()  # type: ignore[assignment]
    monkeypatch.setattr(bridge, "_fail_pending_requests", lambda error: None)
    monkeypatch.setattr(bridge, "_request_gm_runtime_stop", lambda: None)
    monkeypatch.setattr(bridge, "_read_gm_runtime_running_flag", lambda: False)
    monkeypatch.setattr(sys, "_current_frames", lambda: {12345: frame})
    monkeypatch.setattr(gm_execute_module.logger, "warning", lambda message: warnings.append(str(message)))

    bridge.stop()

    assert len(warnings) == 1
    assert "gm_running=False" in warnings[0]
    assert "GMStrategyBridge" in warnings[0]
    assert "_frame_probe" in warnings[0]


def test_gm_strategy_process_bridge_requests_stops_gm_runtime_when_bridge_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bridge 停止后，GM runtime 主循环也应收到 stop 信号。"""
    stop_event = threading.Event()
    stop_event.set()
    stop_calls: list[str] = []

    monkeypatch.setattr(gm_strategy_module, "_stop_bridge_timer", lambda: stop_calls.append("timer"))
    monkeypatch.setattr(gm_strategy_module, "_request_soft_runtime_stop", lambda: stop_calls.append("soft_stop"))
    install_gm_strategy_runtime_context(
        dispatcher=object(),
        ready_event=threading.Event(),
        stop_event=stop_event,
        stats={},
        subscribe_symbols=[],
        request_queue=queue.Queue(),
        startup_state={},
        runtime_stop_requested=threading.Event(),
        runtime_stop_lock=threading.Lock(),
    )
    try:
        gm_strategy_module._process_bridge_requests(object())
    finally:
        clear_gm_strategy_runtime_context()

    assert stop_calls == ["timer", "soft_stop"]


def test_gm_strategy_soft_runtime_stop_skips_duplicate_unsubscribe_when_already_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若主线程已请求 runtime stop，GM timer 回调不应再重复退订。"""
    stop_requested = threading.Event()
    stop_requested.set()
    stop_lock = threading.Lock()
    stop_calls: list[str] = []
    basic_module = sys.modules["gm.api.basic"]

    def _unsubscribe_all() -> None:
        stop_calls.append("unsubscribe")

    monkeypatch.setattr(basic_module, "_py_gmi_unsubscribe_all", _unsubscribe_all, raising=False)
    monkeypatch.setattr(basic_module, "running", True, raising=False)
    install_gm_strategy_runtime_context(
        dispatcher=object(),
        ready_event=threading.Event(),
        stop_event=threading.Event(),
        stats={},
        subscribe_symbols=[],
        request_queue=queue.Queue(),
        startup_state={},
        runtime_stop_requested=stop_requested,
        runtime_stop_lock=stop_lock,
    )
    try:
        gm_strategy_module._request_soft_runtime_stop()
    finally:
        clear_gm_strategy_runtime_context()

    assert stop_calls == []
    assert basic_module.running is False


def test_gm_strategy_dispatch_subscribe_symbols_adds_only_new_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from axile.executor.gm.core.api_bridge import GMSubscribeSymbolsRequest

    subscribe_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        gm_strategy_module,
        "subscribe",
        lambda *, symbols, frequency: subscribe_calls.append((symbols, frequency)),
    )
    install_gm_strategy_runtime_context(
        dispatcher=object(),
        ready_event=threading.Event(),
        stop_event=threading.Event(),
        stats={},
        subscribe_symbols=["SHSE.600000"],
        request_queue=queue.Queue(),
        startup_state={},
        runtime_stop_requested=threading.Event(),
        runtime_stop_lock=threading.Lock(),
    )
    try:
        added_symbols = gm_strategy_module._dispatch_bridge_request(
            GMSubscribeSymbolsRequest(symbols=["SHSE.600000", "SZSE.000001"]),
        )
        context = get_gm_strategy_runtime_context()
        assert context is not None
        subscribed_symbols = list(context.subscribe_symbols)
    finally:
        clear_gm_strategy_runtime_context()

    assert added_symbols == ["SZSE.000001"]
    assert subscribe_calls == [("SZSE.000001", "tick")]
    assert subscribed_symbols == ["SHSE.600000", "SZSE.000001"]


def test_get_account_assets_uses_bridge_runtime_in_serv_addr_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """serv_addr 模式下账户资产查询应通过 bridge，而不是直调 gm.api。"""
    executor = _build_executor()
    executor.account_id = "account-id"
    executor._gm_config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    bridge_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeBridge:
        def call(self, request: Any, timeout: float | None = None) -> Any:
            bridge_calls.append((request.operation, request.as_kwargs()))
            if request.operation == "get_position":
                return [
                    {
                        "symbol": "SHSE.510180",
                        "side": 1,
                        "volume": 147700,
                        "available": 147700,
                        "market_value": 572189.81,
                    }
                ]
            if request.operation == "get_cash":
                return {"available": 220600.45}
            raise AssertionError(f"unexpected operation: {request.operation}")

        def is_running(self) -> bool:
            return True

    executor._strategy_bridge = _FakeBridge()
    monkeypatch.setattr(
        gm_api_bridge_module,
        "get_position",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.get_position")),
    )
    monkeypatch.setattr(
        gm_api_bridge_module,
        "get_cash",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.get_cash")),
    )

    assets = executor.get_account_assets()

    assert assets.available_cash == 220600.45
    assert len(assets.positions) == 1
    assert assets.positions[0].symbol == "510180.SH"
    assert assets.positions[0].extra["gm_symbol"] == "SHSE.510180"
    assert bridge_calls == [
        ("get_position", {"account_id": "account-id"}),
        ("get_cash", {"account_id": "account-id"}),
    ]


def test_subscribe_price_requests_symbols_when_bridge_is_running() -> None:
    executor = _build_executor()
    executor._subscribe_symbols = ["SHSE.600000"]
    requested_symbols: list[list[str]] = []

    class _FakeBridge:
        def is_running(self) -> bool:
            return True

        def request_symbols(self, symbols: list[str]) -> None:
            requested_symbols.append(list(symbols))

    executor._strategy_bridge = _FakeBridge()

    executor.subscribe_price(["SHSE.600000", "SZSE.000001"])

    assert executor._subscribe_symbols == ["600000.SH", "000001.SZ"]
    assert requested_symbols == [["SZSE.000001"]]


def test_get_market_data_uses_bridge_runtime_in_serv_addr_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """serv_addr 模式下行情快照应通过 bridge，而不是直调 gm.api.current。"""
    executor = _build_executor()
    executor.account_id = "account-id"
    executor._gm_config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    bridge_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeBridge:
        def call(self, request: Any, timeout: float | None = None) -> Any:
            bridge_calls.append((request.operation, request.as_kwargs()))
            if request.operation == "current":
                return [
                    {
                        "symbol": "SHSE.510180",
                        "created_at": __import__("datetime").datetime(2026, 3, 24, 14, 0, 0),
                        "price": 3.874,
                        "last_volume": 1000,
                        "quotes": [
                            {"bid_p": 3.873, "bid_v": 10000, "ask_p": 3.874, "ask_v": 12000},
                        ],
                    }
                ]
            raise AssertionError(f"unexpected operation: {request.operation}")

        def is_running(self) -> bool:
            return True

    executor._strategy_bridge = _FakeBridge()
    monkeypatch.setattr(
        gm_api_bridge_module,
        "current",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.current")),
    )

    market_data = executor.get_market_data(["SHSE.510180"])

    assert set(market_data) == {"510180.SH"}
    assert bridge_calls == [("current", {"symbols": ["SHSE.510180"]})]


def test_place_order_uses_bridge_runtime_in_serv_addr_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """serv_addr 模式下真实下单应通过 bridge，而不是直调 gm.api.order_volume。"""
    executor = _build_executor()
    executor.account_id = "account-id"
    executor._execution_order_ids = set()
    executor._gm_config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    bridge_calls: list[tuple[str, dict[str, Any]]] = []
    outcomes: list[tuple[str, dict[str, object] | None]] = []

    class _Attempt:
        def record_outcome(self, outcome: str, metadata: dict[str, object] | None = None) -> None:
            outcomes.append((outcome, metadata))

    class _FakeBridge:
        def call(self, request: Any, timeout: float | None = None) -> Any:
            bridge_calls.append((request.operation, request.as_kwargs()))
            if request.operation == "order_volume":
                return [{"cl_ord_id": "gm-cl-1"}]
            raise AssertionError(f"unexpected operation: {request.operation}")

        def is_running(self) -> bool:
            return True

    executor._strategy_bridge = _FakeBridge()
    executor.set_account_control_guard(
        type(
            "_Guard",
            (),
            {"begin_operation": lambda self, *_args, **_kwargs: _Attempt()},
        )()
    )
    monkeypatch.setattr(
        gm_api_bridge_module,
        "order_volume",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.order_volume")),
    )

    order = executor.place_order(
        symbol="SHSE.600000",
        direction=gm_execute_module.OrderDirection.BUY,
        order_type=gm_execute_module.OrderType.LIMIT,
        volume=100,
        price=12.3,
    )

    assert order.order_id == "gm-cl-1"
    assert order.symbol == "600000.SH"
    assert order.extra["gm_symbol"] == "SHSE.600000"
    assert bridge_calls == [
        (
            "order_volume",
            {
                "symbol": "SHSE.600000",
                "volume": 100,
                "side": gm_execute_module.GMOrderSide.BUY,
                "order_type": gm_execute_module.GMOrderKind.LIMIT,
                "position_effect": gm_execute_module.GMPositionEffect.OPEN,
                "price": 12.3,
                "account": "account-id",
            },
        )
    ]
    assert outcomes == [("submitted", {"order_id": "gm-cl-1"})]


def test_pending_and_cancel_order_use_bridge_runtime_in_serv_addr_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serv_addr 模式下的查单与撤单应通过 bridge。"""
    executor = _build_executor()
    executor.account_id = "account-id"
    executor._gm_config = GMAccountConfig(
        connection_mode="service",
        account_id="account-id",
        token="token",
        serv_addr="127.0.0.1:7001",
    )
    bridge_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeBridge:
        def call(self, request: Any, timeout: float | None = None) -> Any:
            bridge_calls.append((request.operation, request.as_kwargs()))
            if request.operation == "get_unfinished_orders":
                return [
                    {
                        "account_id": "account-id",
                        "order_id": "1001",
                        "cl_ord_id": "gm-cl-1",
                        "symbol": "SHSE.600000",
                        "side": gm_execute_module.GMOrderSide.BUY,
                        "order_type": gm_execute_module.GMOrderKind.LIMIT,
                        "volume": 100,
                        "price": 12.3,
                        "status": 1,
                        "created_at": __import__("datetime").datetime(2026, 3, 24, 14, 0, 0),
                    }
                ]
            if request.operation == "get_execution_reports":
                return []
            if request.operation == "order_cancel":
                return None
            raise AssertionError(f"unexpected operation: {request.operation}")

        def is_running(self) -> bool:
            return True

    executor._strategy_bridge = _FakeBridge()
    monkeypatch.setattr(
        gm_api_bridge_module,
        "get_unfinished_orders",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.get_unfinished_orders")),
    )
    monkeypatch.setattr(
        gm_api_bridge_module,
        "get_execution_reports",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.get_execution_reports")),
    )
    monkeypatch.setattr(
        gm_api_bridge_module,
        "order_cancel",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call gm.api.order_cancel")),
    )

    pending_orders = executor.get_pending_orders("SHSE.600000")
    canceled = executor.cancel_order("SHSE.600000", "gm-cl-1")

    assert len(pending_orders) == 1
    assert canceled is True
    assert bridge_calls == [
        ("get_unfinished_orders", {"account_id": "account-id"}),
        ("order_cancel", {"wait_cancel_orders": [{"cl_ord_id": "gm-cl-1", "account_id": "account-id"}]}),
    ]


def test_cleanup_stops_callback_monitoring_without_clearing_callbacks() -> None:
    """执行级 cleanup 应停止 GM 监控，但不应清空外部注册的回调。"""
    executor = _build_executor()
    events: list[str] = []

    class _FakeDispatcher:
        def clear_all_callbacks(self) -> None:
            events.append("clear")

    class _FakeBridge:
        def stop(self) -> None:
            events.append("stop_bridge")

    executor._callback_dispatcher = _FakeDispatcher()
    executor._strategy_bridge = _FakeBridge()
    executor._callback_monitoring = True

    executor._cleanup()

    assert events == ["stop_bridge"]
    assert executor._strategy_bridge is None
    assert executor._callback_monitoring is False


def test_gm_1026_startup_reports_connection_failure_before_execution():
    from axile.executor.connection_state import connection_error_scope, connection_observer_scope

    observations = []
    with connection_observer_scope(observations.append):
        bridge = GMStrategyBridge(token="test", account_id="test", callback_dispatcher=object())

    class UpdateTokenError(RuntimeError):
        status = 1026

    def fail(_filename):
        raise UpdateTokenError("更新令牌错误 status=1026")

    bridge._execute_strategy = fail
    with pytest.raises(RuntimeError) as raised:
        bridge.start(timeout=0.5)
    assert connection_error_scope(raised.value) == "account_connection"
    assert observations[-1].status == "error"
    assert "1026" in observations[-1].error["technical_detail"]
    assert "fail" in observations[-1].error["traceback"]


def test_gm_account_status_filters_identity_and_only_accepts_logged_in(monkeypatch):
    import threading
    from types import SimpleNamespace

    from axile.executor.connection_state import ConnectionMonitor
    from axile.executor.gm.core import gm_strategy

    monitor = ConnectionMonitor()
    observations = []
    monitor.bind(observations.append)
    context = SimpleNamespace(account_id="wanted", connection_monitor=monitor, stop_event=threading.Event())
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    gm_strategy.on_account_status(None, {"account_id": "other", "status": {"state": 3}})
    gm_strategy.on_account_status(None, {"account_id": "wanted", "status": {"state": 2}})
    assert observations == []
    gm_strategy.on_account_status(None, {"account_id": "wanted", "status": {"state": 3}})
    assert observations[-1].status == "ok"
    gm_strategy.on_trade_data_disconnected(None)
    assert observations[-1].status == "error"
    gm_strategy.on_account_status(None, {"account_id": "wanted", "status": {"state": 3}})
    assert observations[-1].status == "ok" and observations[-1].error is None


@pytest.mark.parametrize(
    "operation,account_id,recovers",
    [
        ("get_cash", "account-id", True),
        ("get_position", "account-id", True),
        ("get_orders", "account-id", True),
        ("current", None, False),
        ("get_cash", "other-account", False),
    ],
)
def test_account_bridge_access_recovers_only_current_account(monkeypatch, operation, account_id, recovers):
    from types import SimpleNamespace

    from axile.executor.connection_state import connection_monitor

    executor = _build_executor()
    executor.account_id = "account-id"
    executor._gm_config = object()
    bridge = SimpleNamespace(call=lambda *_args, **_kwargs: [])
    monkeypatch.setattr(executor, "_ensure_strategy_bridge", lambda **_kwargs: bridge)
    monitor = connection_monitor(executor)
    observations = []
    monitor.bind(observations.append)
    monitor.failed(RuntimeError("旧连接故障"))
    executor._call_bridge(SimpleNamespace(operation=operation, account_id=account_id))
    assert observations[-1].status == ("ok" if recovers else "error")
