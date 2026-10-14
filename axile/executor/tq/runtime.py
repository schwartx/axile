"""在单一 owner thread 中驱动 TqApi 与 ``wait_update``."""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, TypeVar, cast

from axile.executor.algorithms.utils.clock import clock_monotonic, get_default_clock
from axile.executor.connection_state import ConnectionMonitor
from axile.executor.instrument_catalog_records import EXCHANGES, Instrument, normalize_catalog_symbol
from axile.executor.tq.connection import report_tq_failure
from axile.executor.tq.symbols import TQInstrument, TQSymbolResolver

T = TypeVar("T")


@dataclass(slots=True)
class _Command:
    operation: Callable[[object], object]
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None
    status: Literal["pending", "running", "finished", "cancelled"] = "pending"
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> bool:
        """仅让尚未取消的命令进入执行态。"""
        with self.lock:
            if self.status != "pending":
                return False
            self.status = "running"
            return True

    def cancel(self) -> bool:
        """仅取消尚未开始的命令。"""
        with self.lock:
            if self.status != "pending":
                return False
            self.status = "cancelled"
            self.done.set()
            return True

    def finish(self, *, result: object = None, error: BaseException | None = None) -> None:
        """发布已经开始执行的命令结果。"""
        with self.lock:
            if self.status != "running":
                return
            self.result = result
            self.error = error
            self.status = "finished"
            self.done.set()

    def fail_pending(self, error: BaseException) -> None:
        """运行时停止时唤醒仍在排队的调用方。"""
        with self.lock:
            if self.status != "pending":
                return
            self.error = error
            self.status = "finished"
            self.done.set()


def snapshot_entity(entity: object) -> dict[str, object]:
    """复制 TqSdk Entity 的公开字段，避免把可变 SDK 对象带出 owner thread."""
    if isinstance(entity, Mapping):
        return {str(key): value for key, value in entity.items() if not str(key).startswith("_")}
    result: dict[str, object] = {}
    for name in dir(entity):
        if name.startswith("_"):
            continue
        try:
            value = getattr(entity, name)
        except Exception:  # noqa: BLE001 - SDK Entity 属性可能动态求值
            continue
        if not callable(value):
            result[name] = value
    return result


