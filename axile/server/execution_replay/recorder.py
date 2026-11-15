"""账户唯一的录制旁路；采集线程不执行原生写入或排空。"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_us
from axile.executor.replay import set_replay_observer
from axile.server.execution_replay.contracts import encode_json, freeze_data, freeze_json
from axile.server.execution_replay.service import native_writer

MAX_IDENTITIES = 4_096
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 512 * 1024
TAIL_US = 5_000_000


@dataclass
class _Session:
    recording_id: str
    context: dict[str, object]
    queue: deque[tuple[str, Any]] = field(default_factory=deque)
    sequence: int = 0
    last_time_us: int | None = None
    stopping: bool = False
    error: str | None = None
    state: str = "starting"
    thread: threading.Thread | None = None
    request_revision: int = 0
    queries: dict[int, tuple[int, tuple[str, tuple[str, ...]], int]] = field(default_factory=dict)
    completed_queries: dict[tuple[str, tuple[str, ...]], tuple[int, int, int]] = field(default_factory=dict)
    seed_interest_scopes: dict[str, set[str] | None] = field(
        default_factory=lambda: {"orders": set(), "positions": set()}
    )
    next_poll: float = 0.0
    poll_pending: bool = False
    waiting_for_writer: bool = False
    queue_bytes: int = 0
    capture_streams: set[str] = field(default_factory=set)


class ReplayRecorder:
    """管理单账户录制意愿、连接绑定及唯一后台 writer。

    Parameters
    ----------
    account_id : int
        已确认的账户身份。
    root : str | Path
        服务端确定的录制根目录，不从请求取得。
    context : Mapping | None
        冻结的渠道能力、扩展字段声明和品种语义。
    status_callback : Callable | None
        状态变化的只读通知；通知异常不影响交易。
    """

    def __init__(
        self,
        account_id: int,
        root: str | Path,
        context: Mapping[str, object] | None = None,
        status_callback: Callable[[dict[str, object]], None] | None = None,
        *,
        writer_factory: Callable[..., Any] | None = None,
        now_us: Callable[[], int] = clock_now_us,
    ) -> None:
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise ValueError("invalid_replay_account")
        self.account_id = account_id
        self.root = str(root)
        self._context = self._freeze_context(context or {})
        self._status_callback = status_callback
        self._writer_factory = writer_factory or native_writer
        self._now_us = now_us
        self._condition = threading.Condition(threading.RLock())
        self._owner: object | None = None
        self._supported = True
        self._sync_generation = 0
        self._desired = False
        self._config_version = -1
        self._effective_version = -1
        self._error: str | None = None
        self._session: _Session | None = None
        self._closed = False
        self._executions: dict[str, set[str]] = {}
        self._interest_orders: set[str] = set()
        self._interest_positions: set[str] = set()
        self._tails: dict[str, int] = {}

    @staticmethod
    def _freeze_context(context: Mapping[str, object]) -> dict[str, object]:
        frozen = freeze_json(context)
        if not isinstance(frozen, dict):
            raise ValueError("invalid_replay_context")
        frozen["schema_version"] = 2
        return frozen

    def status(self) -> dict[str, object]:
        """返回当前意愿与实际状态，不在调用线程读取原生 writer。"""
        with self._condition:
            return self._status_locked()

    def _status_locked(self) -> dict[str, object]:
        session = self._session
        if self._error:
            state = "error"
        elif session is not None and session.stopping:
            state = "closing"
        elif not self._desired:
            state = "disabled"
        elif not self._supported:
            state = "unsupported"
        elif self._owner is None:
            state = "waiting_connection"
        else:
            state = session.state if session else "starting"
        result: dict[str, object] = {
            "desired": self._desired,
            "state": state,
            "config_version": self._config_version,
            "effective_version": self._effective_version,
        }
        if state == "starting":
            result["effective_version"] = None
        if state == "starting" and session is not None and session.waiting_for_writer:
            result["reason"] = "等待上一段录制结束"
        if session:
            result["recording_id"] = session.recording_id
        if self._error:
            result["error"] = self._error
        return result

    def _notify(self) -> None:
        if self._status_callback is not None:
            try:
                self._status_callback(self.status())
            except Exception:  # noqa: BLE001 - 运行状态回传不影响录制或交易
                pass

    def configure(self, enabled: bool, config_version: int) -> dict[str, object]:
        """请求启停并立即返回，旧版本忽略；不等待 writer 创建或封口。"""
        if type(enabled) is not bool or type(config_version) is not int or config_version < 0:
            raise ValueError("invalid_replay_configuration")
        with self._condition:
            if self._closed or config_version < self._config_version:
                return self._status_locked()
            if config_version == self._config_version:
                return self._status_locked()
            was_enabled = self._desired
            self._desired = enabled
            self._config_version = config_version
            if not enabled:
                self._unbind_hook_locked()
                self._stop_locked()
                if self._session is None:
                    self._effective_version = config_version
            else:
                if not was_enabled:
                    self._error = None
                self._start_locked()
                if self._session is None or not self._session.stopping:
                    self._effective_version = config_version
            result = self._status_locked()
        self._notify()
        return result

    def attach(
        self, executor: object | None, context: Mapping[str, object] | None = None, *, supported: bool = True
    ) -> dict[str, object]:
        """绑定已有连接；换连接或解释上下文时关闭旧段，不查询或重连。"""
        frozen = self._freeze_context(context) if context is not None else None
        with self._condition:
            changed = (
                executor is not self._owner
                or frozen is not None
                and frozen != self._context
                or supported != self._supported
            )
            if changed:
                self._unbind_hook_locked()
                previous = self._owner
                if previous is not None and getattr(previous, "_replay_recorder", None) is self:
                    for name in ("_replay_recorder", "_replay_account_id", "_replay_sync_generation"):
                        if hasattr(previous, name):
                            delattr(previous, name)
                self._stop_locked()
                self._owner = executor
                self._sync_generation += 1
                self._interest_orders.clear()
                self._interest_positions.clear()
                self._tails.clear()
                if frozen is not None:
                    self._context = frozen
            self._supported = bool(supported)
            if executor is not None:
                setattr(executor, "_replay_recorder", self)
                setattr(executor, "_replay_account_id", self.account_id)
                setattr(executor, "_replay_sync_generation", self._sync_generation)
            self._start_locked()
            result = self._status_locked()
        self._notify()
        return result

    def _unbind_hook_locked(self) -> None:
        if self._owner is not None and getattr(self._owner, "_replay_recorder", None) is self:
            set_replay_observer(self._owner, None)

    def _start_locked(self) -> None:
        if self._closed or not self._desired or not self._supported or self._owner is None or self._error:
            return
        if self._session is not None:
            return
        session = _Session(uuid4().hex, self._freeze_context(self._context))
        self._session = session
        self._interest_orders.clear()
        self._interest_positions.clear()

        def observe_session(topic: str, data: Mapping[str, object]) -> None:
            with self._condition:
                if self._session is session and not session.stopping:
                    self.observe(topic, data)

        set_replay_observer(self._owner, observe_session)
        for execution_id, symbols in self._executions.items():
            self.observe(
                "/execution",
                {
                    "execution_id": execution_id,
                    "phase": "carry_in",
                    "symbols": sorted(symbols),
                    "source": "worker_cache",
                },
            )
        session.thread = threading.Thread(
            target=self._write, args=(session,), name=f"replay-{self.account_id}", daemon=True
        )
        try:
            session.thread.start()
        except Exception:  # noqa: BLE001 - 创建旁路线程失败不能影响账户执行
            self._session = None
            self._error = "replay_writer_start_error"
            self._unbind_hook_locked()

    def _stop_locked(self, error: str | None = None) -> None:
        session = self._session
        if error:
            self._error = error
            self._unbind_hook_locked()
        if session is not None:
            session.stopping = True
            session.error = error or session.error
            self._condition.notify_all()

    def observe(self, topic: str, data: Mapping[str, object]) -> None:
        """冻结、取时和编号；编码与写入在后台执行。"""
        with self._condition:
            session = self._session
            if session is None or session.stopping or not self._desired:
                return
            try:
                timestamp = self._now_us()
                if type(timestamp) is not int or not 0 <= timestamp <= 2**53 - 1:
                    raise ValueError("replay_invalid_clock")
                if session.last_time_us is not None and timestamp < session.last_time_us:
                    raise ValueError("replay_clock_rollback")
                if isinstance(data, Mapping) and not self._relevant_locked(topic, data, timestamp):
                    return
                frozen = freeze_data(topic, data, session.context)
                self._normalize_generation_locked(topic, frozen, session)
                self._capture_interest_locked(frozen)
                if topic.startswith(("/quote/", "/market_trade/")) and topic not in session.capture_streams:
                    if len(session.capture_streams) >= 126:
                        raise ValueError("replay_stream_budget")
                    self._enqueue_locked(
                        session,
                        timestamp,
                        "/capture",
                        {
                            "symbol": topic.rsplit("/", 1)[-1],
                            "streams": [topic],
                            "boundary": "start",
                        },
                    )
                    session.capture_streams.add(topic)
                self._enqueue_locked(session, timestamp, topic, frozen)
                session.last_time_us = timestamp
                self._condition.notify_all()
            except Exception as error:  # noqa: BLE001 - 采集旁路不得改变业务结果
                code = (
                    str(error)
                    if isinstance(error, ValueError) and str(error).startswith("replay_")
                    else "replay_capture_error"
                )
                self._stop_locked(code)

    def _enqueue_locked(self, session: _Session, timestamp: int, topic: str, data: dict[str, object]) -> None:
        text = encode_json(
            {
                "schema_version": 2,
                "recording_id": session.recording_id,
                "capture_seq": session.sequence + 1,
                "time_us": timestamp,
                "topic": topic,
                "data": data,
            }
        )
        size = len(text.encode("utf-8"))
        if size > MAX_EVENT_BYTES or session.queue_bytes + size > MAX_QUEUE_BYTES:
            raise ValueError("replay_capture_budget")
        session.queue.append(("message", text))
        session.queue_bytes += size
        session.sequence += 1

    def _capture_interest_locked(self, data: dict[str, object]) -> None:
        # Keep directly observed interest until recording ends; no trade merge or owner inference.
        direct = data.get("symbols")
        symbols = {
            value
            for value in [data.get("symbol"), *(direct if isinstance(direct, list) else [])]
            if isinstance(value, str) and value
        }
        for name in ("orders", "positions"):
            rows = data.get(name, [])
            if isinstance(rows, list):
                symbols.update(
                    row["symbol"] for row in rows if isinstance(row, dict) and isinstance(row.get("symbol"), str)
                )
        if len(self._interest_positions | symbols) > MAX_IDENTITIES:
            raise ValueError("replay_interest_budget")
        self._interest_positions.update(symbols)

    def begin_query(self, scope: list[str]) -> dict[str, object] | None:
        """短锁分配查询覆盖 token；发起次序与消息序号分别维护。"""
        with self._condition:
            session = self._session
            if session is None or session.stopping or not self._desired:
                return None
            if not isinstance(scope, list) or not all(
                isinstance(symbol, str) and 0 < len(symbol) <= 128 for symbol in scope
            ):
                return None
            session.request_revision += 1
            while len(session.queries) + len(session.completed_queries) >= MAX_IDENTITIES and session.completed_queries:
                del session.completed_queries[next(iter(session.completed_queries))]
            if len(session.queries) >= MAX_IDENTITIES:
                self._stop_locked("replay_query_budget")
                return None
            key = ("symbols", tuple(sorted(set(scope)))) if scope else ("account", ())
            session.queries[session.request_revision] = self._sync_generation, key, session.sequence
            return {
                "sync_generation": self._sync_generation,
                "query_basis": {
                    "recording_id": session.recording_id,
                    "start_seq": session.sequence,
                    "request_revision": session.request_revision,
                },
            }

    def finish_query(self, token: Mapping[str, object] | None) -> None:
        """释放已退出查询的未完成证明，不撤销已冻结结果的重复观察。"""
        if not isinstance(token, Mapping):
            return
        with self._condition:
            session = self._session
            basis = token.get("query_basis")
            if session is None or not isinstance(basis, Mapping):
                return
            revision, start = basis.get("request_revision"), basis.get("start_seq")
            generation = token.get("sync_generation")
            issued = session.queries.get(revision) if type(revision) is int else None
            if (
                basis.get("recording_id") == session.recording_id
                and type(generation) is int
                and generation == self._sync_generation
                and type(start) is int
                and issued is not None
                and issued[0] == generation
                and issued[2] == start
            ):
                del session.queries[revision]

    def _normalize_generation_locked(self, topic: str, data: dict[str, object], session: _Session) -> None:
        is_quote = topic.startswith("/quote/")
        if topic not in {"/order", "/order_set", "/connection"} and not is_quote:
            return
        if topic == "/connection":
            invalidated = any(data.get(key) is False for key in ("synchronized", "ready", "connected", "available"))
            if invalidated:
                self._sync_generation += 1
                session.queries.clear()
                session.completed_queries.clear()
                if self._owner is not None:
                    setattr(self._owner, "_replay_sync_generation", self._sync_generation)
                data["sync_generation"] = self._sync_generation
        generation = data.setdefault("sync_generation", self._sync_generation)
        if (topic == "/order" or is_quote) and (type(generation) is not int or generation != self._sync_generation):
            data["available" if is_quote else "confirmed_book"] = False
        if generation is not None and (
            type(generation) is not int or generation < 0 or generation > self._sync_generation
        ):
            details = data.get("details", {})
            data["details"] = {
                **(details if isinstance(details, dict) else {"source_details": details}),
                "observed_sync_generation": generation,
            }
            data["sync_generation"] = None
        if is_quote:
            return
        data.setdefault(
            "scope",
            {"kind": "symbols", "symbols": [data["symbol"]]}
            if topic == "/order" and isinstance(data.get("symbol"), str)
            else {"kind": "account", "symbols": []},
        )
        self._normalize_query_locked(topic, data, session)

    def _normalize_query_locked(self, topic: str, data: dict[str, object], session: _Session) -> None:
        if topic == "/order_set":
            self._validate_query_locked(data, session)
        elif topic == "/order" and "query_basis" in data:
            self._validate_query_locked(data, session, save_completed=False)
            if data["query_basis"] is None:
                data["confirmed_book"] = False

    @staticmethod
    def _scope_key(scope: object) -> tuple[str, tuple[str, ...]] | None:
        if isinstance(scope, list) and all(isinstance(symbol, str) for symbol in scope):
            return "symbols", tuple(sorted(set(scope)))
        if not isinstance(scope, dict):
            return None
        if scope.get("kind") == "account":
            return "account", ()
        symbols = scope.get("symbols")
        if (
            scope.get("kind") == "symbols"
            and isinstance(symbols, list)
            and all(isinstance(symbol, str) for symbol in symbols)
        ):
            return "symbols", tuple(sorted(set(symbols)))
        return None

    def _validate_query_locked(
        self, data: dict[str, object], session: _Session, *, save_completed: bool = True
    ) -> None:
        basis = data.get("query_basis")
        scope = self._scope_key(data.get("scope"))
        if not isinstance(basis, dict) or scope is None:
            data["query_basis"] = None
            return
        revision = basis.get("request_revision")
        start = basis.get("start_seq")
        issued = session.queries.get(revision) if type(revision) is int else None
        completed = session.completed_queries.get(scope)
        if (
            basis.get("recording_id") != session.recording_id
            or type(data.get("sync_generation")) is not int
            or data.get("sync_generation") != self._sync_generation
            or type(start) is not int
            or type(revision) is not int
            or issued != (self._sync_generation, scope, start)
            and completed != (revision, self._sync_generation, start)
        ):
            data["query_basis"] = None
            return
        if not save_completed:
            return
        session.queries.pop(revision, None)
        if completed is None or revision > completed[0]:
            session.completed_queries.pop(scope, None)
            session.completed_queries[scope] = (revision, self._sync_generation, start)

    def _relevant_locked(self, topic: str, data: Mapping[str, object], timestamp: int) -> bool:
        if not topic.startswith(("/quote/", "/market_trade/")):
            return True
        symbol = str(data.get("symbol") or topic.rsplit("/", 1)[-1])
        return (
            symbol in self._interest_orders
            or symbol in self._interest_positions
            or any(symbol in symbols for symbols in self._executions.values())
            or timestamp < self._tails.get(symbol, 0)
        )

    def begin_execution(
        self, execution_id: str, symbols: Iterable[str], data: Mapping[str, object] | None = None
    ) -> None:
        """登记执行相关品种并记录开始，不发起行情订阅。"""
        execution_symbols = set(symbols)
        with self._condition:
            if execution_id not in self._executions and len(self._executions) >= MAX_IDENTITIES:
                self._stop_locked("replay_interest_budget")
                return
            self._executions[execution_id] = execution_symbols
        self.observe(
            "/execution",
            {**(data or {}), "execution_id": execution_id, "phase": "start", "symbols": sorted(execution_symbols)},
        )

    def end_execution(self, execution_id: str, data: Mapping[str, object] | None = None) -> None:
        """记录结束并保留已有订阅的五秒尾部；再迟成交仍接受。"""
        self.observe("/execution", {**(data or {}), "execution_id": execution_id, "phase": "end"})
        with self._condition:
            timestamp = self._now_us()
            for symbol in self._executions.pop(execution_id, set()):
                self._tails[symbol] = timestamp + TAIL_US
            self._seal_locked()

    def _seal_locked(self) -> None:
        session = self._session
        if session is not None and not session.stopping and (not session.queue or session.queue[-1][0] != "seal"):
            session.queue.append(("seal", ""))
            self._condition.notify_all()

    def tick(self) -> None:
        """唤醒有界轮转和安静尾部封口，不在调用线程运行原生操作。"""
        with self._condition:
            if self._session is not None:
                self._condition.notify_all()

    def _write(self, session: _Session) -> None:
        writer = None
        try:
            self._seed_context_metadata(session)
            writer = self._open_writer(session)
            if writer is None:
                return
            self._seed_interest(session)
            self._seed_cached_observations(session)
            while True:
                with self._condition:
                    if not session.queue and session.stopping:
                        break
                    self._queue_poll_locked(session)
                    if not session.queue:
                        self._condition.wait(0.1)
                        continue
                    item = session.queue.popleft()
                    if item[0] == "message":
                        session.queue_bytes -= len(item[1].encode("utf-8"))
                    if item[0].startswith("poll"):
                        session.poll_pending = False
                if item[0] == "message":
                    writer.append(item[1])
                elif item[0] == "seal":
                    writer.seal()
                else:
                    writer.poll(int(item[1]))
                    if item[0] == "poll_seal":
                        writer.seal()
            writer.finish()
        except Exception as error:  # noqa: BLE001 - writer 故障隔离且保留未封口文件
            with self._condition:
                code = "replay_close_timeout" if isinstance(error, TimeoutError) else "replay_writer_error"
                if isinstance(error, ValueError) and str(error).startswith("replay_"):
                    code = str(error)
                elif error.args and error.args[0] == "REPLAY_DISK_SPACE":
                    code = "replay_disk_space_low"
                self._stop_locked(code)
        finally:
            with self._condition:
                session.queue.clear()
                session.queue_bytes = 0
                if self._session is session:
                    self._session = None
                    self._effective_version = self._config_version
                    self._start_locked()
            self._notify()

    def _open_writer(self, session: _Session):
        """重试交接期间的文件锁占用；已有事件需要等候写入。"""
        context_json = encode_json(session.context)
        while True:
            with self._condition:
                if session.stopping and not session.queue:
                    return None
            try:
                writer = self._writer_factory(self.root, self.account_id, session.recording_id, context_json)
                if writer is None:
                    raise RuntimeError("writer factory returned no writer")
            except BlockingIOError:
                with self._condition:
                    if session.stopping and not session.queue:
                        return None
                    first_wait = not session.waiting_for_writer
                    session.waiting_for_writer = True
                if first_wait:
                    self._notify()
                with self._condition:
                    self._condition.wait(0.1)
                continue
            with self._condition:
                session.waiting_for_writer = False
                session.state = "recording"
                if not session.stopping:
                    self._effective_version = self._config_version
            self._notify()
            return writer

    def _seed_context_metadata(self, session: _Session) -> None:
        with self._condition:
            owner = self._owner if self._session is session and not session.stopping else None
        hook = getattr(owner, "replay_context_metadata", None)
        environment = "unknown"
        account_mode = None
        if callable(hook):
            try:
                metadata = hook()
                if (
                    isinstance(metadata, dict)
                    and set(metadata) <= {"environment", "account_mode"}
                    and metadata.get("environment") in {"live", "demo", "unknown"}
                    and ("account_mode" not in metadata or metadata["account_mode"] in {"live", "kq", "sim"})
                ):
                    environment = metadata["environment"]
                    account_mode = metadata.get("account_mode")
            except Exception:  # noqa: BLE001 - 元数据来源失败不能改变交易或补造环境
                pass
        with self._condition:
            if self._session is session:
                session.context["environment"] = environment
                if account_mode is not None:
                    session.context["account_mode"] = account_mode

    def _seed_cached_observations(self, session: _Session) -> None:
        with self._condition:
            owner = self._owner if self._session is session and not session.stopping else None
        seed = getattr(owner, "replay_seed_observations", None)
        if not callable(seed):
            return
        for topic, data in seed():
            self.observe(topic, data)

    def _seed_interest(self, session: _Session) -> None:
        with self._condition:
            owner = self._owner if self._session is session and not session.stopping else None
        try:
            seed = getattr(owner, "replay_interest_symbols", None)
            if not callable(seed):
                return
            interest = seed()
            if not isinstance(interest, dict) or set(interest) - {"orders", "positions"}:
                raise ValueError("replay_interest_scope")
            groups = {}
            for name in ("orders", "positions"):
                values = interest.get(name, [])
                if not isinstance(values, (list, set, tuple)) or not all(
                    isinstance(symbol, str) and 0 < len(symbol.encode("utf-8")) <= 256 for symbol in values
                ):
                    raise ValueError("replay_interest_budget")
                groups[name] = set(values)
            with self._condition:
                if self._session is session and not session.stopping:
                    for name, target in (("orders", self._interest_orders), ("positions", self._interest_positions)):
                        if (observed := session.seed_interest_scopes.get(name)) is not None:
                            if len(target | groups[name]) > MAX_IDENTITIES:
                                raise ValueError("replay_interest_budget")
                            target.update(groups[name] - observed)
        finally:
            with self._condition:
                session.seed_interest_scopes.clear()

    def _queue_poll_locked(self, session: _Session) -> None:
        now = clock_monotonic()
        if session.stopping or session.poll_pending or now < session.next_poll:
            return
        timestamp = self._now_us()
        if type(timestamp) is not int or not 0 <= timestamp <= 2**53 - 1:
            raise ValueError("replay_invalid_clock")
        if session.last_time_us is not None and timestamp < session.last_time_us:
            raise ValueError("replay_clock_rollback")
        expired = [symbol for symbol, until in self._tails.items() if timestamp >= until]
        for symbol in expired:
            del self._tails[symbol]
        session.queue.append(("poll_seal" if expired else "poll", str(timestamp)))
        session.poll_pending = True
        session.last_time_us = timestamp
        session.next_poll = now + 0.1

    def close(self, timeout: float | None = 1.0) -> dict[str, object]:
        """停止接收并等待后台关闭；超时后继续在后台排空。"""
        if timeout is not None and not 0 <= timeout <= 30:
            raise ValueError("invalid_replay_close_timeout")
        with self._condition:
            self._closed = True
            self._desired = False
            self._unbind_hook_locked()
            if self._owner is not None and getattr(self._owner, "_replay_recorder", None) is self:
                for name in ("_replay_recorder", "_replay_account_id", "_replay_sync_generation"):
                    if hasattr(self._owner, name):
                        delattr(self._owner, name)
            self._owner = None
            self._stop_locked()
            session = self._session
            thread = session.thread if session is not None else None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._notify()
        return self.status()
