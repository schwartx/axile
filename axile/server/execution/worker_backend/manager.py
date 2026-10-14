"""多进程执行 worker 管理器。."""

from __future__ import annotations

import asyncio
import atexit
from dataclasses import dataclass, field
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext, SpawnProcess
from threading import Lock
from typing import cast
from uuid import uuid4

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.executor.instrument_catalog import InstrumentCatalog
from axile.executor.models.execution_result import ExecutionOutcome, ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from axile.server.account_control.ipc import RemoteMonitor
from axile.server.db.models.account import AccountContext
from axile.server.execution.worker_backend.catalog import CatalogSession
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendErrorPayload,
    WorkerBackendRequest,
    WorkerBackendResponse,
    WorkerTerminationSignal,
)
from axile.server.execution.worker_backend.worker import run_worker_backend_loop
from axile.server.portfolio_function import PortfolioFunctionResult
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
    ) -> None:
        """保留 IPC 的安全展示原因与机器码，不从异常原文推断业务状态。"""
        super().__init__(message)
        self.execution_error = execution_error
        self.reason_code = reason_code
        self.error_id = error_id
        self.retryable = retryable


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

_worker_backend_manager: "WorkerBackendManager | None" = None


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


class WorkerBackendManager:
    """维护一账户一多进程 worker 的最小管理器。.

    Notes
    -----
    **常驻设计（有意为之，勿轻易加空闲回收/数量上限）**：

    渠道插件可声明 ``PROCESS`` backend（见 ``dispatch.py``），用于承载进程绑定、
    全局上下文隔离或多线程不安全的 SDK。每个此类账户首次执行时 spawn 一个常驻
    worker，把登录会话**焐热**并在后续执行间复用；这正是插件选择 PROCESS 而非
    THREAD 的目的。因此「成功执行后不回收」是期望行为，不是疏漏。

    ``_workers`` 以 ``account_id`` 为 key，上界 = 进程存活期内执行过的**不同
    PROCESS 账户数**（有限稳定集合），并非无界增长。worker 仅在通信失败/超时
    （:meth:`_force_drop_worker`）、健康检查失败时替换、或 :meth:`shutdown_all`
    时释放；**刻意没有空闲 TTL 回收，也没有容量上限**。

    该取舍绑定当前运营规模：并发活跃 PROCESS 账户 1~3、执行节奏最短 15 分钟。此时
    常驻进程上界≈3（噪声级内存），而 15 分钟节奏下会话被高频复用，任何空闲
    回收都会用重登录延迟/频控风险换取几乎为零的内存收益，属净亏。

    **重新评估的阈值**（满足任一再引入「惰性 TTL 回收 + LRU 软上限」）：
    并发活跃 PROCESS 账户 ≳ 20；或出现账户 churn（频繁建号→跑一次→弃用，导致
    ``account_id`` 只出现一次、进程再不被复用）。届时回收逻辑必须对
    ``request_lock`` 做非阻塞抢占并跳过在途 worker，**绝不回收正在执行的
    进程**。更精确的做法是在账户删除/换渠道的清理钩子里事件驱动地调用
    :meth:`_force_drop_worker`，优于定时扫空闲。
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
        self.instrument_catalog = InstrumentCatalog()
        self._catalog_accounts: dict[int, tuple[str, bool, str]] = {}
        self._execute_recv_timeout = execute_recv_timeout
        self._shutdown_recv_timeout = shutdown_recv_timeout
        atexit.register(self.close)

    def _spawn_worker(self, account_id: int) -> _WorkerBackendHandle:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        child_control_conn, parent_control_conn = self._ctx.Pipe(duplex=False)
        parent_catalog, child_catalog = self._ctx.Pipe(duplex=True)
        parent_monitor, child_monitor = self._ctx.Pipe(duplex=True)
        process = self._ctx.Process(
            target=run_worker_backend_loop,
            args=(child_conn, account_id, child_control_conn, child_catalog, child_monitor),
            name=f"axile-execution-worker-{account_id}",
            daemon=True,
        )
        process.start()
        child_conn.close()
        child_control_conn.close()
        child_catalog.close()
        child_monitor.close()
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

    def _record_catalog_execution(self, account_id: int) -> None:
        """只更新使用时间，避免在途请求的旧快照重新启用已暂停来源。"""
        with self._lock:
            channel, enabled, _ = self._catalog_accounts[account_id]
            latest = clock_now_iso().replace(" ", "T")
            self._catalog_accounts[account_id] = channel, enabled, latest
            handle = self._workers.get(account_id)
            if handle is not None and handle.catalog is not None:
                handle.catalog.configure(channel=channel, enabled=enabled, last_execution=latest)

    def _send_shutdown(self, handle: _WorkerBackendHandle, *, lock_timeout: float = -1) -> None:
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
        try:
            handle.control_connection.close()
        except OSError:
            pass
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
        if handle.catalog is not None:
            handle.catalog.close()

    def _dispose_handle(self, handle: _WorkerBackendHandle, *, lock_timeout: float = -1) -> None:
        try:
            if not handle.connection.closed and handle.process.is_alive():
                self._send_shutdown(handle, lock_timeout=lock_timeout)
        except (BrokenPipeError, EOFError, OSError, WorkerBackendExecutionError):
            pass
        try:
            handle.connection.close()
        except OSError:
            pass
        try:
            handle.control_connection.send(None)
        except (BrokenPipeError, EOFError, OSError):
            pass
        try:
            handle.control_connection.close()
        except OSError:
            pass
        if handle.process.is_alive():
            handle.process.join(timeout=2.0)
        if handle.process.is_alive():
            handle.process.terminate()
            handle.process.join(timeout=2.0)
        if handle.monitor is not None:
            handle.monitor.close()
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
                handle.control_connection.send(
                    WorkerTerminationSignal(
                        execution_id=request.execution_id or "",
                        reason=termination_controller.reason(),
                        mode=termination_controller.mode(),
                    )
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
        with self._lock:
            handle.retired = True
            if self._workers.get(account_id) is handle:
                self._workers.pop(account_id, None)
        self._terminate_process(handle)

    def _drop_account_blocking(self, account_id: int) -> None:
        with self._lock:
            handle = self._workers.pop(account_id, None)
        if handle is not None:
            self._dispose_handle(handle)

    @staticmethod
    def _is_handle_healthy(handle: _WorkerBackendHandle) -> bool:
        return not handle.retired and handle.process.is_alive() and not handle.connection.closed

    def _get_or_create_worker(self, account_id: int) -> _WorkerBackendHandle:
        with self._lock:
            handle = self._workers.get(account_id)
            if handle is not None and self._is_handle_healthy(handle):
                return handle

            if handle is not None:
                self._dispose_handle(handle)

            handle = self._spawn_worker(account_id)
            self._workers[account_id] = handle
            return handle

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
        while True:
            handle = self._get_or_create_worker(account_id)
            response: object = None
            failure: Exception | None = None
            force_termination = False
            with handle.request_lock:
                if handle.retired:
                    continue
                if request.account_payload:
                    from axile.server.db.models.account import AccountSnapshot

                    with self._lock:
                        known_account = account_id in self._catalog_accounts
                    if not known_account:
                        self.configure_catalog_account(AccountSnapshot.model_validate(request.account_payload))
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
                    if not isinstance(response, WorkerBackendResponse):
                        raise WorkerBackendExecutionError("worker backend 返回了未知响应类型")
                    if response.request_id != request.request_id:
                        raise WorkerBackendExecutionError("worker backend 返回了不匹配的 request_id")
                    if self._requires_ctp_session_recovery(response):
                        self._force_drop_worker(account_id, handle)
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
            extra["worker_error"] = {
                "type": error.type,
                "message": error.message,
                "retryable": error.retryable,
            }

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
    ) -> dict[str, object]:
        """创建或复用账户 Worker 中的长连接执行器。"""
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="prepare",
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload={"expected_trading_day": expected_trading_day or ""},
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            _DEFAULT_PREPARE_RECV_TIMEOUT_SECONDS,
            termination_controller,
        )
        if response.kind != "result" or response.output_payload is None:
            message = response.error.message if response.error is not None else "账户通道准备失败"
            error = response.error
            raise WorkerBackendExecutionError(
                message,
                execution_error=message,
                reason_code=error.reason_code if error else None,
                error_id=error.error_id if error else None,
                retryable=error.retryable if error else False,
            )
        return response.output_payload

    async def drop_account(self, account_id: int) -> None:
        """关闭并移除指定账户的常驻 Worker。"""
        await asyncio.to_thread(self._drop_account_blocking, account_id)

    async def get_account_assets(self, account: AccountContext) -> UnifiedAccountAssets:
        """通过账户常驻 worker 查询最新资产快照."""
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
            _ACCOUNT_ASSET_RECV_TIMEOUT_SECONDS,
        )
        if response.kind != "result" or response.output_payload is None:
            message = response.error.message if response.error is not None else "账户资产查询失败"
            raise WorkerBackendExecutionError(message)
        return UnifiedAccountAssets.model_validate(response.output_payload)

    async def calculate_portfolio(
        self,
        account: AccountContext,
        code: str,
        *,
        execution_id: str | None = None,
    ) -> PortfolioFunctionResult:
        """通过账户常驻 worker 执行自定义组合函数，失败后丢弃 worker."""
        termination_controller = self._termination_controller(execution_id)
        request = WorkerBackendRequest(
            request_id=uuid4().hex,
            command="calculate_portfolio",
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload={"code": code},
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            PORTFOLIO_FUNCTION_TIMEOUT_SECONDS + PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS,
            termination_controller,
        )
        if response.kind != "result" or response.output_payload is None:
            message = response.error.message if response.error is not None else "自定义组合函数执行失败"
            if not self._requires_ctp_session_recovery(response):
                await self.drop_account(_require_account_id(account))
            raise WorkerBackendExecutionError(message)
        try:
            result = PortfolioFunctionResult.from_payload(response.output_payload)
        except Exception:
            await self.drop_account(_require_account_id(account))
            raise
        if not result.ok:
            await self.drop_account(_require_account_id(account))
        return result

    async def execute_trade(
        self,
        *,
        account: AccountContext,
        standard_input: UnifiedStandardInput,
        standard_input_dict: dict[str, object],
        audit_input: dict[str, object],
        execution_id: str | None,
        trigger_source: str,
        cleanup: bool,
        notification_snapshot: dict[str, object] | None = None,
    ) -> tuple[UnifiedStandardOutput, dict[str, object] | None]:
        """
        通过多进程 worker 执行调仓请求.

        Parameters
        ----------
        account : Account
            当前执行所属账户。
        standard_input : UnifiedStandardInput
            结构化标准输入对象。
        standard_input_dict : dict[str, object]
            发送给 worker 的标准输入字典快照。
        audit_input : dict[str, object]
            预先脱敏后的审计输入。
        execution_id : str | None
            当前 execution 标识。
        trigger_source : str
            触发来源，例如手动执行或调度触发。
        cleanup : bool
            执行结束后是否要求 worker 做清理。

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
                "cleanup": cleanup,
                "notification_snapshot": notification_snapshot,
            },
        )
        response = await asyncio.to_thread(
            self._request_blocking,
            _require_account_id(account),
            request,
            _resolve_worker_recv_timeout(
                standard_input.execution_timeout,
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
        result = self._handle_response(response)
        if not account.is_started and not self._requires_ctp_session_recovery(response):
            await self.drop_account(_require_account_id(account))
        return result

    def shutdown_all(self) -> None:
        """显式关闭所有多进程 worker。."""
        self.instrument_catalog.close()
        with self._lock:
            workers = list(self._workers.items())
            self._workers.clear()
        for _account_id, handle in workers:
            # 服务关停时不能等待仍在后台恢复的请求锁；其线程会随 worker 退出而解除等待。
            self._dispose_handle(handle, lock_timeout=self._shutdown_recv_timeout)

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
