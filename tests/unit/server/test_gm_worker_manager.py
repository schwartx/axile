"""worker backend manager 的单元测试。"""

from __future__ import annotations

import asyncio
from threading import Event, Lock, Thread

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.models.execution_result import ExecutionStatus
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
from axile.server.portfolio_runner import (
    PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS,
    PORTFOLIO_FUNCTION_TIMEOUT_SECONDS,
)
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


def test_manager_shutdown_all_workers_joins_processes() -> None:
    """shutdown_all 应发送关闭请求并清空 worker 缓存。"""
    manager = WorkerBackendManager()
    handle = _FakeHandle(is_alive=True)
    manager._workers[2] = handle

    manager.shutdown_all()

    assert manager._workers == {}
    assert handle.connection.sent_commands == ["shutdown"]
    assert handle.connection.closed is True
    assert handle.process.join_calls == [2.0]
    assert handle.process.terminate_called is False


def _execute_request() -> WorkerBackendRequest:
    return WorkerBackendRequest(
        request_id="req-exec",
        command="execute_trade",
        account_payload={},
        execution_id=None,
        payload={},
    )


def test_request_blocking_polls_with_configured_timeout() -> None:
    """正常响应时应先按配置超时 poll，再 recv 返回响应。"""
    manager = WorkerBackendManager(execute_recv_timeout=1.5)
    handle = _FakeHandle(is_alive=True, poll_result=True)
    manager._workers[2] = handle

    response = manager._request_blocking(2, _execute_request(), timeout=1.5)

    assert response.request_id == "req-exec"
    assert handle.connection.poll_calls == [1.5]
    assert handle.process.terminate_called is False
    assert manager._workers[2] is handle


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


def test_request_blocking_drops_recovered_handle_before_next_request() -> None:
    manager = WorkerBackendManager()
    stale = _FakeHandle()
    replacement = _FakeHandle()
    manager._workers[2] = stale
    recovery_received = Event()
    allow_recovery_return = Event()
    second_started = Event()
    responses: dict[str, WorkerBackendResponse] = {}

    def stale_recv() -> WorkerBackendResponse:
        if stale.connection.last_request_id == "req-recovery":
            recovery_received.set()
            assert allow_recovery_return.wait(1)
            return WorkerBackendResponse(
                request_id="req-recovery",
                kind="error",
                channel_type=TradeChannel.CTP,
                error=WorkerBackendErrorPayload(
                    type="ctp_session_recovery_required",
                    message="ReqQryTradingAccount同步拒绝: return_code=-2",
                    retryable=True,
                ),
            )
        raise AssertionError("后续请求不得发送到污染 worker")

    stale.connection.recv = stale_recv  # type: ignore[method-assign]
    replacement.connection.recv = lambda: WorkerBackendResponse(  # type: ignore[method-assign]
        request_id=replacement.connection.last_request_id,
        kind="result",
        output_payload={"ok": True},
    )
    manager._spawn_worker = lambda _account_id: replacement  # type: ignore[method-assign]

    def run_recovery() -> None:
        responses["recovery"] = manager._request_blocking(
            2,
            WorkerBackendRequest("req-recovery", "get_account_assets", {}, None, {}),
            timeout=1,
        )

    def run_next_request() -> None:
        second_started.set()
        responses["next"] = manager._request_blocking(
            2,
            WorkerBackendRequest("req-next", "get_account_assets", {}, None, {}),
            timeout=1,
        )

    recovery_thread = Thread(target=run_recovery)
    recovery_thread.start()
    assert recovery_received.wait(1)
    next_thread = Thread(target=run_next_request)
    next_thread.start()
    assert second_started.wait(1)
    allow_recovery_return.set()
    recovery_thread.join(timeout=1)
    next_thread.join(timeout=1)

    assert not recovery_thread.is_alive()
    assert not next_thread.is_alive()
    assert responses["recovery"].error is not None
    assert responses["next"].output_payload == {"ok": True}
    assert stale.connection.sent_commands == ["get_account_assets"]
    assert replacement.connection.sent_commands == ["get_account_assets"]
    assert manager._workers[2] is replacement
    assert stale.process.terminate_called is True


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


def test_execute_trade_worker_timeout_follows_standard_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """调仓 worker 外层等待应使用账户执行超时并追加 IPC 余量。"""
    manager = WorkerBackendManager()
    account = build_account(id=2, execution_timeout=180)
    standard_input = type("StandardInput", (), {"execution_timeout": 180})()
    captured_timeouts: list[float] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        captured_timeouts.append(float(args[2]))
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(request_id=request.request_id, kind="result", output_payload={})

    expected = object()
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "_handle_response", lambda response: expected)

    result = asyncio.run(
        manager.execute_trade(
            account=account,
            standard_input=standard_input,  # type: ignore[arg-type]
            standard_input_dict={"execution_timeout": 180},
            audit_input={},
            execution_id="exec-1",
            trigger_source="manual",
            cleanup=True,
        )
    )

    assert result == (expected, None)
    assert captured_timeouts == [60.0, 240.0]


