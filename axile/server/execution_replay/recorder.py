"""账户唯一的有界录制旁路；采集线程不执行原生写入或排空。"""

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
from axile.server.execution_replay.contracts import MAX_MESSAGE_BYTES, encode_json, freeze_data, freeze_json
from axile.server.execution_replay.service import native_writer

MAX_QUEUE_MESSAGES = 4_096
MAX_QUEUE_BYTES = 16 * 1024 * 1024
MAX_IDENTITIES = 4_096
TAIL_US = 5_000_000
_TERMINAL = frozenset({"filled", "canceled", "rejected", "expired"})
_TRADE_FIELDS = ("symbol", "price", "quantity", "direction", "position_side", "offset", "order_id")


@dataclass
class _Session:
    recording_id: str
    context: dict[str, object]
    queue: deque[tuple[str, str, int]] = field(default_factory=deque)
    queued_bytes: int = 0
    sequence: int = 0
    last_time_us: int | None = None
    stopping: bool = False
    error: str | None = None
    deadline: float | None = None
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
        self._positions: set[str] = set()
        self._position_parts: set[tuple[str, str | None]] = set()
        self._orders: set[tuple[str, str]] = set()
        self._order_changes: dict[tuple[str, str], int] = {}
        self._terminal_orders: set[tuple[str, str]] = set()
        self._order_set_revisions: dict[str, int] = {}
        self._order_set_observation: dict[str, tuple[int, int, int]] = {}
        self._trade_facts: dict[str, dict[str, object]] = {}
        self._order_executions: dict[tuple[str, str], str] = {}
        self._order_origins: dict[tuple[str, str], str] = {}
        self._command_metadata: dict[str, dict[str, str]] = {}
        self._interest_orders: set[str] = set()
        self._interest_positions: set[str] = set()
        self._tails: dict[str, int] = {}
        self._tail_trades: set[str] = set()
        self._tail_commands: set[str] = set()

    @staticmethod
    def _freeze_context(context: Mapping[str, object]) -> dict[str, object]:
        frozen = freeze_json(context)
        if not isinstance(frozen, dict):
            raise ValueError("invalid_replay_context")
        frozen["schema_version"] = 1
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
                self._positions.clear()
                self._position_parts.clear()
                self._orders.clear()
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
        self._orders.clear()
        self._order_changes.clear()
        self._terminal_orders.clear()
        self._order_set_revisions.clear()
        self._order_set_observation.clear()
        self._trade_facts.clear()
        self._order_executions.clear()
        self._order_origins.clear()
        self._command_metadata.clear()
        self._interest_orders.clear()
        self._interest_positions.clear()
        self._tail_trades.clear()
        self._tail_commands.clear()

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
            session.deadline = clock_monotonic() + 1.0
            self._condition.notify_all()

    def observe(self, topic: str, data: Mapping[str, object]) -> None:
        """短临界区冻结、取时、编号与有界入队，任何失败只停止录制。"""
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
                self._associate_locked(topic, frozen)
                message = encode_json(
                    {
                        "schema_version": 1,
                        "recording_id": session.recording_id,
                        "capture_seq": session.sequence + 1,
                        "time_us": timestamp,
                        "topic": topic,
                        "data": frozen,
                    }
                )
                size = len(message.encode("utf-8"))
                if size > MAX_MESSAGE_BYTES:
                    raise ValueError("replay_message_budget")
                if len(session.queue) >= MAX_QUEUE_MESSAGES or session.queued_bytes + size > MAX_QUEUE_BYTES:
                    raise ValueError("replay_queue_budget")
                session.queue.append(("message", message, size))
                session.queued_bytes += size
                session.sequence += 1
                session.last_time_us = timestamp
                self._track_locked(topic, frozen, timestamp)
                self._condition.notify_all()
            except Exception as error:  # noqa: BLE001 - 采集旁路不得改变业务结果
                code = (
                    str(error)
                    if isinstance(error, ValueError) and str(error).startswith("replay_")
                    else "replay_capture_error"
                )
                self._stop_locked(code)

    def begin_query(self, scope: list[str]) -> dict[str, object] | None:
        """短锁分配查询覆盖 token；发起次序与消息序号分别维护。"""
        with self._condition:
            session = self._session
            if session is None or session.stopping or not self._desired:
                return None
            if (
                not isinstance(scope, list)
                or len(scope) > MAX_IDENTITIES
                or not all(isinstance(symbol, str) and 0 < len(symbol) <= 128 for symbol in scope)
            ):
                return None
            session.request_revision += 1
            while len(session.queries) + len(session.completed_queries) >= MAX_IDENTITIES:
                if not session.completed_queries:
                    self._stop_locked("replay_query_budget")
                    return None
                del session.completed_queries[next(iter(session.completed_queries))]
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
                self._order_set_revisions.clear()
                self._order_set_observation.clear()
                if self._owner is not None:
                    setattr(self._owner, "_replay_sync_generation", self._sync_generation)
                data["sync_generation"] = self._sync_generation
        generation = data.setdefault("sync_generation", self._sync_generation)
        if (topic == "/order" or is_quote) and (type(generation) is not int or generation != self._sync_generation):
            data["book_valid" if is_quote else "confirmed_book"] = False
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
            symbol in self._positions
            or symbol in self._interest_orders
            or symbol in self._interest_positions
            or any(key[0] == symbol for key in self._orders)
            or any(symbol in symbols for symbols in self._executions.values())
            or timestamp < self._tails.get(symbol, 0)
        )

    def _associate_locked(self, topic: str, data: dict[str, object]) -> None:
        self._associate_command_locked(topic, data)
        if topic == "/command" and data.get("operation") != "place_order":
            return
        self._associate_order_locked(data)

    def _associate_command_locked(self, topic: str, data: dict[str, object]) -> None:
        command_id = data.get("command_id")
        if isinstance(command_id, str):
            metadata = self._command_metadata.get(command_id, {})
            for field, value in metadata.items():
                if data.get(field) in (None, "", "unknown"):
                    data[field] = value
            if topic == "/command":
                additions = {
                    field: data[field]
                    for field in ("execution_id", "origin")
                    if isinstance(data.get(field), str) and data[field] not in ("", "unknown")
                }
                if any(field in metadata and metadata[field] != value for field, value in additions.items()):
                    raise ValueError("replay_identity_conflict")
                if additions:
                    self._command_metadata[command_id] = {**metadata, **additions}

    def _associate_order_locked(self, data: dict[str, object]) -> None:
        symbol, order_id = data.get("symbol"), data.get("order_id")
        if isinstance(symbol, str) and symbol and isinstance(order_id, str) and order_id:
            key = symbol, order_id
            execution_id = data.get("execution_id")
            if isinstance(execution_id, str) and execution_id:
                self._order_executions[key] = execution_id
            elif key in self._order_executions:
                data["execution_id"] = self._order_executions[key]
            origin = data.get("origin")
            if isinstance(origin, str) and origin not in ("", "unknown"):
                self._order_origins[key] = origin
            elif key in self._order_origins:
                data["origin"] = self._order_origins[key]

    def _track_locked(self, topic: str, data: dict[str, object], timestamp: int) -> None:
        if topic == "/order":
            if data.get("sync_generation") == self._sync_generation:
                self._track_order_locked(data, timestamp)
        elif topic == "/order_set":
            self._track_order_set_locked(data, timestamp)
        elif topic == "/account_observation":
            self._track_positions_locked(data, timestamp)
        elif topic == "/trade":
            key, symbol = data.get("trade_key"), data.get("symbol")
            self._track_trade_order_locked(data)
            if data.get("identity_quality") == "stable" and isinstance(key, str) and key not in self._tail_trades:
                self._tail_trades.add(key)
                if isinstance(symbol, str):
                    self._tails[symbol] = timestamp + TAIL_US
        elif topic == "/command" and data.get("phase") == "request":
            key, symbol = data.get("command_id"), data.get("symbol")
            if isinstance(key, str) and key not in self._tail_commands and isinstance(symbol, str):
                self._tail_commands.add(key)
                self._tails[symbol] = timestamp + TAIL_US
        seed_scopes = self._session.seed_interest_scopes.values() if self._session else ()
        if (
            max(
                len(self._positions),
                len(self._position_parts),
                len(self._orders),
                len(self._order_changes),
                len(self._terminal_orders),
                len(self._order_set_revisions),
                len(self._order_set_observation),
                len(self._trade_facts),
                len(self._order_executions),
                len(self._order_origins),
                len(self._command_metadata),
                len(self._tail_trades),
                len(self._tail_commands),
                len(self._tails),
                *(len(scope) for scope in seed_scopes if scope is not None),
            )
            > MAX_IDENTITIES
        ):
            raise ValueError("replay_identity_budget")

    def _track_seed_scope_locked(self, name: str, scope: tuple[str, tuple[str, ...]]) -> None:
        session = self._session
        if session is None or name not in session.seed_interest_scopes:
            return
        if scope[0] == "account":
            session.seed_interest_scopes[name] = None
        elif (symbols := session.seed_interest_scopes[name]) is not None:
            symbols.update(scope[1])

    @staticmethod
    def _order_key(data: dict[str, object]) -> tuple[str, str] | None:
        symbol, order_id = data.get("symbol"), data.get("order_id")
        if not isinstance(symbol, str) or not symbol or not isinstance(order_id, str) or not order_id:
            return None
        return symbol, order_id

    def _forget_order_locked(self, key: tuple[str, str], timestamp: int) -> None:
        if key in self._orders:
            self._orders.discard(key)
            self._tails[key[0]] = timestamp + TAIL_US

    def _track_order_locked(self, data: dict[str, object], timestamp: int, *, individual: bool = True) -> None:
        key = self._order_key(data)
        if key is None or self._session is None:
            return
        if individual and "query_basis" in data:
            basis = data["query_basis"]
            if (
                not isinstance(basis, dict)
                or self._order_changes.get(key, 0) > basis["start_seq"]
                or basis["request_revision"] <= self._order_set_revisions.get(key[0], 0)
                or any(
                    (generation, sequence) > (self._sync_generation, basis["start_seq"])
                    for generation, _, sequence in (
                        self._order_set_observation.get(scope, (0, 0, 0)) for scope in (key[0], "")
                    )
                )
            ):
                return
        if data.get("status") in _TERMINAL:
            self._terminal_orders.add(key)
            self._forget_order_locked(key, timestamp)
        elif key not in self._terminal_orders:
            self._orders.add(key)
        else:
            return
        if individual:
            self._order_changes[key] = self._session.sequence

    def _track_trade_order_locked(self, data: dict[str, object]) -> None:
        trade = data.get("trade_key")
        if data.get("identity_quality") != "stable" or not isinstance(trade, str):
            return
        previous = self._trade_facts.get(trade, {})
        before = self._known_trade_facts(previous)
        incoming_quality = data.get("field_quality")
        incoming_quality = incoming_quality if isinstance(incoming_quality, dict) else {}
        previous_quality = previous.get("field_quality")
        previous_quality = previous_quality if isinstance(previous_quality, dict) else {}
        quality = {**previous_quality, **incoming_quality}
        merged = {**previous, "field_quality": quality}
        for name in _TRADE_FIELDS:
            value = data.get(name)
            if name in before and incoming_quality.get(name) in {"missing", "invalid"}:
                quality[name] = "known"
                continue
            if value is None:
                continue
            if name in {"direction", "position_side", "offset"} and isinstance(value, str):
                value = value.casefold()
            if name in before and name != "order_id" and value != before[name]:
                continue
            merged[name] = value
        self._trade_facts[trade] = merged
        after = self._known_trade_facts(merged)
        key = self._order_key(after)
        if key is not None and before != after and self._session is not None:
            self._order_changes[key] = self._session.sequence

    @staticmethod
    def _known_trade_facts(data: dict[str, object]) -> dict[str, object]:
        quality = data.get("field_quality")
        quality = quality if isinstance(quality, dict) else {}
        return {
            field: data[field]
            for field in _TRADE_FIELDS
            if data.get(field) is not None and quality.get(field) not in {"missing", "invalid"}
        }

    def _order_scope_symbols_locked(self, scope: tuple[str, tuple[str, ...]], rows: list[dict]) -> set[str]:
        if scope[0] == "symbols":
            return set(scope[1])
        return (
            {key[0] for key in self._order_changes}
            | {key[0] for key in self._orders}
            | self._interest_orders
            | set(self._order_set_revisions)
            | {row["symbol"] for row in rows}
        )

    def _current_set_scope_locked(self, symbols: Iterable[str], revision: int) -> bool:
        return all(revision > self._order_set_revisions.get(symbol, 0) for symbol in symbols)

    def _observe_set_locked(self, symbol: str, revision: int, sequence: int) -> None:
        if (self._sync_generation, revision) > self._order_set_observation.get(symbol, (0, 0, 0))[:2]:
            self._order_set_observation[symbol] = self._sync_generation, revision, sequence

    def _track_order_set_locked(self, data: dict[str, object], timestamp: int) -> None:
        basis, scope, rows = data.get("query_basis"), self._scope_key(data.get("scope")), data.get("orders", [])
        if (
            self._session is None
            or not isinstance(basis, dict)
            or scope is None
            or data.get("scope_complete") is not True
            or data.get("synchronized") is not True
            or data.get("sync_generation") != self._sync_generation
            or not isinstance(rows, list)
            or any(not isinstance(row, dict) or self._order_key(row) is None for row in rows)
        ):
            return
        start, revision = basis["start_seq"], basis["request_revision"]
        account = self._order_set_observation.get("")
        if account is not None and account[:2] >= (self._sync_generation, revision):
            return
        symbols = self._order_scope_symbols_locked(scope, rows)
        global_scope = scope[0] == "account" and self._current_set_scope_locked(symbols, revision)
        for symbol in symbols:
            if revision <= self._order_set_revisions.get(symbol, 0):
                continue
            self._order_set_revisions[symbol] = revision
            self._observe_set_locked(symbol, revision, self._session.sequence)
            self._track_seed_scope_locked("orders", ("symbols", (symbol,)))
            self._interest_orders.discard(symbol)
            present = set()
            for row in rows:
                if row["symbol"] != symbol:
                    continue
                key = self._order_key(row)
                present.add(key)
                if self._order_changes.get(key, 0) <= start:
                    self._track_order_locked(row, timestamp, individual=False)
            for key in tuple(self._orders):
                if key[0] == symbol and key not in present and self._order_changes.get(key, 0) <= start:
                    self._forget_order_locked(key, timestamp)
        if global_scope:
            self._observe_set_locked("", revision, self._session.sequence)
            self._track_seed_scope_locked("orders", scope)

    @staticmethod
    def _position_update(row: object) -> tuple[tuple[str, str | None], bool] | None:
        if not isinstance(row, dict):
            return None
        quality = row.get("field_quality")
        quality = quality if isinstance(quality, dict) else {}
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol or quality.get("symbol") in {"missing", "invalid"}:
            return None
        side = row.get("position_side")
        if side not in {"NET", "LONG", "SHORT"} or quality.get("position_side") in {"missing", "invalid"}:
            side = None
        quantity = row.get("quantity")
        is_zero = (
            type(quantity) in (int, float) and quantity == 0 and quality.get("quantity") not in {"missing", "invalid"}
        )
        return (symbol, side), is_zero

    def _track_positions_locked(self, data: dict[str, object], timestamp: int) -> None:
        generation = data.get("sync_generation", self._sync_generation)
        if type(generation) is not int or generation != self._sync_generation:
            return
        positions = data.get("positions", [])
        if not isinstance(positions, list):
            return
        scope = self._scope_key(data.get("scope"))
        if data.get("scope_complete") is True and scope is not None:
            self._track_seed_scope_locked("positions", scope)
            self._clear_interest_locked(self._interest_positions, data.get("scope"))
            if scope[0] == "account":
                self._position_parts.clear()
            else:
                self._position_parts.difference_update(key for key in tuple(self._position_parts) if key[0] in scope[1])
        for row in positions:
            update = self._position_update(row)
            if update is None:
                continue
            key, is_zero = update
            if is_zero:
                if key[1] is not None:
                    self._position_parts.discard(key)
            else:
                self._position_parts.add(key)
        symbols = {key[0] for key in self._position_parts}
        for symbol in self._positions - symbols:
            self._tails[symbol] = timestamp + TAIL_US
        self._positions = symbols

    def _clear_interest_locked(self, interest: set[str], scope: object) -> None:
        key = self._scope_key(scope)
        if key is not None:
            if key[0] == "account":
                interest.clear()
            else:
                interest.difference_update(key[1])

    def begin_execution(
        self, execution_id: str, symbols: Iterable[str], data: Mapping[str, object] | None = None
    ) -> None:
        """登记执行相关品种并记录开始，不发起行情订阅。"""
        execution_symbols = set(symbols)
        with self._condition:
            self._executions[execution_id] = execution_symbols
            if len(self._executions) + sum(len(values) for values in self._executions.values()) > MAX_IDENTITIES:
                self._stop_locked("replay_identity_budget")
                return
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
            if len(session.queue) >= MAX_QUEUE_MESSAGES + 8:
                self._stop_locked("replay_control_budget")
                return
            session.queue.append(("seal", "", 0))
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
            writer = self._writer_factory(
                self.root, self.account_id, session.recording_id, encode_json(session.context)
            )
            with self._condition:
                session.state = "recording"
                self._effective_version = self._config_version
            self._notify()
            self._seed_interest(session)
            self._seed_cached_observations(session)
            while True:
                with self._condition:
                    if session.error:
                        break
                    if session.stopping and session.deadline is not None and clock_monotonic() > session.deadline:
                        raise TimeoutError("replay_close_timeout")
                    if not session.queue and session.stopping:
                        break
                    self._queue_poll_locked(session)
                    if not session.queue:
                        self._condition.wait(0.1)
                        continue
                    item = session.queue.popleft()
                    session.queued_bytes -= item[2]
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
            if not session.error:
                writer.finish()
        except Exception as error:  # noqa: BLE001 - writer 故障隔离且保留未封口文件
            with self._condition:
                code = "replay_close_timeout" if isinstance(error, TimeoutError) else "replay_writer_error"
                if isinstance(error, ValueError) and str(error).startswith("replay_"):
                    code = str(error)
                self._stop_locked(code)
        finally:
            with self._condition:
                session.queue.clear()
                session.queued_bytes = 0
                if self._session is session:
                    self._session = None
                    self._effective_version = self._config_version
                    self._start_locked()
            self._notify()

    def _seed_context_metadata(self, session: _Session) -> None:
        with self._condition:
            owner = self._owner if self._session is session and not session.stopping else None
        hook = getattr(owner, "replay_context_metadata", None)
        environment = "unknown"
        if callable(hook):
            try:
                metadata = hook()
                if (
                    isinstance(metadata, dict)
                    and set(metadata) <= {"environment"}
                    and metadata.get("environment") in {"live", "demo", "unknown"}
                ):
                    environment = metadata["environment"]
            except Exception:  # noqa: BLE001 - 元数据来源失败不能改变交易或补造环境
                pass
        with self._condition:
            if self._session is session:
                session.context["environment"] = environment

    def _seed_cached_observations(self, session: _Session) -> None:
        with self._condition:
            owner = self._owner if self._session is session and not session.stopping else None
        seed = getattr(owner, "replay_seed_observations", None)
        if not callable(seed):
            return
        for index, (topic, data) in enumerate(seed()):
            if index >= MAX_IDENTITIES:
                raise ValueError("replay_seed_budget")
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
                if (
                    not isinstance(values, (list, set, tuple))
                    or len(values) > MAX_IDENTITIES
                    or not all(isinstance(symbol, str) and 0 < len(symbol.encode("utf-8")) <= 256 for symbol in values)
                ):
                    raise ValueError("replay_interest_budget")
                groups[name] = set(values)
            with self._condition:
                if self._session is session and not session.stopping:
                    for name, target in (("orders", self._interest_orders), ("positions", self._interest_positions)):
                        if (observed := session.seed_interest_scopes.get(name)) is not None:
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
        if len(session.queue) >= MAX_QUEUE_MESSAGES + 8:
            raise ValueError("replay_control_budget")
        expired = [symbol for symbol, until in self._tails.items() if timestamp >= until]
        for symbol in expired:
            del self._tails[symbol]
        session.queue.append(("poll_seal" if expired else "poll", str(timestamp), 0))
        session.poll_pending = True
        session.next_poll = now + 0.1

    def close(self, timeout: float = 1.0) -> dict[str, object]:
        """停止接收并有界等待后台关闭；超时明确报错，不阻塞交易退出。"""
        if not 0 <= timeout <= 30:
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
            if thread.is_alive():
                with self._condition:
                    self._stop_locked("replay_close_timeout")
        self._notify()
        return self.status()