def _quote_value_snapshot(value: object) -> object:
    """递归展开行情中的 SDK Mapping（如 TradingTime），不复制其私有 API 引用。"""
    if isinstance(value, Mapping):
        return {str(key): _quote_value_snapshot(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, list):
        return [_quote_value_snapshot(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_quote_value_snapshot(item) for item in value)
    return deepcopy(value)


@dataclass(slots=True)
class _QuoteSubscription:
    started: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    entity: object | None = None
    snapshot: dict[str, object] | None = None
    error: Exception | None = None
    warned: bool = False


class TQRuntime:
    """TqApi 单线程运行时与同步命令桥."""

    def __init__(
        self,
        api_factory: Callable[[], object],
        *,
        command_timeout: float = 30.0,
        connection: ConnectionMonitor | None = None,
    ) -> None:
        self._connection = connection or ConnectionMonitor()
        self._api_factory = api_factory
        self._command_timeout = command_timeout
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: BaseException | None = None
        self._runtime_error: BaseException | None = None
        self._close_error: BaseException | None = None
        self._api: object | None = None
        self._resolver: TQSymbolResolver | None = None
        # condition 保护订阅登记、快照与等待状态；SDK entity / task 仅由 owner 使用。
        self._subscriptions: dict[str, _QuoteSubscription] = {}
        self._quote_condition = threading.Condition()
        self._logger = logging.getLogger(__name__)
        self._listeners: list[Callable[[str, dict[str, object]], None]] = []
        self._orders: dict[str, dict[str, object]] = {}
        self._trades: dict[str, dict[str, object]] = {}
        self._thread = threading.Thread(target=self._run, name="axile-tqsdk-runtime", daemon=True)
        self._thread.start()
        if not self._ready.wait(command_timeout):
            error = TimeoutError("TqSdk 运行时初始化超时")
            report_tq_failure(self._connection, error, "天勤连接超时", "运行时初始化等待")
            self._connection.close()
            raise error
        if self._startup_error is not None:
            raise RuntimeError(f"TqSdk 运行时初始化失败: {self._startup_error}") from self._startup_error

    @property
    def resolver(self) -> TQSymbolResolver:
        """返回当前执行器的按需 symbol 解析器副本。"""
        if self._resolver is None:
            raise RuntimeError("TqSdk symbol 索引尚未就绪")
        return self._resolver

    def add_listener(self, listener: Callable[[str, dict[str, object]], None]) -> None:
        """注册运行时快照监听器."""
        self._listeners.append(listener)

    def is_alive(self) -> bool:
        """返回 owner thread 是否仍在运行."""
        return self._thread.is_alive() and not self._stopped.is_set()

    def call(self, operation: Callable[[object], T], *, timeout: float | None = None) -> T:
        """在 owner thread 执行同步操作并返回复制后的结果."""
        if self._stopped.is_set():
            if self._runtime_error is not None:
                raise RuntimeError(f"TqSdk 运行时异常停止: {self._runtime_error}") from self._runtime_error
            raise RuntimeError("TqSdk 运行时已关闭")
        command = _Command(cast("Callable[[object], object]", operation))
        self._commands.put(command)
        if self._stopped.is_set():
            command.fail_pending(self._stopped_error())
        wait_timeout = self._command_timeout if timeout is None else timeout
        if not command.done.wait(wait_timeout):
            if command.cancel():
                raise TimeoutError("TqSdk 命令排队超时")
            command.done.wait()
        if command.error is not None:
            raise RuntimeError(f"TqSdk 命令失败: {command.error}") from command.error
        return cast(T, command.result)

    def _stopped_error(self) -> RuntimeError:
        if self._runtime_error is not None:
            return RuntimeError(f"TqSdk 运行时异常停止: {self._runtime_error}")
        return RuntimeError("TqSdk 运行时已经停止")

    def subscribe(self, symbols: list[str]) -> None:
        """登记共享订阅；SDK 协程由 owner 的事件泵驱动，不等待首笔行情。"""
        with self._quote_condition:
            if self._stopped.is_set():
                raise self._stopped_error()
            pending: list[tuple[str, _QuoteSubscription]] = []
            for symbol in dict.fromkeys(symbols):
                if symbol in self._subscriptions:
                    continue
                state = _QuoteSubscription()
                self._subscriptions[symbol] = state
                pending.append((symbol, state))
            if pending:
                self._commands.put(_Command(lambda api: self._start_subscriptions(api, pending)))

    def _start_subscriptions(self, api: object, pending: list[tuple[str, _QuoteSubscription]]) -> None:
        for symbol, state in pending:
            self._start_subscription(api, symbol, state)

    def _start_subscription(self, api: object, symbol: str, state: _QuoteSubscription) -> None:
        coroutine = self._subscribe_quote(api, symbol, state)
        try:
            state.task = getattr(api, "create_task")(coroutine)
        except Exception as exc:
            coroutine.close()
            self._subscription_failed(symbol, state, exc)

    async def _subscribe_quote(self, api: object, symbol: str, state: _QuoteSubscription) -> None:
        try:
            # 同步 get_quote 有约 30 秒 SDK 超时；在 SDK 协程中 await 才不会独占 owner。
            state.entity = await getattr(api, "get_quote")(symbol)
            self._publish_quote(state)
        except Exception as exc:
            self._subscription_failed(symbol, state, exc)
            return
        if state.warned:
            self._logger.info("TQ 行情订阅恢复 %s，耗时 %.3fs", symbol, time.monotonic() - state.started)

    def _subscription_failed(self, symbol: str, state: _QuoteSubscription, error: Exception) -> None:
        with self._quote_condition:
            state.error = error
            self._quote_condition.notify_all()
        self._logger.warning("TQ 行情订阅失败 %s，耗时 %.3fs: %s", symbol, time.monotonic() - state.started, error)

    def _publish_quote(self, state: _QuoteSubscription) -> None:
        snapshot = cast(dict[str, object], _quote_value_snapshot(snapshot_entity(state.entity)))
        with self._quote_condition:
            state.snapshot = snapshot
            self._quote_condition.notify_all()
        self._emit("quote", deepcopy(snapshot))

    def quote_snapshots(self, symbols: list[str], *, timeout: float = 5.0) -> dict[str, dict[str, object]]:
        """在调用线程按单次总 deadline 等待首份快照，返回已就绪合约的独立副本。

        超时不取消共享订阅；失败合约记录诊断并省略，交由规划层处理缺失行情。
        已缓存快照保留 SDK 原始时间戳，不等待下一笔更新。
        """
        deadline = time.monotonic() + timeout
        self.subscribe(symbols)
        with self._quote_condition:
            states = {symbol: self._subscriptions[symbol] for symbol in dict.fromkeys(symbols)}
            while True:
                if self._stopped.is_set():
                    raise self._stopped_error()
                pending = {s: state for s, state in states.items() if state.snapshot is None and state.error is None}
                remaining = deadline - time.monotonic()
                if not pending or remaining <= 0:
                    break
                if threading.current_thread() is self._thread:
                    raise RuntimeError("TQ owner 线程不能等待首笔行情")
                self._quote_condition.wait(remaining)
            for symbol, state in pending.items():
                if not state.warned:
                    state.warned = True
                    self._logger.warning("TQ 首次行情订阅超时 %s，耗时 %.3fs", symbol, time.monotonic() - state.started)
            return {symbol: deepcopy(state.snapshot) for symbol, state in states.items() if state.snapshot is not None}

    def quote_snapshot(self, symbol: str, *, timeout: float = 5.0) -> dict[str, object]:
        """读取单合约元数据；失败或到期时明确报错，禁止以默认规格继续交易。"""
        rows = self.quote_snapshots([symbol], timeout=timeout)
        if symbol in rows:
            return rows[symbol]
        with self._quote_condition:
            error = self._subscriptions[symbol].error
        if error is not None:
            raise RuntimeError(f"TQ 行情订阅失败 {symbol}: {error}") from error
        raise TimeoutError(f"TQ 首次行情订阅超时: {symbol}")

    def close(self) -> None:
        """停止事件泵并关闭 TqApi."""
        if self._stopped.is_set():
            return
        self._commands.put(None)
        self._thread.join(timeout=self._command_timeout)
        if self._thread.is_alive():
            raise TimeoutError("TqSdk 运行时关闭超时")
        if self._close_error is not None:
            raise RuntimeError(f"TqSdk 运行时关闭失败: {self._close_error}") from self._close_error

    def _build_catalog(self, api: object) -> TQSymbolResolver:
        return TQSymbolResolver([], loader=self._load_symbol)

    def _load_symbol(self, symbol: str) -> list[TQInstrument]:
        if "." in symbol and symbol.split(".", 1)[0] not in EXCHANGES:
            exchange, local = symbol.split(".", 1)
            return [TQInstrument(symbol, local, exchange, "QUOTE_ONLY")]
        rows = self.query_catalog([symbol], 5.0)
        return [TQInstrument(r.key, r.symbol, r.exchange, r.kind) for r in rows]

    def install_catalog(self, rows: list[Instrument], loader: Callable[[str], list[TQInstrument]]) -> None:
        """原子替换执行器的解析器副本，不读取实时行情规格。"""
        self._resolver = TQSymbolResolver(
            [TQInstrument(r.key, r.symbol, r.exchange, r.kind) for r in rows], loader=loader
        )

    def query_catalog(self, symbols: list[str] | None, timeout: float | None) -> list[Instrument]:
        """在 owner 协程内查询基础资料，调用线程只等待有界结果。"""
        timeout = self._command_timeout if timeout is None else timeout
        done = threading.Event()
        result: list[Instrument] = []
        errors: list[Exception] = []
        deadline = clock_monotonic() + timeout

        async def collect(api: object) -> None:
            try:
                result.extend(await self._query_catalog_async(api, symbols, deadline))
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        task = self.call(lambda api: getattr(api, "create_task")(collect(api)), timeout=timeout)
        if not get_default_clock().event_wait(done, max(0.0, deadline - clock_monotonic())):
            self._commands.put(_Command(lambda api: getattr(task, "cancel")()))
            raise TimeoutError("TQ 合约基础资料查询超时")
        if errors:
            raise errors[0]
        return result

    @staticmethod
    async def _sdk_result(value):
        if hasattr(value, "__await__"):
            return await value
        return value

    async def _query_catalog_async(self, api: object, symbols: list[str] | None, deadline: float) -> list[Instrument]:
        requested = {normalize_catalog_symbol(symbol) for symbol in symbols} if symbols is not None else None
        products: set[str] = set()
        if requested is not None:
            for symbol in requested:
                match = re.search(r"[A-Za-z_]+(?=\d)", symbol.split(".", 1)[-1])
                if match is None:
                    raise ValueError(f"无法解析天勤合约代码: {symbol}")
                products.add(match.group())
        records: dict[str, Instrument] = {}
        for kind in ("FUTURE", "OPTION", "COMBINE"):
            if clock_monotonic() >= deadline:
                raise TimeoutError("TQ 合约代码目录查询超时")
            filters = {"product_id": sorted(products)} if requested is not None and kind != "OPTION" else {}
            found = await self._sdk_result(getattr(api, "query_quotes")(ins_class=kind, expired=False, **filters))
            for raw in found:
                exchange, _, symbol = str(raw).partition(".")
                if exchange not in EXCHANGES or not symbol:
                    continue
                record = Instrument(exchange, symbol, kind)
                if requested is None or requested.intersection((record.key, record.symbol)):
                    records[record.key] = record
        return list(records.values())

    def _emit(self, kind: str, payload: dict[str, object]) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(kind, dict(payload))
            except Exception:
                continue

    def _pump_changes(self, api: object) -> None:
        is_changing = getattr(api, "is_changing", lambda _entity: False)
        with self._quote_condition:
            subscriptions = tuple(self._subscriptions.values())
        for state in subscriptions:
            if state.entity is not None and is_changing(state.entity):
                self._publish_quote(state)
        for kind, getter_name, previous in (
            ("order", "get_order", self._orders),
            ("trade", "get_trade", self._trades),
        ):
            getter = getattr(api, getter_name, None)
            if not callable(getter):
                continue
            rows = getter()
            if not isinstance(rows, Mapping):
                continue
            current = {str(key): snapshot_entity(value) for key, value in rows.items() if not str(key).startswith("_")}
            for key, value in current.items():
                if previous.get(key) != value:
                    self._emit(kind, value)
            previous.clear()
            previous.update(current)

    def _run(self) -> None:
        try:
            try:
                api = self._api_factory()
            except BaseException as exc:
                report_tq_failure(self._connection, exc, "天勤连接失败", "API 工厂初始化")
                raise
            self._connection.succeeded()
            self._api = api
            self._resolver = self._build_catalog(api)
        except BaseException as exc:  # noqa: BLE001 - 必须把 owner thread 初始化错误交给调用方
            self._startup_error = exc
            if self._api is not None:
                try:
                    getattr(self._api, "close")()
                except Exception:
                    pass
                self._api = None
            self._ready.set()
            self._stopped.set()
            return
        self._ready.set()
        try:
            while True:
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    command = ...
                if command is None:
                    break
                if isinstance(command, _Command):
                    if command.start():
                        try:
                            result = command.operation(api)
                        except BaseException as exc:  # noqa: BLE001 - 跨线程传播 SDK 错误
                            command.finish(error=exc)
                        else:
                            command.finish(result=result)
                try:
                    wait_update = getattr(api, "wait_update")
                    wait_update(deadline=time.time() + 0.05)
                    self._pump_changes(api)
                except BaseException:  # noqa: BLE001 - 终止失效事件泵并让后续调用看到异常
                    raise
        except BaseException as exc:  # noqa: BLE001 - 保存事件泵异常供同步调用方读取
            self._runtime_error = exc
            report_tq_failure(self._connection, exc, "天勤运行时异常", "事件泵运行")
        finally:
            self._shutdown(api)

    def _shutdown(self, api: object) -> None:
        """在 owner 内取消订阅并唤醒读价和命令等待方，再释放 SDK。"""
        with self._quote_condition:
            self._stopped.set()
            for state in self._subscriptions.values():
                if state.task is not None and not state.task.done():
                    state.task.cancel()
            self._quote_condition.notify_all()
        failure = self._stopped_error()
        while True:
            try:
                pending = self._commands.get_nowait()
            except queue.Empty:
                break
            if isinstance(pending, _Command):
                pending.fail_pending(failure)
        try:
            getattr(api, "close")()
        except BaseException as exc:  # noqa: BLE001 - close 错误必须跨线程传播
            self._close_error = exc
        finally:
            self._api = None


__all__ = ["TQRuntime", "snapshot_entity"]
