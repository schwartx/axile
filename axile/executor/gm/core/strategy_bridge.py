"""
GM 策略框架桥接器.

在后台线程中运行掘金策略框架的事件循环，并将订单、成交与行情事件转发到统一分发器。

Notes
-----
该桥接器负责在独立线程中启动 ``gm.api.run()``，避免阻塞主线程。
订单状态与成交回报可以稳定转发，但 GM SDK 的实时行情订阅在后台线程中存在已知限制。
如需获取实时行情快照，建议通过 ``GMExecutor.get_market_data()`` 调用 ``current()``。
"""

import os
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol, TypeAlias

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_monotonic, get_default_clock
from axile.executor.connection_state import CONNECTION_ERROR_SCOPE, ConnectionMonitor, preserve_connection_failure
from axile.executor.gm.connection import report_gm_failure, report_runtime_failure
from axile.executor.gm.core.api_bridge import GMBridgeRequestPayload, GMSdkRequest, GMSubscribeSymbolsRequest
from axile.executor.gm.core.bridge_context import (
    GMStartupHistoryEntry,
    GMStartupStateValue,
    clear_gm_strategy_runtime_context,
    install_gm_strategy_runtime_context,
)
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData

GMBridgeScalar: TypeAlias = str | int | float | bool | None
GMBridgeResultMapping: TypeAlias = dict[str, GMBridgeScalar]
GMBridgeResultItem: TypeAlias = GMBridgeScalar | GMBridgeResultMapping
GMBridgeResult: TypeAlias = GMBridgeScalar | GMBridgeResultMapping | list[GMBridgeResultItem]
SignalHandler: TypeAlias = int | Callable[[int, FrameType | None], None] | None


class GMBridgeStartupError(RuntimeError):
    """GM bridge 启动失败."""

    execution_error: str
    error_scope: str


class GMAuthenticationError(GMBridgeStartupError):
    """GM token 无效或已失效."""


def _normalize_startup_error(error: Exception) -> GMBridgeStartupError:
    """把 GM SDK 启动异常收敛为安全、稳定的错误类型."""
    status = getattr(error, "status", None)
    message = str(error)
    if status == 1000 or isinstance(error, GMAuthenticationError):
        normalized = GMAuthenticationError("GM token 无效或已失效")
        normalized.execution_error = "GM token 无效或已失效"
    else:
        normalized = GMBridgeStartupError(f"GM bridge 启动失败: {message or error.__class__.__name__}")
        normalized.execution_error = "GM 启动失败，具体原因未确认"
    normalized.error_scope = CONNECTION_ERROR_SCOPE
    preserve_connection_failure(normalized, error)
    return normalized


class GMBridgeEventSink(Protocol):
    """GM bridge 可写入的事件 sink 协议."""

    def dispatch_order_update(self, order: UnifiedOrder) -> None:
        """分发订单更新事件."""

    def dispatch_trade_record(self, trade: TradeRecord) -> None:
        """分发成交记录事件."""

    def dispatch_price_data(self, price_data: UnifiedPriceData) -> None:
        """分发行情数据事件."""


@dataclass(slots=True)
class GMBridgeRequest:
    """
    Bridge 线程里执行的单个 GM 请求.

    Attributes
    ----------
    request : GMBridgeRequestPayload
        待在 GM runtime 上下文中执行的类型化请求。
    future : Future[GMBridgeResult]
        回填执行结果的 Future；调用方超时后会将其取消。
    deadline : float | None
        请求的绝对过期时刻（``time.monotonic()`` 时基）；``None`` 表示不过期。
        消费端在真正调用 SDK 前会二次校验该字段，兜住「调用方刚超时、
        消费端已把请求取出队列」这段无法用取消标志覆盖的竞态窗口。
    """

    request: GMBridgeRequestPayload
    future: Future[GMBridgeResult]
    deadline: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        """
        判断请求是否已越过 deadline.

        Parameters
        ----------
        now : float | None, optional
            用于比较的当前时刻（``time.monotonic()`` 时基）；缺省时取当前时刻。

        Returns
        -------
        bool
            已过期返回 ``True``；未设置 deadline 时恒为 ``False``。
        """
        if self.deadline is None:
            return False
        return (time.monotonic() if now is None else now) >= self.deadline


