"""worker backend manager 的单元测试。"""

from __future__ import annotations

import asyncio
from threading import Event, Lock, Thread

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from axile.server.execution.worker_backend import manager as worker_manager_module
from axile.server.execution.worker_backend.manager import (
    WorkerBackendExecutionError,
    WorkerBackendManager,
    WorkerBackendTimeoutError,
)
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendErrorPayload,
    WorkerBackendRequest,
    WorkerBackendResponse,
)
from axile.server.portfolio_function import PortfolioFunctionError
from tests.unit.server._execution_test_support import build_account


class _FakeProcess:
    def __init__(self, *, is_alive: bool = True, join_stops: bool = True, exitcode: int | None = None) -> None:
        self._is_alive = is_alive
        self._join_stops = join_stops
        self.exitcode = exitcode
        self.join_calls: list[float] = []
        self.terminate_called = False
        self.kill_called = False

    def is_alive(self) -> bool:
        return self._is_alive

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(0.0 if timeout is None else timeout)
        if self._join_stops:
            self._is_alive = False

    def terminate(self) -> None:
        self.terminate_called = True
        self._is_alive = False

    def kill(self) -> None:
        self.kill_called = True
        self._is_alive = False


class _FakeConnection:
    def __init__(self, *, poll_result: bool = True) -> None:
        self.closed = False
        self.sent_commands: list[str] = []
        self.last_request_id = ""
        self.poll_result = poll_result
        self.poll_calls: list[float | None] = []

    def send(self, payload: object) -> None:
        command = getattr(payload, "command", None)
        if isinstance(command, str):
            self.sent_commands.append(command)
        request_id = getattr(payload, "request_id", None)
        if isinstance(request_id, str):
            self.last_request_id = request_id

    def poll(self, timeout: float | None = None) -> bool:
        self.poll_calls.append(timeout)
        return self.poll_result

    def recv(self) -> WorkerBackendResponse:
        return WorkerBackendResponse(
            request_id=self.last_request_id,
            kind="result",
            output_payload={"shutdown": True},
        )

    def close(self) -> None:
        self.closed = True


class _FakeControlConnection:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[object] = []

    def send(self, payload: object) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class _FakeHandle:
    catalog = None
    instruments = None

    def __init__(
        self,
        *,
        is_alive: bool = True,
        poll_result: bool = True,
        join_stops: bool = True,
    ) -> None:
        self.account_id = 2
        self.monitor = None
        self.process = _FakeProcess(is_alive=is_alive, join_stops=join_stops)
        self.connection = _FakeConnection(poll_result=poll_result)
        self.control_connection = _FakeControlConnection()
        self.request_lock = Lock()
        self.retired = False


def test_manager_recreates_dead_worker_before_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """manager 不应复用已死亡的 worker handle。"""
    manager = WorkerBackendManager()
    dead_handle = _FakeHandle(is_alive=False)
    manager._workers[2] = dead_handle

    created: list[_FakeHandle] = []

    def fake_spawn_worker(account_id: int) -> _FakeHandle:
        assert account_id == 2
        handle = _FakeHandle(is_alive=True)
        created.append(handle)
        return handle

    monkeypatch.setattr(manager, "_spawn_worker", fake_spawn_worker)

    handle = manager._get_or_create_worker(account_id=2)

    assert handle is created[0]
    assert dead_handle.connection.closed is True


def _execute_request() -> WorkerBackendRequest:
    return WorkerBackendRequest(
        request_id="req-exec",
        command="execute_trade",
        account_payload={},
        execution_id=None,
        payload={},
    )


def test_worker_restart_flag_preserves_error_and_never_replays_current_request(monkeypatch):
    manager = WorkerBackendManager()
    old = _FakeHandle()
    replacement = _FakeHandle()
    manager._workers[2] = old
    original = WorkerBackendErrorPayload(type="timeout_error", message="original instrument timeout")
    old.connection.recv = lambda: WorkerBackendResponse(
        request_id="req-exec",
        kind="error",
        error=original,
        requires_worker_restart=True,
    )
    spawn = []

    def spawn_worker(account_id):
        spawn.append(account_id)
        return replacement

    monkeypatch.setattr(manager, "_spawn_worker", spawn_worker)
    response = manager._request_blocking(2, _execute_request(), timeout=1)
    assert response.error is original
    assert old.process.terminate_called
    assert 2 not in manager._workers
    assert spawn == []
    assert old.connection.sent_commands == ["execute_trade"]
    manager._request_blocking(2, _execute_request(), timeout=1)
    assert spawn == [2]
    assert manager._workers[2] is replacement
    manager.close()


