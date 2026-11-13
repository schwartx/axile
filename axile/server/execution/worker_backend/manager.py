"""多进程执行 worker 管理器。."""

from __future__ import annotations

import asyncio
import atexit
from dataclasses import dataclass, field
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext, SpawnProcess
from threading import Lock, Thread
from typing import Any, cast
from uuid import uuid4

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.instrument_catalog import InstrumentCatalog
from axile.executor.models.execution_result import ExecutionOutcome, ExecutionStatus
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from axile.server.account_control.ipc import RemoteMonitor
from axile.server.connection_state import ConnectionBinding
from axile.server.db.models.account import AccountContext, AccountSnapshot
from axile.server.db.models.base import now_str
from axile.server.execution.worker_backend.catalog import CatalogSession
from axile.server.execution.worker_backend.orders import OrdersChannel
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendErrorPayload,
    WorkerBackendRequest,
    WorkerBackendResponse,
    WorkerReplayConfig,
    WorkerReplayControl,
    WorkerReplayStatus,
    WorkerTerminationSignal,
)
from axile.server.execution.worker_backend.quota import QuotaMirror
from axile.server.execution.worker_backend.worker import run_worker_backend_loop
from axile.server.portfolio_function import PortfolioFunctionError, _normalize_target
from axile.server.portfolio_runner import (
    PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS,
    PORTFOLIO_FUNCTION_TIMEOUT_SECONDS,
)


class WorkerBackendExecutionError(RuntimeError):
    """多进程 worker 返回错误响应时抛出的异常。"""

    def __init__(
        self,
        message: str,
        *,
        execution_error: str | None = None,
        reason_code: str | None = None,
        error_id: int | None = None,
        retryable: bool = False,
        technical_detail: str | None = None,
        formatted_traceback: str | None = None,
        error_scope: str | None = None,
        diagnostics: list[dict[str, object]] | None = None,
        exception_type: str | None = None,
        retry_after: int = 0,
    ) -> None:
        """保留 IPC 原文、traceback 与机器码，不从原文推断业务状态。"""
        super().__init__(message)
        self.execution_error = execution_error
        self.reason_code = reason_code
        self.error_id = error_id
        self.retryable = retryable
        self.technical_detail = technical_detail
        self.formatted_traceback = formatted_traceback
        self.error_scope = error_scope
        self.diagnostics = diagnostics or []
        self.exception_type = exception_type
        self.retry_after = retry_after

    @classmethod
    def from_payload(cls, error: WorkerBackendErrorPayload | None, fallback: str) -> WorkerBackendExecutionError:
        """把远端错误证据还原为主进程异常，保持渠道诊断和冷却信息。"""
        message = error.message if error is not None else fallback
        return cls(
            message,
            execution_error=message,
            reason_code=getattr(error, "reason_code", None),
            error_id=getattr(error, "error_id", None),
            retryable=getattr(error, "retryable", False),
            technical_detail=getattr(error, "technical_detail", None),
            formatted_traceback=getattr(error, "traceback", None),
            error_scope=getattr(error, "error_scope", None),
            diagnostics=getattr(error, "diagnostics", None),
            exception_type=getattr(error, "exception_type", None),
            retry_after=getattr(error, "retry_after", 0),
        )


class WorkerBackendTimeoutError(WorkerBackendExecutionError):
    """多进程 worker 在超时内未返回响应时抛出的异常。.

    Notes
    -----
    该异常专门覆盖「worker 进程仍存活、但业务逻辑卡死」的活性风险：
    此时 pipe 不会断开，普通阻塞 ``recv()`` 会永久等待。捕获后应强制
    终止对应 worker 进程，避免执行线程、请求锁与账户运行占位被永久占用。
    """


_DEFAULT_EXECUTE_RECV_TIMEOUT_SECONDS = 600.0
"""业务请求缺少有效执行超时时，等待 worker 响应的兼容兜底（秒）。."""

_EXECUTION_TIMEOUT_GRACE_SECONDS = 60.0
"""worker 内部执行超时后返回结构化结果的 IPC 余量（秒）。."""

_DEFAULT_SHUTDOWN_RECV_TIMEOUT_SECONDS = 5.0
"""关闭请求等待 worker 确认的默认超时（秒）。."""

_DEFAULT_PREPARE_RECV_TIMEOUT_SECONDS = 60.0
"""账户通道登录与准备的默认等待时限（秒）。."""

_ACCOUNT_ASSET_RECV_TIMEOUT_SECONDS = 30.0
"""人工账户资产查询的最大等待时限（秒）."""

_CTP_SESSION_RECOVERY_ERROR_TYPE = "ctp_session_recovery_required"

_TERMINATION_POLL_SECONDS = 0.1
"""等待 worker 响应时观察人工终止事件的最大间隔（秒）."""

_TERMINATION_GRACE_SECONDS = 5.0
"""人工终止后等待 worker 协作撤单并退出的宽限时间（秒）."""

_REPLAY_STATE_PRIORITY = {
    "recording": 0,
    "error": 1,
    "closing": 2,
    "starting": 3,
    "waiting_connection": 4,
    "unsupported": 5,
    "disabled": 6,
}

_worker_backend_manager: "WorkerBackendManager | None" = None
_connection_binding_lock = Lock()


def _require_account_id(account: AccountContext) -> int:
    if account.id is None:
        raise WorkerBackendExecutionError("worker backend 需要持久化后的 account.id")
    return int(account.id)


def _resolve_worker_recv_timeout(execution_timeout: object, *, fallback: float) -> float:
    """
    根据本次业务执行超时推导主进程等待 worker 的时限.

    Parameters
    ----------
    execution_timeout : object
        调仓或清仓入口解析出的本次执行超时秒数。
    fallback : float
        缺少有效正数超时时使用的兼容兜底秒数。

    Returns
    -------
    float
        业务执行超时加固定 IPC 收尾余量；输入无效时返回兜底值。

    Notes
    -----
    外层等待必须晚于 worker 内部 deadline，否则主进程可能先强杀进程，
    把本应记录为 trigger=timeout 的结构化终止降级成通信失败。
    """
    if isinstance(execution_timeout, bool) or not isinstance(execution_timeout, (int, float)):
        return fallback
    timeout = float(execution_timeout)
    if timeout <= 0:
        return fallback
    return timeout + _EXECUTION_TIMEOUT_GRACE_SECONDS