def test_execute_trade_drops_ctp_worker_after_session_recovery_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    standard_input = type("StandardInput", (), {"execution_timeout": 180})()
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

    output, normalized = asyncio.run(
        manager.execute_trade(
            account=account,
            standard_input=standard_input,  # type: ignore[arg-type]
            standard_input_dict={},
            audit_input={},
            execution_id="exec-1",
            trigger_source="manual",
            cleanup=True,
        )
    )

    assert commands == ["execute_trade"]
    assert normalized is None
    assert output.success is False
    assert output.extra["worker_error"]["type"] == "ctp_session_recovery_required"


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


def test_empty_positions_still_drops_inactive_account_after_normal_response(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2, is_started=False)
    dropped: list[int] = []
    expected = object()

    async def fake_prepare(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ready": True}

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(request_id=request.request_id, kind="result", output_payload={})

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(manager, "prepare_account", fake_prepare)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)
    monkeypatch.setattr(manager, "_handle_response", lambda _response: expected)

    result = asyncio.run(
        manager.empty_positions(account=account, empty_kwargs={}, audit_input={}, execution_id="exec-clear-normal")
    )

    assert result is expected
    assert dropped == [2]


def test_session_recovery_rebuilds_worker_only_for_the_next_request(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
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


def test_empty_positions_worker_timeout_follows_clear_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """清仓 worker 外层等待应使用清仓自己的执行超时并追加 IPC 余量。"""
    manager = WorkerBackendManager()
    account = build_account(id=2, execution_timeout=180)
    captured_timeouts: list[float] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        captured_timeouts.append(float(args[2]))
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(request_id=request.request_id, kind="result", output_payload={})

    expected = object()
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "_handle_response", lambda response: expected)

    result = asyncio.run(
        manager.empty_positions(
            account=account,
            empty_kwargs={"execution_timeout": 90},
            audit_input={},
            execution_id="exec-clear-1",
        )
    )

    assert result is expected
    assert captured_timeouts == [60.0, 150.0]


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


def test_prepare_account_sends_expected_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    captured: list[WorkerBackendRequest] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        captured.append(request)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={"ready": True, "trading_day": "20260824"},
        )

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(manager.prepare_account(account, "20260824"))

    assert result["trading_day"] == "20260824"
    assert captured[0].command == "prepare"
    assert captured[0].payload == {"expected_trading_day": "20260824"}


def test_calculate_portfolio_uses_dedicated_timeout_without_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    captured: list[tuple[WorkerBackendRequest, float]] = []
    dropped: list[int] = []

    async def fail_prepare(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("calculate_portfolio 不应额外发送 prepare 请求")

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        timeout = args[2]
        assert isinstance(request, WorkerBackendRequest)
        assert isinstance(timeout, float)
        captured.append((request, timeout))
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={"ok": True, "target": {"x": 1.0}},
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(manager, "prepare_account", fail_prepare)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    result = asyncio.run(manager.calculate_portfolio(account, "code"))

    assert result.target == {"x": 1.0}
    assert captured[0][0].command == "calculate_portfolio"
    assert captured[0][1] == PORTFOLIO_FUNCTION_TIMEOUT_SECONDS + PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS
    assert dropped == []


def test_calculate_portfolio_drops_worker_after_script_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    dropped: list[int] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={"ok": False, "error": "boom", "error_type": "RuntimeError"},
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)

    result = asyncio.run(manager.calculate_portfolio(account, "code"))

    assert result.ok is False
    assert dropped == [2]


def test_calculate_portfolio_keeps_replacement_after_ctp_session_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
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


def test_calculate_portfolio_drops_worker_after_worker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
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


def test_calculate_portfolio_drops_worker_after_invalid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    account = build_account(id=2)
    dropped: list[int] = []

    async def fake_to_thread(func: object, *args: object) -> WorkerBackendResponse:
        del func
        request = args[1]
        assert isinstance(request, WorkerBackendRequest)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={"ok": True, "target": "invalid"},
        )

    async def fake_drop_account(account_id: int) -> None:
        dropped.append(account_id)

    def fail_from_payload(_payload: dict[str, object]) -> object:
        raise ValueError("invalid worker result")

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(manager, "drop_account", fake_drop_account)
    monkeypatch.setattr(worker_manager_module.PortfolioFunctionResult, "from_payload", fail_from_payload)

    with pytest.raises(ValueError, match="invalid worker result"):
        asyncio.run(manager.calculate_portfolio(account, "code"))

    assert dropped == [2]


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


def test_drop_account_disposes_registered_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WorkerBackendManager()
    handle = _FakeHandle()
    manager._workers[2] = handle
    disposed: list[object] = []
    monkeypatch.setattr(manager, "_dispose_handle", disposed.append)

    asyncio.run(manager.drop_account(2))

    assert manager._workers == {}
    assert disposed == [handle]


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