def test_request_blocking_timeout_force_kills_and_releases_lock() -> None:
    """worker 存活但卡死（poll 超时）应强制终止进程、移出注册表并释放请求锁。"""
    manager = WorkerBackendManager()
    handle = _FakeHandle(is_alive=True, poll_result=False)
    manager._workers[2] = handle

    with pytest.raises(WorkerBackendTimeoutError):
        manager._request_blocking(2, _execute_request(), timeout=0.01)

    # 进程被强制终止且从注册表移除，避免下次复用卡死 worker。
    assert handle.process.terminate_called is True
    assert manager._workers == {}
    # request_lock 必须已释放，否则该账户 worker 将永久不可用。
    assert handle.request_lock.acquire(blocking=False) is True
    handle.request_lock.release()


def test_request_blocking_eof_reports_exception_type_request_and_exitcode() -> None:
    """空消息 EOFError 也应带回可定位的 worker 退出信息。"""
    manager = WorkerBackendManager()
    handle = _FakeHandle(is_alive=True, join_stops=False)
    handle.process.exitcode = 7
    manager._workers[2] = handle

    def raise_eof() -> WorkerBackendResponse:
        raise EOFError

    handle.connection.recv = raise_eof  # type: ignore[method-assign]

    with pytest.raises(
        WorkerBackendExecutionError,
        match=r"EOFError.*request_id=req-exec.*worker_exitcode=7",
    ):
        manager._request_blocking(2, _execute_request(), timeout=1.0)

    assert manager._workers == {}


@pytest.mark.parametrize(("execution_timeout", "recv_timeout"), [(0, 600.0), (45, 105.0), (180, 240.0)])
def test_execute_trade_drops_ctp_worker_after_session_recovery_without_replay(
    monkeypatch: pytest.MonkeyPatch,
    execution_timeout: int,
    recv_timeout: float,
) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    commands: list[str] = []

    async def fake_prepare(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ready": True}

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert args[2] == recv_timeout
        assert isinstance(request, WorkerBackendRequest)
        commands.append(request.command)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=TradeChannel.CTP,
            error=WorkerBackendErrorPayload(
                type="ctp_session_recovery_required",
                message="ReqQryTradingAccount同步拒绝: return_code=-2",
                retryable=True,
            ),
        )

    monkeypatch.setattr(manager, "prepare_account", fake_prepare)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    output, normalized = asyncio.run(
        manager.execute_trade(
            account=account,
            standard_input_dict={"execution_timeout": execution_timeout},
            audit_input={},
            execution_id="exec-1",
            trigger_source="manual",
        )
    )

    assert commands == ["execute_trade"]
    assert normalized is None
    assert output.success is False
    assert output.extra["worker_error"]["type"] == "ctp_session_recovery_required"


def test_empty_positions_keeps_paused_gm_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """清仓结束后保留登录会话，供当前委托查询及后续执行复用。"""
    manager = WorkerBackendManager()
    account = build_account(id=2, trade_channel=TradeChannel.GM, is_started=False)
    dropped = []

    async def prepare(*_args, **_kwargs):
        return {"ready": True}

    async def to_thread(_func, *args):
        request = args[1]
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={
                "success": True,
                "status": "SUCCEEDED",
                "channel_type": "gm",
                "account_assets": UnifiedAccountAssets.unavailable().model_dump(mode="json"),
                "symbol_results": {},
            },
        )

    async def drop(account_id):
        dropped.append(account_id)

    monkeypatch.setattr(manager, "prepare_account", prepare)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    monkeypatch.setattr(manager, "drop_account", drop)

    output = asyncio.run(
        manager.empty_positions(account=account, empty_kwargs={}, audit_input={}, execution_id="clear")
    )

    assert output.success
    assert dropped == []