class GMStrategyBridge:
    """
    GM 策略框架桥接器.

    在后台线程中运行掘金策略框架，并接收订单、成交与行情回调。

    Examples
    --------
    >>> dispatcher = GMCallbackDispatcher()
    >>> bridge = GMStrategyBridge(
    ...     token="your_token",
    ...     account_id="your_account_id",
    ...     callback_dispatcher=dispatcher,
    ...     serv_addr="127.0.0.1:7001",
    ... )
    >>> bridge.start()
    >>> bridge.stop()
    """

    def __init__(
        self,
        token: str,
        account_id: str,
        callback_dispatcher: GMBridgeEventSink,
        strategy_id: str | None = None,
        serv_addr: str | None = None,
        subscribe_symbols: list[str] | None = None,
    ) -> None:
        """
        初始化策略桥接器.

        Parameters
        ----------
        token : str
            掘金终端令牌。
        account_id : str
            掘金账户 ID。
        callback_dispatcher : GMBridgeEventSink
            用于接收统一订单、成交和行情事件的分发器。
        strategy_id : str | None, optional
            策略 ID；未提供时自动生成。
        serv_addr : str | None, optional
            掘金服务地址。
        subscribe_symbols : list[str] | None, optional
            启动后需要订阅的标的列表。
        """
        self._token = token
        self._account_id = account_id
        self._callback_dispatcher = callback_dispatcher
        self._strategy_id = strategy_id or f"axile_callback_{int(time.time())}"
        self._serv_addr = serv_addr
        self._subscribe_symbols = subscribe_symbols or []
        self.connection_monitor = ConnectionMonitor()

        # 线程控制
        self._running = False
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._startup_error: Exception | None = None
        self._runtime_stop_requested = threading.Event()
        self._runtime_stop_lock = threading.Lock()
        self._request_queue: queue.Queue[GMBridgeRequest] = queue.Queue()

        # 统计信息
        self._stats = {
            "order_status_received": 0,
            "execution_report_received": 0,
            "tick_received": 0,
            "errors": 0,
        }
        self._startup_state: dict[str, GMStartupStateValue] = {
            "phase": "initialized",
            "history": [{"phase": "initialized", "ts": time.time()}],
        }

        logger.info(f"StrategyBridge 初始化: strategy_id={self._strategy_id}")

    def start(self, timeout: float = 30.0) -> bool:
        """
        启动策略框架（在后台线程中运行）.

        Parameters
        ----------
        timeout : float, default=30.0
            等待桥接器完成启动的超时时间，单位为秒。

        Returns
        -------
        bool
            启动成功时返回 ``True``，否则返回 ``False``。
        """
        if self._running:
            logger.warning("StrategyBridge 已经在运行中")
            return True

        self._stop_event.clear()
        self._ready_event.clear()
        self._runtime_stop_requested.clear()
        self._startup_error = None
        self._update_startup_state("thread_starting")

        # 在后台线程中运行策略框架
        self._thread = threading.Thread(
            target=self._run_strategy_loop,
            name="GMStrategyBridge",
            daemon=True,
        )
        self._thread.start()

        # 小步等待可同时观察 ready 与后台线程异常，避免线程早已退出却仍等满 30 秒。
        self._wait_for_startup(timeout)

        if self._ready_event.is_set() and self._startup_error is None:
            self._running = True
            logger.success("StrategyBridge 启动成功")
            return True

        if self._startup_error is not None:
            startup_error = _normalize_startup_error(self._startup_error)
            self.stop()
            raise startup_error from self._startup_error

        grace_timeout = self._compute_startup_grace_timeout(timeout)
        thread_alive = self._thread is not None and self._thread.is_alive()
        if thread_alive and grace_timeout > 0:
            logger.warning(
                f"StrategyBridge 启动在 {timeout} 秒内未就绪，但线程仍存活；继续等待 {grace_timeout} 秒宽限窗口"
            )
            self._wait_for_startup(grace_timeout)
            if self._startup_error is not None:
                startup_error = _normalize_startup_error(self._startup_error)
                self.stop()
                raise startup_error from self._startup_error
            if self._ready_event.is_set():
                self._running = True
                logger.success("StrategyBridge 在宽限窗口内启动成功")
                return True

        logger.error(f"StrategyBridge 启动超时 ({timeout}秒), {self._build_startup_timeout_summary()}")
        error = TimeoutError("GM 策略桥启动超时")
        error.technical_detail = self._build_startup_timeout_summary()  # type: ignore[attr-defined]
        report_gm_failure(
            self.connection_monitor,
            error,
            "连接超时",
            "GM 策略桥在启动及宽限等待结束后仍未就绪。",
            facts={"失败阶段": "策略桥启动等待", "启动等待秒数": timeout, "宽限等待秒数": grace_timeout},
            steps=("检查 GM 服务与目标账户登录情况。", "查看技术详情中的启动阶段记录。"),
        )
        self._startup_error = error
        self.stop()
        return False

    def _wait_for_startup(self, timeout: float) -> None:
        deadline = clock_monotonic() + max(timeout, 0.0)
        while not self._ready_event.is_set() and self._startup_error is None:
            remaining = deadline - clock_monotonic()
            if remaining <= 0:
                break
            get_default_clock().event_wait(self._ready_event, min(0.05, remaining))

    def _account_startup_failed(self, error: Exception) -> None:
        self._startup_error = error

    @staticmethod
    def _compute_startup_grace_timeout(timeout: float) -> float:
        """返回启动超时后的额外宽限时间."""
        if timeout <= 0:
            return 0.0
        return min(5.0, max(0.05, timeout * 0.5))

    @staticmethod
    def _compute_shutdown_grace_timeout(timeout: float) -> float:
        """返回停止等待后的额外宽限时间."""
        if timeout <= 0:
            return 0.0
        return min(2.0, max(0.05, timeout * 0.4))

    def stop(self) -> None:
        """停止策略框架."""
        if not self._running and self._thread is None:
            return

        logger.info("正在停止 StrategyBridge...")
        self._stop_event.set()
        self._running = False

        # 注意：不要调用 gm.api.stop()，因为它会执行 sys.exit(2) 导致整个进程退出
        # 我们在此捕获 SystemExit，将它仅作为 GM SDK 的退出信号，而不是让整个进程退出
        self._fail_pending_requests(RuntimeError("GM bridge 已停止"))

        self._request_gm_runtime_stop()

        # 等待线程结束
        if self._thread and self._thread.is_alive():
            join_timeout = 5.0
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                grace_timeout = self._compute_shutdown_grace_timeout(join_timeout)
                if grace_timeout > 0:
                    self._thread.join(timeout=grace_timeout)
                if self._thread.is_alive():
                    logger.warning(
                        f"StrategyBridge 线程未能在 {join_timeout + grace_timeout:.1f} 秒内结束; "
                        f"{self._build_thread_debug_summary(self._thread)}"
                    )

        self._thread = None
        logger.info("StrategyBridge 已停止")

    def _request_gm_runtime_stop(self) -> None:
        """请求 GM SDK 软停止实时 run() 主循环，避免触发 sys.exit(2)."""
        try:
            from gm.api import basic as gm_basic  # type: ignore
        except Exception as exc:
            logger.debug(f"无法导入 gm.api.basic，跳过 runtime stop 请求: {exc}")
            return

        try:
            with self._runtime_stop_lock:
                first_request = not self._runtime_stop_requested.is_set()
                self._runtime_stop_requested.set()

                unsubscribe_all = getattr(gm_basic, "_py_gmi_unsubscribe_all", None)
                if first_request and callable(unsubscribe_all):
                    unsubscribe_all()
            setattr(gm_basic, "running", False)
            logger.debug("已请求 GM runtime 软停止")
        except Exception as exc:
            logger.warning(f"请求 GM runtime 停止失败: {exc}")

    def _update_startup_state(self, phase: str, **details: GMStartupStateValue) -> None:
        """记录 bridge 启动阶段."""
        entry: GMStartupHistoryEntry = {"phase": phase, "ts": time.time()}
        if details:
            entry.update(details)
        history = self._startup_state.setdefault("history", [])
        if isinstance(history, list):
            history.append(entry)
        self._startup_state["phase"] = phase
        self._startup_state["last"] = entry

    def _build_startup_timeout_summary(self) -> str:
        """构建启动超时时的诊断摘要."""
        phase = self._startup_state.get("phase", "unknown")
        history = self._startup_state.get("history", [])
        history_tail = []
        if isinstance(history, list):
            history_tail = history[-5:]
        history_summary = " -> ".join(str(item.get("phase", "?")) for item in history_tail if isinstance(item, dict))
        thread_alive = self._thread is not None and self._thread.is_alive()
        return f"phase={phase}, thread_alive={thread_alive}, history={history_summary}"

    @staticmethod
    def _read_gm_runtime_running_flag() -> bool | None:
        """读取 GM SDK realtime run() 主循环的 running 标志."""
        try:
            from gm.api import basic as gm_basic  # type: ignore
        except Exception:
            return None
        running = getattr(gm_basic, "running", None)
        return running if isinstance(running, bool) else None

    def _build_thread_debug_summary(self, thread: threading.Thread) -> str:
        """构建 bridge 线程的调试摘要."""
        gm_running = self._read_gm_runtime_running_flag()
        stack_summary = self._format_thread_stack_summary(thread.ident)
        return f"thread_name={thread.name}, thread_ident={thread.ident}, gm_running={gm_running}, stack={stack_summary}"

    @staticmethod
    def _format_thread_stack_summary(thread_ident: int | None) -> str:
        """提取线程当前栈顶摘要，便于定位是否卡在 gmi_poll."""
        if thread_ident is None:
            return "unavailable(thread ident missing)"

        frame = sys._current_frames().get(thread_ident)
        if frame is None:
            return "unavailable(frame missing)"

        extracted = traceback.extract_stack(frame)
        if not extracted:
            return "unavailable(empty stack)"

        tail = extracted[-3:]
        return " <- ".join(f"{Path(item.filename).name}:{item.lineno}:{item.name}" for item in tail)

    def is_running(self) -> bool:
        """检查是否正在运行."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def is_ready(self) -> bool:
        """读取交易账户就绪门；不会调用 SDK 或启动恢复。"""
        return self.is_running() and self._ready_event.is_set() and self._startup_error is None

    def get_stats(self) -> dict[str, int]:
        """获取统计信息."""
        return self._stats.copy()

    def request_symbols(self, symbols: list[str], *, timeout: float = 10.0) -> list[str]:
        """请求 bridge 订阅新增标的."""
        requested_symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        if not requested_symbols:
            return []

        existing_symbols = set(self._subscribe_symbols)
        new_symbols = [symbol for symbol in requested_symbols if symbol not in existing_symbols]
        if not new_symbols:
            return []

        if self._running:
            self._submit_request(GMSubscribeSymbolsRequest(symbols=new_symbols), timeout=timeout)

        self._subscribe_symbols.extend(new_symbols)
        return new_symbols

    def call(self, request: GMSdkRequest, timeout: float | None = 30.0) -> GMBridgeResult:
        """在 bridge 的 run() 上下文里执行类型化 GM SDK 请求."""
        return self._submit_request(request, timeout=timeout)

    def _submit_request(self, request: GMBridgeRequestPayload, timeout: float | None = 30.0) -> GMBridgeResult:
        """在 bridge 的 run() 上下文里执行 GM 请求."""
        if not self._running:
            raise RuntimeError(f"GM bridge 未运行，无法执行操作: {request.operation}")

        bridge_request = GMBridgeRequest(
            request=request,
            future=Future(),
            deadline=None if timeout is None else time.monotonic() + timeout,
        )
        self._request_queue.put(bridge_request)

        try:
            return bridge_request.future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            # 超时后必须让请求失效：``Future.result(timeout=...)`` 超时只是让调用方
            # 放弃等待，Future 仍是 PENDING，消费端 (`_process_bridge_requests`) 的
            # `future.cancelled()` 守卫因此恒为 False——GM runtime 恢复后仍会照常
            # 执行这笔已被判定失败的请求，若为下单/撤单即可能重复下单或错误撤单。
            # PENDING 态 cancel() 必然成功，直接关掉「请求仍在队列里」这一主导窗口；
            # 已被取出、尚未 dispatch 的残留窗口由消费端的 deadline 二次校验兜底。
            bridge_request.future.cancel()
            raise TimeoutError(
                f"GM bridge 调用超时: operation={bridge_request.request.operation}, timeout={timeout}"
            ) from exc

    def _fail_pending_requests(self, error: Exception) -> None:
        """
        停止时回填所有未处理请求，避免 Future 悬挂.

        Parameters
        ----------
        error : Exception
            回填给未处理请求的异常。

        Notes
        -----
        用 ``set_running_or_notify_cancel()`` 而非 ``done()`` 判定：调用方超时会
        并发 ``cancel()`` 同一个 Future，先查 ``done()`` 再 ``set_exception()``
        存在 TOCTOU 竞态，可能抛出 ``InvalidStateError``。已被取消的请求直接跳过，
        调用方本就已按超时处理。
        """
        while True:
            try:
                request = self._request_queue.get_nowait()
            except queue.Empty:
                return

            if request.future.set_running_or_notify_cancel():
                request.future.set_exception(error)

    def _run_strategy_loop(self) -> None:
        """在后台线程中运行掘金策略框架."""
        import sys

        try:
            # 使用固定的策略文件
            strategy_dir = Path(__file__).parent
            strategy_file = strategy_dir / "gm_strategy.py"
            self._update_startup_state("thread_running", strategy_file=str(strategy_file))
            logger.debug(f"策略文件: {strategy_file}")

            if not strategy_file.exists():
                raise FileNotFoundError(f"策略文件不存在: {strategy_file}")

            # 切换到策略文件目录，使用相对路径
            original_cwd = os.getcwd()
            os.chdir(strategy_dir)

            # 确保策略目录在 sys.path 中
            if str(strategy_dir) not in sys.path:
                sys.path.insert(0, str(strategy_dir))

            try:
                # 使用相对文件名
                self._update_startup_state("executing_strategy")
                self._execute_strategy("gm_strategy.py")
            finally:
                os.chdir(original_cwd)

        except Exception as e:
            self._stats["errors"] += 1
            self._startup_error = e
            if not self._stop_event.is_set():
                report_runtime_failure(self.connection_monitor, e, authentication=isinstance(e, GMAuthenticationError))
            self._update_startup_state("thread_error", error=str(e))
            logger.opt(exception=e).error("策略框架运行出错: {}", e)
        finally:
            self._update_startup_state("thread_exiting")
            self._running = False
            if not self._stop_event.is_set() and self._startup_error is None:
                report_gm_failure(
                    self.connection_monitor,
                    RuntimeError("GM 策略运行时意外停止"),
                    "交易运行时停止",
                    "GM 策略线程在未主动停止时退出。",
                    facts={"退出对象": "策略运行线程", "主动停止": "否"},
                    steps=("检查 GM 交易服务运行情况。", "处理故障后重新启动账户运行时。"),
                )

    def _execute_strategy(self, strategy_file: str) -> None:
        """执行策略文件."""
        import sys

        install_gm_strategy_runtime_context(
            dispatcher=self._callback_dispatcher,
            ready_event=self._ready_event,
            stop_event=self._stop_event,
            stats=self._stats,
            subscribe_symbols=self._subscribe_symbols,
            request_queue=self._request_queue,
            startup_state=self._startup_state,
            runtime_stop_requested=self._runtime_stop_requested,
            runtime_stop_lock=self._runtime_stop_lock,
            account_id=self._account_id,
            connection_monitor=self.connection_monitor,
            startup_failure_handler=self._account_startup_failed,
        )

        # 保存原始的 sys.argv，并临时替换为最小参数
        # 掘金的 run() 函数会解析 sys.argv，但主进程的命令行参数（如 --ssl-keyfile）不被支持
        original_argv = sys.argv
        sys.argv = [strategy_file]

        try:
            import signal

            from gm.api import MODE_LIVE, basic, run, set_account_id, set_serv_addr, set_token  # type: ignore

            self._update_startup_state("gm_api_imported")
            set_token(self._token)
            if self._serv_addr:
                set_serv_addr(self._serv_addr)
            set_account_id(self._account_id)
            basic.running = True
            self._update_startup_state("sdk_configured", serv_addr=bool(self._serv_addr))

            # 禁用 signal（因为 signal 只能在主线程中使用）
            # 保存原始的 signal 函数
            original_signal = signal.signal

            def dummy_signal(signum: int, _handler: SignalHandler) -> None:
                """空操作的 signal 替代."""
                logger.debug(f"跳过 signal({signum}) 注册（非主线程）")
                return None

            # 临时替换 signal.signal
            signal.signal = dummy_signal  # type: ignore[assignment]

            try:
                # 运行策略（阻塞）
                self._update_startup_state("gm_run_invoked")
                run_kwargs = {
                    "strategy_id": self._strategy_id,
                    "filename": strategy_file,
                    "mode": MODE_LIVE,
                    "token": self._token,
                }
                if self._serv_addr:
                    run_kwargs["serv_addr"] = self._serv_addr
                run(**run_kwargs)
                self._update_startup_state("gm_run_returned")
            finally:
                # 恢复原始的 signal 函数
                signal.signal = original_signal
        finally:
            # 恢复原始的 sys.argv
            sys.argv = original_argv

            clear_gm_strategy_runtime_context()