@dataclass(slots=True)
class _WorkerBackendHandle:
    account_id: int
    process: SpawnProcess
    connection: Connection
    control_connection: Connection
    request_lock: Lock = field(default_factory=Lock)
    retired: bool = False
    catalog: CatalogSession | None = None
    monitor: RemoteMonitor | None = None
    orders: OrdersChannel | None = None
    quota_mirror: QuotaMirror | None = None
    connection_binding: ConnectionBinding | None = None
    control_lock: Lock = field(default_factory=Lock)
    replay_lock: Lock = field(default_factory=Lock)
    replay_pending: str | None = None
    replay_sent_version: int = -1
    control_thread: Thread | None = None
    control_config: WorkerReplayControl | None = None
    control_status: WorkerReplayControl | None = None
    control_termination: WorkerTerminationSignal | None = None
    control_closed: bool = False
    replay_draining: bool = False


class WorkerBackendManager:
    """维护一账户一多进程 worker 的最小管理器。.

    Notes
    -----
    各渠道共用每账户独立的常驻进程。首次连接后持续复用，调度暂停不会
    关闭会话；配置变更、主动断开、通信故障和服务退出负责显式回收。
    当前会话状态由存活实例与监控快照提供，数据库仅保存历史和连接意愿。
    """

    def __init__(
        self,
        *,
        execute_recv_timeout: float = _DEFAULT_EXECUTE_RECV_TIMEOUT_SECONDS,
        shutdown_recv_timeout: float = _DEFAULT_SHUTDOWN_RECV_TIMEOUT_SECONDS,
    ) -> None:
        """
        初始化多进程 worker manager.

        Parameters
        ----------
        execute_recv_timeout : float, default=600.0
            ``execute_trade``/``empty_positions`` 等业务请求等待 worker
            响应的超时秒数；超时视为 worker 卡死并强制终止其进程。
        shutdown_recv_timeout : float, default=5.0
            关闭请求等待 worker 确认的超时秒数；超时后回退到强制终止。
        """
        self._ctx = cast("SpawnContext", get_context("spawn"))
        self._lock = Lock()
        # 一账户一常驻 worker；无空闲回收/无上限是有意取舍，理由与重评估阈值见类 docstring。
        self._workers: dict[int, _WorkerBackendHandle] = {}
        self._retiring_workers: dict[int, list[_WorkerBackendHandle]] = {}
        self._replay_configs: dict[int, WorkerReplayConfig] = {}
        self.instrument_catalog = InstrumentCatalog()
        self._catalog_accounts: dict[int, tuple[str, bool, str]] = {}
        self._execute_recv_timeout = execute_recv_timeout
        self._shutdown_recv_timeout = shutdown_recv_timeout
        atexit.register(self.close)

    def _spawn_worker(self, account_id: int) -> _WorkerBackendHandle:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        child_control_conn, parent_control_conn = self._ctx.Pipe(duplex=True)
        parent_catalog, child_catalog = self._ctx.Pipe(duplex=True)
        parent_monitor, child_monitor = self._ctx.Pipe(duplex=True)
        parent_orders, child_orders = self._ctx.Pipe(duplex=True)
        parent_quota, child_quota = self._ctx.Pipe(duplex=True)
        process = self._ctx.Process(
            target=run_worker_backend_loop,
            args=(child_conn, account_id, child_control_conn, child_catalog, child_monitor, child_orders, child_quota),
            name=f"axile-execution-worker-{account_id}",
            daemon=True,
        )
        process.start()
        child_conn.close()
        child_control_conn.close()
        child_catalog.close()
        child_monitor.close()
        child_orders.close()
        child_quota.close()
        catalog = CatalogSession(cast("Connection", parent_catalog), self.instrument_catalog, enabled=False)
        channel, enabled, last_execution = self._catalog_accounts.get(account_id, ("", False, ""))
        catalog.configure(channel=channel, enabled=enabled, last_execution=last_execution)
        return _WorkerBackendHandle(
            account_id=account_id,
            process=process,
            connection=cast("Connection", parent_conn),
            control_connection=cast("Connection", parent_control_conn),
            catalog=catalog,
            monitor=RemoteMonitor(cast("Connection", parent_monitor), account_id, process.is_alive),
            orders=OrdersChannel(cast("Connection", parent_orders)),
            quota_mirror=QuotaMirror(cast("Connection", parent_quota), account_id, process),
        )

    def configure_catalog_account(self, account: AccountContext, *, last_execution: str = "") -> None:
        """目录只使用非暂停账户，优先 CTP 与最近执行的账户。"""
        account_id = _require_account_id(account)
        with self._lock:
            previous = self._catalog_accounts.get(account_id, ("", False, ""))[2]
            latest = max(previous, last_execution.replace(" ", "T"))
            channel, enabled = str(account.trade_channel), account.is_started
            self._catalog_accounts[account_id] = channel, enabled, latest
            handle = self._workers.get(account_id)
            if handle is not None and handle.catalog is not None:
                handle.catalog.configure(channel=channel, enabled=enabled, last_execution=latest)

    async def configure_replay(self, account: AccountContext) -> dict[str, object]:
        """保存意愿后仅通知已有 worker，不创建交易连接。"""
        return await asyncio.to_thread(self._replay_control_blocking, account, True)

    async def replay_status(self, account: AccountContext) -> dict[str, object]:
        """通过独立控制通路读取录制状态，不排队等待交易。"""
        return await asyncio.to_thread(self._replay_control_blocking, account, False)

    def _replay_control_blocking(self, account: AccountContext, update: bool) -> dict[str, object]:
        from axile.server.execution.worker_backend.replay import replay_config

        config = replay_config(account)
        account_id = _require_account_id(account)
        with self._lock:
            previous = self._replay_configs.get(account_id)
            if previous is None or config.config_version > previous.config_version:
                self._replay_configs[account_id] = config
            else:
                config = previous
            handle = self._workers.get(account_id)
            retiring = [item for item in self._retiring_workers.get(account_id, ()) if item.process.is_alive()]
            if retiring:
                self._retiring_workers[account_id] = retiring
            else:
                self._retiring_workers.pop(account_id, None)
            handles = ([handle] if handle is not None else []) + [item for item in retiring if item is not handle]
            if not handles:
                return {
                    "desired": config.enabled,
                    "state": "disabled"
                    if not config.enabled
                    else "waiting_connection"
                    if config.supported
                    else "unsupported",
                    "config_version": config.config_version,
                    "effective_version": config.config_version if not config.enabled else 0,
                    "recording_id": None,
                    "error": None,
                }
        # 先向所有仍存活的实例交付意愿，再共用一次有界的确认预算。
        for handle in handles:
            if handle.process.is_alive():
                try:
                    if update or handle.replay_sent_version < config.config_version:
                        self._queue_replay_config(handle, config)
                except (OSError, EOFError, ValueError):
                    pass
        deadline = clock_monotonic() + 0.5
        results = [self._replay_handle_status(handle, deadline) for handle in handles]
        known = [result.get("effective_version") for result in results]
        effective = (
            min(cast(int, version) for version in known) if all(type(version) is int for version in known) else None
        )
        newest = max(results, key=lambda item: cast(int, item.get("config_version", -1)))
        # 任一存活实例仍在录制时，不能被其他实例的 disabled 掩盖。
        result = min(results, key=lambda item: _REPLAY_STATE_PRIORITY.get(str(item.get("state")), 1))
        return self._replay_status_with_intent(
            account_id,
            result
            | {
                "desired": newest.get("desired", config.enabled),
                "config_version": newest.get("config_version", config.config_version),
                "effective_version": effective,
            },
        )

    def _replay_handle_status(self, handle: _WorkerBackendHandle, deadline: float) -> dict[str, object]:
        fallback = {
            "state": "error",
            "effective_version": None,
            "recording_id": None,
            "error": "录制控制状态待确认；交易连接未改变",
        }
        if not self._is_handle_healthy(handle):
            return fallback
        locked = False
        try:
            locked = handle.replay_lock.acquire(timeout=max(0.0, deadline - clock_monotonic()))
            if not locked:
                return fallback | {"state": "starting", "error": "录制控制正在处理，请稍后查询"}
            if handle.replay_pending is not None:
                self._receive_replay_status(handle, deadline=deadline)
            request = WorkerReplayControl(uuid4().hex)
            self._queue_control_message(handle, request)
            return self._receive_replay_status(handle, deadline=deadline)
        except (OSError, EOFError, TimeoutError, ValueError):
            return fallback
        finally:
            if locked:
                handle.replay_lock.release()

    @staticmethod
    def _queue_replay_config(handle: _WorkerBackendHandle, config: WorkerReplayConfig) -> None:
        """合并最新录制意愿，管道背压不阻塞调用线程。"""
        WorkerBackendManager._queue_control_message(handle, WorkerReplayControl(None, config))

    @staticmethod
    def _queue_control_message(
        handle: _WorkerBackendHandle, message: WorkerReplayControl | WorkerTerminationSignal
    ) -> None:
        """固定槽保存最新配置、状态请求和优先终止信号，空闲即退出。"""
        with handle.control_lock:
            if handle.control_closed or handle.retired:
                raise OSError("worker control channel is closed")
            if isinstance(message, WorkerTerminationSignal):
                handle.control_termination = message
            elif message.config is not None:
                if message.config.config_version <= handle.replay_sent_version:
                    return
                handle.control_config = message
                handle.replay_sent_version = message.config.config_version
            else:
                handle.control_status = message
                handle.replay_pending = message.request_id
            if handle.control_thread is None:
                handle.control_thread = Thread(
                    target=WorkerBackendManager._drain_control_messages,
                    args=(handle,),
                    name=f"worker-control-{handle.account_id}",
                    daemon=True,
                )
                try:
                    handle.control_thread.start()
                except RuntimeError as error:
                    WorkerBackendManager._clear_control_messages(handle)
                    handle.control_thread = None
                    raise OSError("worker control sender unavailable") from error

    @staticmethod
    def _clear_control_messages(handle: _WorkerBackendHandle, failed: object = None) -> None:
        """持发送槽锁释放未发送请求，保留已经在管道内的回复身份。"""
        for message in (failed, handle.control_status):
            if isinstance(message, WorkerReplayControl) and message.config is None:
                if handle.replay_pending == message.request_id:
                    handle.replay_pending = None
        handle.control_config = None
        handle.control_status = None
        handle.control_termination = None
        handle.replay_sent_version = -1

    @staticmethod
    def _drain_control_messages(handle: _WorkerBackendHandle) -> None:
        while True:
            with handle.control_lock:
                if handle.control_closed or handle.retired:
                    WorkerBackendManager._clear_control_messages(handle)
                    handle.control_thread = None
                    return
                message = None
                for name in ("control_termination", "control_config", "control_status"):
                    if (message := getattr(handle, name)) is not None:
                        setattr(handle, name, None)
                        break
                if message is None:
                    handle.control_thread = None
                    return
            try:
                handle.control_connection.send(message)
            except (OSError, EOFError, TypeError):
                with handle.control_lock:
                    WorkerBackendManager._clear_control_messages(handle, message)
                    handle.control_thread = None
                return

    @staticmethod
    def _close_control(handle: _WorkerBackendHandle) -> None:
        """关闭控制端口而不等待阻塞写入，worker 退出会解除在途发送。"""
        with handle.control_lock:
            handle.control_closed = True
            WorkerBackendManager._clear_control_messages(handle)
        try:
            handle.control_connection.close()
        except OSError:
            pass

    def _replay_status_with_intent(self, account_id: int, status: dict[str, object]) -> dict[str, object]:
        """合并最新保存意愿，保留 worker 实际版本及未知生效状态。"""
        with self._lock:
            config = self._replay_configs[account_id]
        if config.config_version < status.get("config_version", -1):
            return status
        return status | {"desired": config.enabled, "config_version": config.config_version}

    @staticmethod
    def _receive_replay_status(handle: _WorkerBackendHandle, *, deadline: float | None = None) -> dict[str, object]:
        expected = handle.replay_pending
        if deadline is None:
            deadline = clock_monotonic() + 0.5
        while expected is not None and handle.control_connection.poll(max(0.0, deadline - clock_monotonic())):
            response = handle.control_connection.recv()
            if not isinstance(response, WorkerReplayStatus):
                raise ValueError("录制状态确认类型不匹配")
            if response.request_id == expected:
                handle.replay_pending = None
                return response.status
            if clock_monotonic() >= deadline:
                break
        raise TimeoutError("录制状态确认超时")

    def _record_catalog_execution(self, account_id: int) -> None:
        """只更新使用时间，避免在途请求的旧快照重新启用已暂停来源。"""
        with self._lock:
            channel, enabled, _ = self._catalog_accounts[account_id]
            latest = now_str()
            self._catalog_accounts[account_id] = channel, enabled, latest
            handle = self._workers.get(account_id)
            if handle is not None and handle.catalog is not None:
                handle.catalog.configure(channel=channel, enabled=enabled, last_execution=latest)

    def _send_shutdown(self, handle: _WorkerBackendHandle, *, lock_timeout: float = -1) -> bool:
        request = WorkerBackendRequest.shutdown(
            request_id=uuid4().hex,
            reason="manager_shutdown",
        )
        if not handle.request_lock.acquire(timeout=lock_timeout):
            raise WorkerBackendTimeoutError("worker backend shutdown 等待在途请求超时")
        try:
            handle.connection.send(request)
            if not handle.connection.poll(self._shutdown_recv_timeout):
                raise WorkerBackendTimeoutError(f"worker backend shutdown 响应超时（{self._shutdown_recv_timeout}s）")
            response = handle.connection.recv()
        finally:
            handle.request_lock.release()

        if not isinstance(response, WorkerBackendResponse):
            raise WorkerBackendExecutionError("worker backend shutdown 返回了未知响应类型")
        if response.request_id != request.request_id:
            raise WorkerBackendExecutionError("worker backend shutdown 返回了不匹配的 request_id")
        return bool(response.output_payload and response.output_payload.get("replay_draining"))

    @staticmethod
    def _terminate_process(handle: _WorkerBackendHandle) -> None:
        """强制终止 worker 进程，绕过优雅 shutdown 与 request_lock.

        Notes
        -----
        用于 worker 卡死或通信失败的兜底路径。该方法**不发送 shutdown
        请求、不获取 ``request_lock``**，因此可以在请求线程仍持有锁、
        worker 业务卡死无法应答时安全调用，避免二次死锁。
        """
        try:
            handle.connection.close()
        except OSError:
            pass
        WorkerBackendManager._close_control(handle)
        if handle.process.is_alive():
            handle.process.terminate()
            handle.process.join(timeout=2.0)
        if handle.process.is_alive():
            kill = getattr(handle.process, "kill", None)
            if callable(kill):
                kill()
                handle.process.join(timeout=2.0)
        if handle.monitor is not None:
            handle.monitor.close()
        orders = getattr(handle, "orders", None)
        if orders is not None:
            orders.close()
        if handle.catalog is not None:
            handle.catalog.close()

    def _dispose_handle(self, handle: _WorkerBackendHandle, *, lock_timeout: float = -1) -> None:
        from axile.server.connection_state import end_connection_instance

        end_connection_instance(getattr(handle, "connection_binding", None), "账户执行器已关闭，会话已结束")
        try:
            if not handle.connection.closed and handle.process.is_alive():
                handle.replay_draining = self._send_shutdown(handle, lock_timeout=lock_timeout)
        except (BrokenPipeError, EOFError, OSError, WorkerBackendExecutionError):
            pass
        try:
            handle.connection.close()
        except OSError:
            pass
        self._close_control(handle)
        if handle.process.is_alive():
            handle.process.join(timeout=2.0)
        if handle.process.is_alive() and not handle.replay_draining:
            handle.process.terminate()
            handle.process.join(timeout=2.0)
        if handle.monitor is not None:
            handle.monitor.close()
        orders = getattr(handle, "orders", None)
        if orders is not None:
            orders.close()
        if handle.catalog is not None:
            handle.catalog.close()

    @staticmethod
    def _remaining_response_time(handle, request, deadline, now):
        if handle.catalog is None:
            return deadline - now
        active, updated, started, finished = handle.catalog.snapshot(request.request_id)
        if active:
            remaining = updated + _DEFAULT_PREPARE_RECV_TIMEOUT_SECONDS - now
            if remaining <= 0:
                raise WorkerBackendTimeoutError("CTP 初始化 worker 连续 60 秒无进展")
            return remaining
        # 初始化不消耗后续业务的 IPC 预算，业务本身的执行 deadline 不变。
        return deadline + max(0.0, finished - started) - now

    def _receive_response(self, handle, request, timeout, termination_controller):
        if handle.catalog is None and termination_controller is None:
            if not handle.connection.poll(timeout):
                raise WorkerBackendTimeoutError(f"worker backend 响应超时（{timeout}s）")
            return handle.connection.recv(), False
        deadline = clock_monotonic() + timeout
        termination_deadline = None
        while True:
            now = clock_monotonic()
            remaining = self._remaining_response_time(handle, request, deadline, now)
            if remaining <= 0:
                raise WorkerBackendTimeoutError(f"worker backend 响应超时（{timeout}s）")
            if (
                termination_controller is not None
                and termination_controller.is_requested()
                and termination_deadline is None
            ):
                self._queue_control_message(
                    handle,
                    WorkerTerminationSignal(
                        execution_id=request.execution_id or "",
                        reason=termination_controller.reason(),
                        mode=termination_controller.mode(),
                    ),
                )
                termination_deadline = now + _TERMINATION_GRACE_SECONDS
            if termination_deadline is not None and now >= termination_deadline:
                return None, True
            wait_for = min(_TERMINATION_POLL_SECONDS, remaining)
            if termination_deadline is not None:
                wait_for = min(wait_for, max(termination_deadline - now, 0.0))
            if handle.connection.poll(wait_for):
                return handle.connection.recv(), False

    def _force_drop_worker(self, account_id: int, handle: _WorkerBackendHandle) -> None:
        """强制丢弃指定 worker handle 并终止其进程.

        Parameters
        ----------
        account_id : int
            worker 绑定的账户标识。
        handle : _WorkerBackendHandle
            需要终止的具体 handle。

        Notes
        -----
        仅当注册表中仍是同一个 handle 时才移除，避免误删并发重建后的新
        worker。终止走 :meth:`_terminate_process`，不依赖 ``request_lock``，
        可在调用方仍持有该锁时安全执行。
        """
        from axile.server.connection_state import end_connection_instance

        end_connection_instance(getattr(handle, "connection_binding", None), "账户执行器已回收，会话已结束")
        with self._lock:
            handle.retired = True
            self._retire_worker_locked(account_id, handle)
        try:
            self._terminate_process(handle)
        finally:
            self._finish_worker_retirement(account_id, handle)

    def _drop_account_blocking(self, account_id: int) -> None:
        with self._lock:
            handle = self._workers.get(account_id)
            if handle is not None:
                self._retire_worker_locked(account_id, handle)
        if handle is not None:
            try:
                self._dispose_handle(handle)
            finally:
                self._finish_worker_retirement(account_id, handle)

    def _retire_worker_locked(self, account_id: int, handle: _WorkerBackendHandle) -> None:
        """将指定实例移出业务注册表，保留退出前的录制控制，不移除替换后的新实例。"""
        if self._workers.get(account_id) is handle:
            self._workers.pop(account_id, None)
        self._track_worker_retirement_locked(account_id, handle)

    def _finish_worker_retirement(self, account_id: int, handle: _WorkerBackendHandle) -> None:
        """关闭结束后才解除录制控制跟踪，不把移出业务注册表当作已停录。"""
        mirror = getattr(handle, "quota_mirror", None)
        if mirror is not None:
            try:
                if mirror.recover():
                    handle.quota_mirror = None
            except Exception:
                logger.exception("报单额度补写失败，重建 worker 前重试 account_id={}", account_id)
        with self._lock:
            self._finish_worker_retirement_locked(account_id, handle)

    def _track_worker_retirement_locked(self, account_id: int, handle: _WorkerBackendHandle) -> None:
        """持 manager 锁登记仍可能写入录制的旧实例。"""
        if all(item is not handle for item in self._retiring_workers.get(account_id, ())):
            self._retiring_workers.setdefault(account_id, []).append(handle)

    def _finish_worker_retirement_locked(self, account_id: int, handle: _WorkerBackendHandle) -> None:
        if handle.process.is_alive() or getattr(handle, "quota_mirror", None) is not None:
            return
        remaining = [item for item in self._retiring_workers.get(account_id, ()) if item is not handle]
        if remaining:
            self._retiring_workers[account_id] = remaining
        else:
            self._retiring_workers.pop(account_id, None)

    @staticmethod
    def _is_handle_healthy(handle: _WorkerBackendHandle) -> bool:
        return not handle.retired and handle.process.is_alive() and not handle.connection.closed

    def _get_or_create_worker(
        self, account_id: int, request: WorkerBackendRequest | None = None
    ) -> _WorkerBackendHandle:
        while True:
            if request is not None:
                self._check_worker_creation(request)
            with self._lock:
                handle = self._workers.get(account_id)
                if handle is not None and self._is_handle_healthy(handle):
                    return handle
                if handle is not None:
                    self._retire_worker_locked(account_id, handle)
                retiring = list(self._retiring_workers.get(account_id, ()))
                if handle is None and not any(getattr(item, "quota_mirror", None) is not None for item in retiring):
                    handle = self._spawn_worker(account_id)
                    self._workers[account_id] = handle
                    return handle

            # 退出和 SQLite 补写都不能占用所有账户共用的注册表锁。
            try:
                if handle is not None:
                    self._dispose_handle(handle)
            finally:
                for item in retiring:
                    self._finish_worker_retirement(account_id, item)
            with self._lock:
                if any(
                    getattr(item, "quota_mirror", None) is not None
                    for item in self._retiring_workers.get(account_id, ())
                ):
                    raise WorkerBackendExecutionError("旧 worker 报单额度尚未恢复，暂不能重建", retryable=True)

    def _check_worker_creation(self, request: WorkerBackendRequest) -> None:
        """创建进程前检查主动断开意愿，后台准备和迟到的查询不能重新占用进程。"""
        if not request.account_payload or request.command == "disconnect":
            return
        from axile.server.execution.resident_connection import AccountConnectionUnavailable

        account = AccountSnapshot.model_validate(request.account_payload)
        if self._is_manually_disconnected(account) and not (
            request.command == "prepare" and request.payload.get("force_reconnect")
        ):
            raise AccountConnectionUnavailable("账户连接已主动断开，请显式重新连接")

    @staticmethod
    def _is_manually_disconnected(account: AccountContext) -> bool:
        """读取当前配置的主动断开意愿，不用已退出 worker 的状态代替持久化结果。"""
        from axile.server.connection_state import connection_config_key
        from axile.server.native_store import native_operation_sync

        current = native_operation_sync("account_get", {"account_id": _require_account_id(account)})
        if current is not None and current.get("archived_at") is not None:
            from axile.server.execution.resident_connection import AccountConnectionUnavailable

            raise AccountConnectionUnavailable("账户已归档，请先恢复")
        state = current.get("connection_state") if current is not None else None
        return bool(
            state is not None
            and state["config_key"] == connection_config_key(account.trade_channel, account.account_config)
            and state["manually_disconnected"]
        )

    @staticmethod
    def _bind_connection_request(handle: _WorkerBackendHandle, request: WorkerBackendRequest) -> AccountSnapshot:
        """连接观察绑定到当前配置和 worker，不让重试携带已退役实例的令牌。"""
        # 委托与交易使用不同管道；首次并发请求也必须共用一个观察身份。
        with _connection_binding_lock:
            return WorkerBackendManager._bind_connection_request_locked(handle, request)

    @staticmethod
    def _bind_connection_request_locked(handle: _WorkerBackendHandle, request: WorkerBackendRequest) -> AccountSnapshot:
        """在短临界区内解析配置并登记实例；不包含柜台访问或 IPC 等待。"""
        from dataclasses import asdict

        from axile.server.connection_state import begin_connection_instance, connection_config_key
        from axile.server.db.models.account import AccountSnapshot

        account = AccountSnapshot.model_validate(request.account_payload)
        from axile.server.execution.worker_backend.replay import current_replay_config

        request.replay_config = current_replay_config(account)
        key = connection_config_key(account.trade_channel, account.account_config)
        binding = getattr(handle, "connection_binding", None)
        if binding is None or binding.config_key != key:
            handle.connection_binding = begin_connection_instance(account)
            if handle.connection_binding is not None:
                from axile.server.connection_runtime import register_session

                register_session(handle.connection_binding, lambda: not handle.retired and handle.process.is_alive())
                if handle.monitor is not None:
                    handle.monitor.connection_binding = handle.connection_binding
        request.connection_binding = (
            asdict(handle.connection_binding) if handle.connection_binding is not None else None
        )
        return account

    def _accept_worker_response(
        self,
        account_id: int,
        handle: _WorkerBackendHandle,
        request: WorkerBackendRequest,
        response: object,
    ) -> bool:
        """校验并消费业务响应；断开意愿保存成功后才停用该实例。"""
        if not isinstance(response, WorkerBackendResponse):
            raise WorkerBackendExecutionError("worker backend 返回了未知响应类型")
        if response.request_id != request.request_id:
            raise WorkerBackendExecutionError("worker backend 返回了不匹配的 request_id")
        from axile.server.connection_state import consume_connection_snapshot

        consume_connection_snapshot(getattr(response, "connection_snapshot", None))
        if self._requires_ctp_session_recovery(response):
            self._force_drop_worker(account_id, handle)
        elif request.command == "disconnect" and response.kind == "result":
            account = AccountSnapshot.model_validate(request.account_payload)
            if not self._is_manually_disconnected(account):
                raise WorkerBackendExecutionError("主动断开状态未保存，账户进程暂未回收，请重试断开")
            with self._lock:
                self._retire_worker_locked(account_id, handle)
            return True
        return False

    def _request_blocking(
        self,
        account_id: int,
        request: WorkerBackendRequest,
        timeout: float,
        termination_controller: ExecutionTerminationController | None = None,
    ) -> WorkerBackendResponse:
        """向 worker 发送请求并在超时约束下阻塞等待响应.

        Parameters
        ----------
        account_id : int
            目标账户标识。
        request : WorkerBackendRequest
            待发送的请求载荷。
        timeout : float
            等待响应的超时秒数；超时视为 worker 卡死并强制终止其进程。

        Returns
        -------
        WorkerBackendResponse
            worker 返回的响应载荷。

        Raises
        ------
        WorkerBackendTimeoutError
            worker 在超时内未返回响应（进程存活但业务卡死）。
        WorkerBackendExecutionError
            通信失败或响应类型/标识不合法。
        """
        from axile.server.execution.resident_connection import AccountConnectionUnavailable

        disconnecting = False
        while True:
            try:
                handle = self._get_or_create_worker(account_id, request)
            except AccountConnectionUnavailable:
                if request.command != "disconnect":
                    raise
                return WorkerBackendResponse(request.request_id, "result", {"disconnected": True})
            response: object = None
            failure: Exception | None = None
            force_termination = False
            with handle.request_lock:
                if handle.retired or self._workers.get(account_id) is not handle:
                    continue
                if request.account_payload:
                    account_snapshot = self._bind_connection_request(handle, request)

                    with self._lock:
                        known_account = account_id in self._catalog_accounts
                    if not known_account:
                        self.configure_catalog_account(account_snapshot)
                    if request.command in {"execute_trade", "empty_positions"}:
                        self._record_catalog_execution(account_id)
                try:
                    handle.connection.send(request)
                    response, force_termination = self._receive_response(
                        handle, request, timeout, termination_controller
                    )
                except (BrokenPipeError, EOFError, OSError, WorkerBackendTimeoutError) as exc:
                    failure = exc

                if failure is None and not force_termination:
                    disconnecting = self._accept_worker_response(account_id, handle, request, response)
            break

        if force_termination:
            assert termination_controller is not None
            self._force_drop_worker(account_id, handle)
            termination_controller.acknowledge_if_requested()
            raise ExecutionTerminated(
                reason=termination_controller.reason(),
                mode=termination_controller.mode(),
                acked_at=termination_controller.acked_at,
                forced=True,
                cancel_attempted=None,
                cancel_unconfirmed=termination_controller.mode() == "cancel_pending",
            )

        # 兜底清理必须在释放 request_lock 之后进行：_force_drop_worker /
        # _terminate_process 都不再获取该锁，避免与当前线程二次死锁。
        if failure is not None:
            from axile.server.connection_state import end_connection_instance

            message = (
                "账户请求超时，执行器已回收"
                if isinstance(failure, WorkerBackendTimeoutError)
                else "账户执行器通信失败，会话已结束"
            )
            end_connection_instance(getattr(handle, "connection_binding", None), message, failure)
            if isinstance(failure, WorkerBackendTimeoutError):
                self._force_drop_worker(account_id, handle)
                raise failure
            # EOFError 的字符串为空；先等待极短时间收割已退出子进程，尽量把退出码带回诊断。
            handle.process.join(timeout=0.1)
            exitcode = handle.process.exitcode
            self._force_drop_worker(account_id, handle)
            failure_detail = str(failure) or repr(failure)
            raise WorkerBackendExecutionError(
                "worker backend 通信失败: "
                f"{failure.__class__.__name__}: {failure_detail}; "
                f"request_id={request.request_id}; worker_exitcode={exitcode}"
            ) from failure

        if disconnecting:
            try:
                self._dispose_handle(handle)
            finally:
                self._finish_worker_retirement(account_id, handle)
        return cast("WorkerBackendResponse", response)

    @staticmethod
    def _termination_controller(execution_id: str | None) -> ExecutionTerminationController | None:
        """从主进程 execution registry 解析本次请求的终止控制器."""
        if execution_id is None:
            return None
        from axile.server.execution.registry import create_termination_controller, get_execution_task_state

        state = get_execution_task_state(execution_id)
        if state is None or state.cancel_event is None:
            return None
        return create_termination_controller(execution_id, state.cancel_event)

    @staticmethod
    def _requires_ctp_session_recovery(response: WorkerBackendResponse) -> bool:
        return response.requires_worker_restart or (
            response.kind == "error"
            and response.channel_type == TradeChannel.CTP
            and response.error is not None
            and response.error.type == _CTP_SESSION_RECOVERY_ERROR_TYPE
        )

    @staticmethod
    def _build_output(response: WorkerBackendResponse) -> UnifiedStandardOutput:
        if response.output_payload is None:
            raise WorkerBackendExecutionError("worker backend 未返回执行结果")
        return UnifiedStandardOutput.model_validate(response.output_payload)

    @staticmethod
    def _build_failed_output(
        error: WorkerBackendErrorPayload | None,
        channel_type: TradeChannel | None = None,
    ) -> UnifiedStandardOutput:
        message = "worker backend 执行失败"
        extra: dict[str, object] = {}
        if error is not None:
            message = error.message or message
            worker_error = {
                "type": error.type,
                "message": error.message,
                "retryable": error.retryable,
                "technical_detail": getattr(error, "technical_detail", None),
                "traceback": getattr(error, "traceback", None),
                "reason_code": error.reason_code,
                "error_id": error.error_id,
                "diagnostics": error.diagnostics or None,
                "exception_type": error.exception_type,
                "retry_after": error.retry_after or None,
            }

            extra["worker_error"] = {key: value for key, value in worker_error.items() if value is not None}

        return UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets.unavailable(),
            memory={"message": message},
            inputs=None,
            symbol_results={},
            outcome=ExecutionOutcome.ERROR,
            status=ExecutionStatus.FAILED,
            error=message,
            outcome_reason=message,
            execution_time=0.0,
            channel_type=channel_type or TradeChannel.GM,
            success=False,
            extra=extra,
        )

    @staticmethod
    def _handle_response(response: WorkerBackendResponse) -> UnifiedStandardOutput:
        if response.kind == "result":
            return WorkerBackendManager._build_output(response)
        if response.kind == "terminated":
            raise ExecutionTerminated(
                reason=response.reason,
                mode=response.mode,
                acked_at=response.acked_at,
                trigger=response.trigger,
                cancel_failed_order_ids=response.cancel_failed_order_ids,
                forced=response.forced,
                cancel_attempted=response.cancel_attempted,
                cancel_unconfirmed=response.cancel_unconfirmed,
                cancel_details=response.cancel_details,
                cancel_outcomes=response.cancel_outcomes,
            )
        if response.kind == "error":
            return WorkerBackendManager._build_failed_output(
                response.error,
                channel_type=response.channel_type,
            )
        raise WorkerBackendExecutionError("worker backend 返回了未知响应状态")

    async def prepare_account(
        self,
        account: AccountContext,
        expected_trading_day: str | None = None,
        *,
        execution_id: str | None = None,
        termination_controller: ExecutionTerminationController | None = None,
        force_reconnect: bool = False,
    ) -> dict[str, object]:
        """创建或复用账户 Worker 中的长连接执行器。"""
        current = self._current_session(account)
        if not force_reconnect and not current["worker_alive"] and current["retry_after_seconds"]:
            from axile.server.execution.resident_connection import AccountConnectionUnavailable

            raise AccountConnectionUnavailable(
                "账户连接恢复冷却中，请稍后重试", retry_after=current["retry_after_seconds"]
            )
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="prepare",
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload={"expected_trading_day": expected_trading_day or "", "force_reconnect": force_reconnect},
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            _DEFAULT_PREPARE_RECV_TIMEOUT_SECONDS,
            termination_controller,
        )
        if response.kind == "terminated":
            self._handle_response(response)
        if response.kind != "result" or response.output_payload is None:
            raise WorkerBackendExecutionError.from_payload(response.error, "账户通道准备失败")
        return response.output_payload

    async def drop_idle_account(self, account_id: int) -> bool:
        """盘前回收空闲账户的 worker；有在途请求或调仓时跳过。"""
        return await asyncio.to_thread(self._drop_idle_account_blocking, account_id)

    def _drop_idle_account_blocking(self, account_id: int) -> bool:
        from axile.server.execution.registry import get_queued_execution_id, get_running_execution_id

        with self._lock:
            if get_queued_execution_id(account_id) or get_running_execution_id(account_id):
                return False
            handle = self._workers.get(account_id)
            if handle is None:
                return True
            if not handle.request_lock.acquire(blocking=False):
                return False
            handle.retired = True
            handle.request_lock.release()
            # 旧会话先退出，防止同一账户在关闭期间另起连接。
            self._retire_worker_locked(account_id, handle)
        try:
            self._dispose_handle(handle)
        finally:
            self._finish_worker_retirement(account_id, handle)
        return True

    async def drop_account(self, account_id: int) -> None:
        """关闭并移除指定账户的常驻 Worker。"""
        await asyncio.to_thread(self._drop_account_blocking, account_id)

    async def disconnect_account(self, account: AccountContext) -> None:
        """断开 SDK 会话并回收账户进程，保留主动断开标记以阻止后台重建。"""
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="disconnect",
            account_payload=account.model_dump(mode="json"),
            execution_id=None,
            payload={},
        )
        response = await asyncio.to_thread(
            self._request_blocking, _require_account_id(account), request, max(15.0, self._shutdown_recv_timeout)
        )
        if response.kind != "result":
            raise WorkerBackendExecutionError.from_payload(response.error, "账户连接断开失败")

    async def order_command(self, account: AccountContext, payload: dict[str, object]) -> dict[str, object]:
        """通过独立通路查询或撤销委托，不占用调仓命令锁。"""
        return await asyncio.to_thread(self._order_command_blocking, account, payload)

    def _order_command_blocking(self, account: AccountContext, payload: dict[str, object]) -> dict[str, object]:
        """委托通路只复用当前配置下已经就绪的会话。"""
        self._check_read_session(account)
        timeout = self._query_timeout(account)
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="prepare",
            account_payload=account.model_dump(mode="json"),
            execution_id=None,
            payload={},
        )
        handle = self._get_or_create_worker(_require_account_id(account), request)
        if handle.orders is None:
            raise WorkerBackendExecutionError("当前委托通路不可用")
        self._bind_connection_request(handle, request)
        try:
            result = handle.orders.request(
                {**payload, "account": request.account_payload, "connection_binding": request.connection_binding},
                timeout=timeout,
            )
            from axile.server.orders_live import consume_orders_snapshot

            consume_orders_snapshot(result.pop("orders_snapshot", None))
            return result
        except TimeoutError as exc:
            self._retire_idle_order_timeout(handle, exc)
            raise

    def _retire_idle_order_timeout(self, handle: _WorkerBackendHandle, error: TimeoutError) -> None:
        """回收空闲且超时的通路；与交易命令互斥，绝不回收正在执行的账户。"""
        from axile.server.connection_state import end_connection_instance
        from axile.server.execution.registry import get_queued_execution_id, get_running_execution_id

        if not handle.request_lock.acquire(blocking=False):
            return
        try:
            if get_running_execution_id(handle.account_id) or get_queued_execution_id(handle.account_id):
                return
            end_connection_instance(handle.connection_binding, "账户委托请求超时，空闲执行器已回收", error)
            self._force_drop_worker(handle.account_id, handle)
        finally:
            handle.request_lock.release()

    @staticmethod
    def _current_session(account: AccountContext) -> dict[str, Any]:
        from axile.server.connection_runtime import current_session
        from axile.server.connection_state import connection_config_key

        return current_session(
            _require_account_id(account), connection_config_key(account.trade_channel, account.account_config)
        )

    @staticmethod
    def _check_read_session(account: AccountContext) -> None:
        """查询必须复用已就绪会话，首次登录也只能由明确操作触发。"""
        from axile.server.execution.resident_connection import AccountConnectionUnavailable

        current = WorkerBackendManager._current_session(account)
        if not current["worker_alive"] or current["status"] != "ready":
            raise AccountConnectionUnavailable(
                "账户尚未连接或会话未就绪，请先连接账户", retry_after=current["retry_after_seconds"]
            )

    @staticmethod
    def _query_timeout(account: AccountContext) -> float:
        """查询不再包含登录准备预算。"""
        return _ACCOUNT_ASSET_RECV_TIMEOUT_SECONDS

    async def get_account_assets(self, account: AccountContext) -> UnifiedAccountAssets:
        """通过账户常驻 worker 查询最新资产快照."""
        self._check_read_session(account)
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="get_account_assets",
            account_payload=account.model_dump(mode="json"),
            execution_id=None,
            payload={},
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            self._query_timeout(account),
        )
        if response.kind != "result" or response.output_payload is None:
            raise WorkerBackendExecutionError.from_payload(response.error, "账户资产查询失败")
        return UnifiedAccountAssets.model_validate(response.output_payload)

    async def get_account_cash_flows(self, account: AccountContext) -> list[dict]:
        """通过已有就绪会话读取渠道流水，不创建交易执行。"""
        from axile.executor.models.cash_flow import ChannelCashFlow
        from axile.server.native_store import native_call

        self._check_read_session(account)
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="get_account_cash_flows",
            account_payload=account.model_dump(mode="json"),
            execution_id=None,
            payload={},
        )
        response = await native_call(
            self._request_blocking, _require_account_id(account), request, self._query_timeout(account)
        )
        if response.kind != "result" or response.output_payload is None:
            raise WorkerBackendExecutionError.from_payload(response.error, "账户流水查询失败")
        rows = response.output_payload.get("rows")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("渠道流水响应无效")
        return [ChannelCashFlow.model_validate(row).model_dump(mode="json") for row in rows]

    async def query_instruments(
        self,
        account: AccountContext,
        symbols: list[str],
        *,
        timeout: float = 5.0,
        require_specs: bool = False,
        use_cache: bool = True,
    ) -> dict[str, dict[str, object]]:
        """精确资料查询复用当前目录管道，不运行组合函数或创建交易 worker。"""
        from axile.server.execution.resident_connection import AccountConnectionUnavailable

        self._check_read_session(account)
        with self._lock:
            handle = self._workers.get(_require_account_id(account))
            if (
                handle is None
                or not self._is_handle_healthy(handle)
                or handle.catalog is None
                or handle.catalog.source is None
            ):
                raise AccountConnectionUnavailable("账户目录会话尚未就绪，请先连接账户")
            source = handle.catalog.source
        query = self.instrument_catalog.ensure if use_cache else self.instrument_catalog.query_source
        rows = await asyncio.to_thread(query, symbols, timeout=timeout, require_specs=require_specs, source_name=source)
        return {symbol: row.wire() for symbol, row in rows.items()}

    async def calculate_portfolio(
        self,
        account: AccountContext,
        code: str,
        *,
        execution_id: str | None = None,
        leverage_overrides: dict[str, float] | None = None,
    ) -> PortfolioTarget:
        """通过账户常驻 worker 执行自定义组合函数，失败后丢弃 worker."""
        if execution_id is None:
            self._check_read_session(account)
        termination_controller = self._termination_controller(execution_id)
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="calculate_portfolio",
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload={"code": code, **({"leverage_overrides": leverage_overrides} if leverage_overrides else {})},
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            PORTFOLIO_FUNCTION_TIMEOUT_SECONDS + PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS,
            termination_controller,
        )
        if response.kind != "result" or response.output_payload is None:
            if not self._requires_ctp_session_recovery(response):
                await self.drop_account(_require_account_id(account))
            raise WorkerBackendExecutionError.from_payload(response.error, "自定义组合函数执行失败")
        try:
            result = response.output_payload["portfolio_result"]
            if isinstance(result, PortfolioFunctionError):
                raise result
            return _normalize_target(result)
        except Exception:
            await self.drop_account(_require_account_id(account))
            raise

    async def execute_trade(
        self,
        *,
        account: AccountContext,
        standard_input_dict: dict[str, object],
        audit_input: dict[str, object],
        execution_id: str | None,
        trigger_source: str,
        notification_snapshot: dict[str, object] | None = None,
    ) -> tuple[UnifiedStandardOutput, dict[str, object] | None]:
        """
        通过多进程 worker 执行调仓请求.

        Parameters
        ----------
        account : Account
            当前执行所属账户。
        standard_input_dict : dict[str, object]
            发送给 worker 的标准输入字典快照。
        audit_input : dict[str, object]
            预先脱敏后的审计输入。
        execution_id : str | None
            当前 execution 标识。
        trigger_source : str
            触发来源，例如手动执行或调度触发。

        Returns
        -------
        tuple[UnifiedStandardOutput, dict[str, object] | None]
            多进程 worker 返回的执行结果及归一化 symbol 字段。
        """
        termination_controller = self._termination_controller(execution_id)
        await self.prepare_account(
            account,
            execution_id=execution_id,
            termination_controller=termination_controller,
        )
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="execute_trade",
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload={
                "standard_input": standard_input_dict,
                "audit_input": audit_input,
                "trigger_source": trigger_source,
                "notification_snapshot": notification_snapshot,
            },
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            _resolve_worker_recv_timeout(
                standard_input_dict.get("execution_timeout"),
                fallback=self._execute_recv_timeout,
            ),
            termination_controller,
        )
        return self._handle_response(response), response.normalized_symbol_fields

    async def empty_positions(
        self,
        *,
        account: AccountContext,
        empty_kwargs: dict[str, object],
        audit_input: dict[str, object],
        execution_id: str,
        notification_snapshot: dict[str, object] | None = None,
    ) -> UnifiedStandardOutput:
        """
        通过多进程 worker 执行清仓请求.

        Parameters
        ----------
        account : Account
            当前执行所属账户。
        empty_kwargs : dict[str, object]
            清仓执行所需的参数字典。
        audit_input : dict[str, object]
            预先脱敏后的审计输入。
        execution_id : str
            当前 execution 标识。

        Returns
        -------
        UnifiedStandardOutput
            多进程 worker 返回并经统一封装后的清仓结果。
        """
        termination_controller = self._termination_controller(execution_id)
        await self.prepare_account(
            account,
            execution_id=execution_id,
            termination_controller=termination_controller,
        )
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="empty_positions",
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload={
                "empty_kwargs": empty_kwargs,
                "audit_input": audit_input,
                "notification_snapshot": notification_snapshot,
            },
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            _resolve_worker_recv_timeout(
                empty_kwargs.get("execution_timeout"),
                fallback=self._execute_recv_timeout,
            ),
            termination_controller,
        )
        # 清仓结束只收尾本次 execution；当前委托查询仍需复用账户登录会话。
        return self._handle_response(response)

    def shutdown_all(self) -> None:
        """显式关闭所有多进程 worker。."""
        self.instrument_catalog.close()
        with self._lock:
            workers = list(self._workers.items())
            self._workers.clear()
            for account_id, retiring in self._retiring_workers.items():
                workers.extend(
                    (account_id, handle) for handle in retiring if all(item is not handle for _, item in workers)
                )
            for account_id, handle in workers:
                self._track_worker_retirement_locked(account_id, handle)
        for account_id, handle in workers:
            try:
                # 服务关停时不能等待仍在后台恢复的请求锁；其线程会随 worker 退出而解除等待。
                self._dispose_handle(handle, lock_timeout=self._shutdown_recv_timeout)
            finally:
                self._finish_worker_retirement(account_id, handle)
        for account_id, handle in workers:
            if handle.replay_draining and handle.process.is_alive():
                handle.process.join()
                self._finish_worker_retirement(account_id, handle)
        if any(getattr(handle, "quota_mirror", None) is not None for _, handle in workers):
            raise WorkerBackendExecutionError("关停时仍有报单额度未恢复")

    def close(self) -> None:
        """关闭所有多进程 worker，兼容 atexit 钩子。."""
        self.shutdown_all()


def get_worker_backend_manager() -> WorkerBackendManager:
    """
    返回全局多进程 worker manager 单例。.

    Returns
    -------
    WorkerBackendManager
        当前进程内复用的多进程 worker manager。
    """
    global _worker_backend_manager
    if _worker_backend_manager is None:
        _worker_backend_manager = WorkerBackendManager()
    return _worker_backend_manager


def shutdown_worker_backend_manager() -> None:
    """
    关闭全局多进程 worker manager，并释放其持有的 worker 进程。.

    Returns
    -------
    None
        该函数仅执行资源清理，不返回结果。
    """
    global _worker_backend_manager
    if _worker_backend_manager is None:
        return

    _worker_backend_manager.shutdown_all()
    _worker_backend_manager = None