def test_clear_positions_preserves_termination_during_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    """清仓准备期间的协作终止须穿过 worker IPC，不能降级为普通执行错误。"""
    import pickle
    from types import SimpleNamespace

    from axile.server.execution.worker_backend import worker

    manager = WorkerBackendManager()
    account = build_account(id=2, trade_channel=TradeChannel.CTP)
    state = worker._WorkerBackendState(account_id=2)
    termination = ExecutionTerminated(
        reason="operator_cancel",
        mode="cancel_pending",
        acked_at="2026-10-25T10:00:00+08:00",
        cancel_failed_order_ids=["pending-order"],
        forced=False,
        cancel_attempted=True,
        cancel_unconfirmed=True,
    )
    commands = []

    def resolve(**kwargs):
        assert kwargs["execution_id"] == "clear-prepare"
        assert kwargs["termination_controller"] is not None
        raise termination

    def request_blocking(account_id, request, timeout, termination_controller):
        assert account_id == 2
        commands.append(request.command)
        return pickle.loads(pickle.dumps(worker._handle_worker_request(request, state)))

    monkeypatch.setattr(manager, "_current_session", lambda _account: SimpleNamespace(worker_alive=True))
    monkeypatch.setattr(manager, "_request_blocking", request_blocking)
    monkeypatch.setattr(worker, "_resolve_prepared_executor", resolve)

    with pytest.raises(ExecutionTerminated) as caught:
        asyncio.run(
            manager.empty_positions(account=account, empty_kwargs={}, audit_input={}, execution_id="clear-prepare")
        )

    assert caught.value.__dict__ == termination.__dict__
    assert commands == ["prepare"]
    assert state.active_execution_id is None
    assert not state.termination_event.is_set()


def test_empty_positions_drops_ctp_worker_after_session_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    commands: list[str] = []

    async def fake_prepare(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ready": True}

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        commands.append(request.command)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=TradeChannel.CTP,
            error=WorkerBackendErrorPayload(
                type="ctp_session_recovery_required",
                message="ReqQryTradingAccount同步拒绝: return_code=-2",
                retryable=True,
            ),
        )

    monkeypatch.setattr(manager, "prepare_account", fake_prepare)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(
        manager.empty_positions(
            account=account,
            empty_kwargs={},
            audit_input={},
            execution_id="exec-clear-1",
        )
    )

    assert commands == ["empty_positions"]
    assert result.success is False


def test_empty_positions_keeps_replacement_after_ctp_session_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2, is_started=False)
    dropped: list[int] = []

    async def fake_prepare(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ready": True}

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=TradeChannel.CTP,
            error=WorkerBackendErrorPayload(
                type="ctp_session_recovery_required",
                message="ReqQryOrder同步拒绝: return_code=-2",
                retryable=True,
            ),
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(manager, "prepare_account", fake_prepare)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)

    result = asyncio.run(
        manager.empty_positions(account=account, empty_kwargs={}, audit_input={}, execution_id="exec-clear-recovery")
    )

    assert result.success is False
    assert dropped == []


def test_session_recovery_rebuilds_worker_only_for_the_next_request(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_check_read_session", lambda _: None)
    account = build_account(id=2)
    stale = _FakeHandle()
    replacement = _FakeHandle()
    manager._workers[2] = stale
    spawned: list[int] = []

    def stale_response() -> WorkerBackendResponse:
        if stale.connection.sent_commands[-1] == "get_account_assets":
            return WorkerBackendResponse(
                request_id=stale.connection.last_request_id,
                kind="error",
                channel_type=TradeChannel.CTP,
                error=WorkerBackendErrorPayload(
                    type="ctp_session_recovery_required",
                    message="ReqQryTradingAccount同步拒绝: return_code=-2",
                    retryable=True,
                ),
            )
        return WorkerBackendResponse(
            request_id=stale.connection.last_request_id,
            kind="result",
            output_payload={"shutdown": True},
        )

    def replacement_response() -> WorkerBackendResponse:
        if replacement.connection.sent_commands[-1] == "get_account_assets":
            return WorkerBackendResponse(
                request_id=replacement.connection.last_request_id,
                kind="result",
                output_payload={
                    "available_cash": 800.0,
                    "total_asset": 1200.0,
                    "market_value": 400.0,
                    "positions": [],
                },
            )
        return WorkerBackendResponse(
            request_id=replacement.connection.last_request_id,
            kind="result",
            output_payload={"shutdown": True},
        )

    def spawn(account_id: int) -> _FakeHandle:
        spawned.append(account_id)
        return replacement

    stale.connection.recv = stale_response  # type: ignore[method-assign]
    replacement.connection.recv = replacement_response  # type: ignore[method-assign]
    monkeypatch.setattr(manager, "_spawn_worker", spawn)

    with pytest.raises(WorkerBackendExecutionError, match="return_code=-2"):
        asyncio.run(manager.get_account_assets(account))
    assets = asyncio.run(manager.get_account_assets(account))

    assert stale.connection.sent_commands == ["get_account_assets"]
    assert spawned == [2]
    assert replacement.connection.sent_commands == ["get_account_assets"]
    assert assets.total_asset == 1200.0


def test_dispose_handle_shutdown_timeout_falls_back_to_terminate() -> None:
    """shutdown 确认超时时不得永久阻塞，应回退到强制终止进程。"""
    manager = WorkerBackendManager(shutdown_recv_timeout=0.01)
    handle = _FakeHandle(is_alive=True, poll_result=False, join_stops=False)

    manager._dispose_handle(handle)

    assert handle.connection.sent_commands == ["shutdown"]
    assert handle.connection.closed is True
    assert handle.process.terminate_called is True


def test_manager_maps_structured_error_to_failed_output() -> None:
    """结构化错误响应应转换为失败输出，而不是抛出裸字符串异常。"""
    response = WorkerBackendResponse(
        request_id="req-1",
        kind="error",
        channel_type=TradeChannel.CTP,
        error=WorkerBackendErrorPayload(
            type="runtime_error",
            message="boom",
            retryable=False,
        ),
    )

    output = WorkerBackendManager._handle_response(response)

    assert output.success is False
    assert output.status == ExecutionStatus.FAILED
    assert output.channel_type == TradeChannel.CTP
    assert output.get_error_message() == "boom"
    assert output.account_assets.source == "unavailable"
    assert output.extra["worker_error"] == {
        "type": "runtime_error",
        "message": "boom",
        "retryable": False,
    }


def test_calculate_portfolio_keeps_replacement_after_ctp_session_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_check_read_session", lambda _: None)
    account = build_account(id=2)
    dropped: list[int] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=TradeChannel.CTP,
            error=WorkerBackendErrorPayload(
                type="ctp_session_recovery_required",
                message="ReqQryTradingAccount同步拒绝: return_code=-2",
                retryable=True,
            ),
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)

    with pytest.raises(WorkerBackendExecutionError, match="return_code=-2"):
        asyncio.run(manager.calculate_portfolio(account, "code"))

    assert dropped == []


def test_request_blocking_force_terminates_unresponsive_worker_after_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 不响应协作终止时应强杀并返回结构化 terminated，而非永久占槽."""
    manager = WorkerBackendManager()
    handle = _FakeHandle(is_alive=True, poll_result=False)
    manager._workers[2] = handle
    cancel_event = Event()
    cancel_event.set()
    controller = ExecutionTerminationController(
        cancel_event=cancel_event,
        reason_provider=lambda: "operator stop",
        mode_provider=lambda: "cancel_pending",
    )
    request = _execute_request()
    request.execution_id = "exec-force-stop"
    monkeypatch.setattr(worker_manager_module, "_TERMINATION_GRACE_SECONDS", 0.0)

    with pytest.raises(ExecutionTerminated) as caught:
        manager._request_blocking(2, request, timeout=10.0, termination_controller=controller)

    assert caught.value.forced is True
    assert caught.value.cancel_unconfirmed is True
    assert caught.value.mode == "cancel_pending"
    assert handle.process.terminate_called is True
    assert manager._workers == {}
    assert len(handle.control_connection.sent) == 1


def test_shutdown_does_not_wait_indefinitely_for_account_recovery_lock():
    """服务退出时，即使账户准备仍持有请求锁，也应回收该 worker。"""
    manager = WorkerBackendManager(shutdown_recv_timeout=0.01)
    handle = _FakeHandle(join_stops=False)
    manager._workers[5] = handle
    handle.request_lock.acquire()
    stopped = Event()

    def shutdown():
        try:
            manager.shutdown_all()
        finally:
            stopped.set()

    thread = Thread(target=shutdown, daemon=True)
    thread.start()
    try:
        assert stopped.wait(1), "关停不应等待账户初始化请求锁"
        assert handle.process.terminate_called
        assert handle.connection.closed
        assert handle.connection.sent_commands == []
    finally:
        handle.request_lock.release()
        thread.join(timeout=1)


def test_execution_activity_does_not_reenable_paused_catalog_source():
    from unittest.mock import Mock

    manager = WorkerBackendManager()
    handle = _FakeHandle()
    handle.catalog = Mock()
    manager._workers[2] = handle
    paused = build_account(id=2, is_started=False)
    try:
        manager.configure_catalog_account(paused, last_execution="2026-10-13 20:00:00")
        manager._record_catalog_execution(2)
        assert manager._catalog_accounts[2][1] is False
        assert handle.catalog.configure.call_args.kwargs["enabled"] is False
        assert manager._catalog_accounts[2][2] >= "2026-10-13T20:00:00"
    finally:
        manager.close()


def test_calculate_portfolio_drops_worker_after_script_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_check_read_session", lambda _: None)
    account = build_account(id=2)
    dropped: list[int] = []
    error = PortfolioFunctionError("boom", error_line=2, error_type="RuntimeError")

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={"portfolio_result": error},
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)

    with pytest.raises(PortfolioFunctionError) as caught:
        asyncio.run(manager.calculate_portfolio(account, "code"))
    assert caught.value is error and caught.value.error_line == 2
    assert dropped == [2]


def test_calculate_portfolio_drops_worker_after_worker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_check_read_session", lambda _: None)
    account = build_account(id=2)
    dropped: list[int] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            error=WorkerBackendErrorPayload(type="RuntimeError", message="prepare failed"),
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)

    with pytest.raises(WorkerBackendExecutionError, match="prepare failed"):
        asyncio.run(manager.calculate_portfolio(account, "code"))

    assert dropped == [2]


@pytest.mark.parametrize(
    "payload, error_type",
    [
        ({}, KeyError),
        ({"ok": True, "target": "invalid"}, KeyError),
        ({"portfolio_result": []}, TypeError),
        ({"portfolio_result": {"x": True}}, TypeError),
        ({"portfolio_result": {"x": float("nan")}}, ValueError),
    ],
)
def test_calculate_portfolio_drops_worker_after_invalid_result(
    monkeypatch: pytest.MonkeyPatch, payload, error_type
) -> None:
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_check_read_session", lambda _: None)
    account = build_account(id=2)
    dropped: list[int] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload=payload,
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)

    with pytest.raises(error_type):
        asyncio.run(manager.calculate_portfolio(account, "code"))

    assert dropped == [2]


def test_drop_account_disposes_registered_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    handle = _FakeHandle()
    manager._workers[2] = handle
    disposed: list[object] = []
    monkeypatch.setattr(manager, "_dispose_handle", disposed.append)

    asyncio.run(manager.drop_account(2))

    assert manager._workers == {}
    assert disposed == [handle]


@pytest.mark.parametrize("mode", ["success", "script_error", "prepare_error"])
def test_calculate_worker_keeps_script_and_prepare_error_channels(monkeypatch, mode):
    from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
    from axile.server.context import SamplePortfolioExecutor
    from axile.server.execution.worker_backend import worker
    from axile.server.execution.worker_backend.worker_state import _WorkerBackendState

    watches = []
    finalized = []
    executor = SamplePortfolioExecutor()

    class Watchdog:
        def __init__(self, *, args, **kwargs):
            watches.append(args[0])

        def start(self):
            pass

    def prepare(**kwargs):
        if mode == "prepare_error":
            raise CtpSessionRecoveryRequired("return_code=-2", return_code=-2)
        return executor

    monkeypatch.setattr(worker.threading, "Thread", Watchdog)
    monkeypatch.setattr(worker, "_resolve_prepared_executor", prepare)
    monkeypatch.setattr(worker, "_finalize_with_orders", lambda actual, state: finalized.append(actual))
    code = "def calculate_portfolio(context):\n    return {'x': 0.25}\n"
    if mode == "script_error":
        code = "def calculate_portfolio(context):\n    raise ValueError('broken script')\n"
    request = WorkerBackendRequest(
        request_id="portfolio",
        command="calculate_portfolio",
        execution_id=None,
        account_payload=build_account(id=2).model_dump(mode="json"),
        payload={"code": code},
    )
    response = worker._handle_calculate_portfolio(request, _WorkerBackendState())
    assert len(finalized) == 1
    if mode == "prepare_error":
        assert response.kind == "error" and response.error.type == "ctp_session_recovery_required"
        assert response.error.retryable is True and watches == [] and finalized == [None]
    else:
        assert finalized == [executor] and len(watches) == 1 and watches[0].is_set()
        assert response.kind == "result"
        result = response.output_payload["portfolio_result"]
        if mode == "script_error":
            assert isinstance(result, PortfolioFunctionError)
            assert result.error_line == 2 and result.error_type == "ValueError"
        else:
            assert result == {"x": 0.25}
