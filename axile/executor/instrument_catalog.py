"""服务级内存目录：按需合并、来源接替、全量成功后整体替换。"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso, get_default_clock
from axile.executor.instrument_catalog_records import Instrument, normalize_catalog_symbol

type Query = Callable[[list[str] | None, float | None], list[Instrument]]


class CatalogError(RuntimeError):
    """必要资料不可用时在下单前终止执行。"""

    def __init__(
        self, message: str, reason_code: str = "CATALOG.UNAVAILABLE", *, causes: list[dict[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.execution_error = message
        self.causes = list(causes or [])


_query_deadline: ContextVar[float | None] = ContextVar("catalog_query_deadline", default=None)


def catalog_deadline(timeout: float | None, absolute: float | None = None) -> float | None:
    """复用已有截止时间，各线程／IPC 层只能缩短预算，不能重新延长。"""
    candidates = [value for value in (_query_deadline.get(), absolute) if value is not None]
    if timeout is not None:
        candidates.append(clock_monotonic() + timeout)
    return min(candidates) if candidates else None


def catalog_remaining(deadline: float | None) -> float | None:
    """取得一次批量查询的剩余额度；到期后不允许再开始原生请求。"""
    if deadline is None:
        return None
    remaining = deadline - clock_monotonic()
    if remaining <= 0:
        raise CatalogError("合约资料查询超时", "CATALOG.TIMEOUT")
    return remaining


@contextmanager
def catalog_query_scope(deadline: float | None) -> Iterator[None]:
    """绑定当前来源调用的预算，兼容原有双参数 Query 接口。"""
    token = _query_deadline.set(catalog_deadline(None, deadline))
    try:
        yield
    finally:
        _query_deadline.reset(token)


@dataclass(frozen=True)
class CatalogLimits:
    """目录批次预算；未指定总预算的 CTP 执行准备保留单次响应超时。"""

    ensure: float = 15.0
    demand_source: float = 5.0
    source: float = 60.0
    refresh: float = 180.0

    @classmethod
    def from_env(cls) -> CatalogLimits:
        """配置等待完整响应的预算，与渠道请求发送间隔分别处理。"""
        defaults = cls()
        source = float(os.getenv("AXILE_CATALOG_SOURCE_TIMEOUT", str(defaults.source)))
        refresh = float(os.getenv("AXILE_CATALOG_REFRESH_TIMEOUT", str(defaults.refresh)))
        if not 0 < source <= refresh:
            raise ValueError("合约目录预算须满足 0 < source <= refresh")
        return cls(source=source, refresh=refresh)


@dataclass
class _Attempt:
    deadline: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    rows: list[Instrument] | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class _Source:
    query: Query
    channel: str = ""
    last_execution: str = ""
    enabled: bool = True


class InstrumentCatalog:
    """线程安全的共享目录；不保存目录版本或历史快照。"""

    def __init__(self, limits: CatalogLimits | None = None) -> None:
        self.limits = limits or CatalogLimits.from_env()
        self._lock = threading.RLock()
        self._rows: dict[str, Instrument] = {}
        self._sources: dict[str, _Source] = {}
        self._pending: dict[tuple[bool, str], _Attempt] = {}
        self._tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._active: str | None = None
        self._sessions: set[str] = set()
        self._last_success: str | None = None
        self._last_error: str | None = None
        self._closed = False
        self._automatic: tuple[str, float] | None = None

    def register(
        self, name: str, query: Query, *, channel: str = "", last_execution: str = "", enabled: bool = True
    ) -> None:
        """保留已登录会话；只有启用的来源参与公共目录刷新。"""
        with self._lock:
            if not self._closed:
                self._sources[name] = _Source(query, channel, last_execution, enabled)
            automatic = self._automatic
        if enabled and automatic is not None and get_default_clock().time() <= automatic[1]:
            self.refresh(automatic[0])

    def arm_refresh(self, session: str, until: float) -> None:
        """盘前先登记场次；首个账户就绪后即可预热，无须等待其他账户。"""
        with self._lock:
            self._automatic = (session, until)
            available = any(source.enabled for source in self._sources.values())
        if available and get_default_clock().time() <= until:
            self.refresh(session)

    def unregister(self, name: str) -> None:
        """来源退出后不再分配新查询。"""
        with self._lock:
            self._sources.pop(name, None)

    def snapshot(self) -> dict[str, Instrument]:
        """复制当前可用记录，调用方不会观察到替换中间态。"""
        with self._lock:
            return {key: row for key, row in self._rows.items() if not row.expired()}

    def _find(self, symbol: str) -> Instrument | None:
        symbol = normalize_catalog_symbol(symbol)
        if "." in symbol:
            row = self._rows.get(symbol)
            return row if row is not None and not row.expired() else None
        matches = [row for row in self._rows.values() if row.symbol == symbol and not row.expired()]
        if len(matches) > 1:
            raise CatalogError(f"合约代码存在交易所歧义: {symbol}", "CATALOG.AMBIGUOUS")
        return matches[0] if matches else None

    def _pending_attempt(self, symbol: str, require_specs: bool) -> _Attempt | None:
        attempt = self._pending.get((require_specs, symbol))
        now = clock_monotonic()
        if attempt is not None and (attempt.deadline is None or attempt.deadline > now):
            return attempt
        local = symbol.split(".", 1)[-1]
        matches = [
            a
            for (specs, key), a in self._pending.items()
            if specs == require_specs
            and (a.deadline is None or a.deadline > now)
            and (key == local or ("." not in symbol and key.split(".", 1)[-1] == local))
        ]
        return matches[0] if len(matches) == 1 else None

    def ensure(
        self,
        symbols: list[str],
        checkpoint: Callable[[], None] | None = None,
        timeout: float | None = None,
        *,
        require_specs: bool = False,
        source_name: str | None = None,
    ) -> dict[str, Instrument]:
        """缺失资料由执行账户补齐；重叠查询共享，取消仅结束当前等待。"""
        if timeout is None and not require_specs:
            timeout = self.limits.ensure
        deadline = catalog_deadline(timeout)
        while True:
            with self._lock:
                current = {raw: self._find(raw) for raw in symbols}
                if require_specs:
                    current = {raw: row if row is not None and row.has_specs else None for raw, row in current.items()}
                if all(row is not None for row in current.values()):
                    return {raw: row for raw, row in current.items() if row is not None}
                missing = list(
                    dict.fromkeys(normalize_catalog_symbol(raw) for raw, row in current.items() if row is None)
                )
                fresh, waits = [], []
                batch = _Attempt(deadline=deadline)
                for symbol in missing:
                    attempt = self._pending_attempt(symbol, require_specs)
                    if attempt is None:
                        self._pending[require_specs, symbol] = attempt = batch
                        fresh.append(symbol)
                    waits.append(attempt)
                if fresh:
                    threading.Thread(
                        target=self._fill,
                        args=(fresh, batch, require_specs, source_name, deadline),
                        daemon=True,
                        name="instrument-demand",
                    ).start()
            for attempt in waits:
                while not attempt.done.is_set():
                    if checkpoint is not None:
                        checkpoint()
                    remaining = 0.05 if deadline is None else deadline - clock_monotonic()
                    if remaining <= 0:
                        raise CatalogError("等待合约资料超时", "CATALOG.TIMEOUT")
                    get_default_clock().event_wait(attempt.done, min(0.05, remaining))
                if checkpoint is not None:
                    checkpoint()
                if attempt.error is not None:
                    raise attempt.error
            # 刷新可能替换了等待期间的结果；在同一锁内复制全部必要资料。
            if deadline is not None and clock_monotonic() >= deadline:
                raise CatalogError("等待合约资料超时", "CATALOG.TIMEOUT")

    def _call(self, query: Query, symbols: list[str] | None, timeout: float | None) -> list[Instrument]:
        attempt = _Attempt()
        deadline = catalog_deadline(timeout)

        def invoke() -> None:
            try:
                with catalog_query_scope(deadline):
                    attempt.rows = query(symbols, catalog_remaining(deadline))
            except Exception as exc:
                attempt.error = exc
            finally:
                attempt.done.set()

        threading.Thread(target=invoke, daemon=True, name="instrument-source").start()
        while not attempt.done.is_set():
            remaining = catalog_remaining(deadline)
            get_default_clock().event_wait(attempt.done, 0.05 if remaining is None else min(0.05, remaining))
            with self._lock:
                if self._closed or not any(source.query is query for source in self._sources.values()):
                    raise CatalogError("合约查询来源已退出")
        catalog_remaining(deadline)
        if attempt.error is not None:
            raise attempt.error
        return attempt.rows or []

    def _load(
        self,
        symbols: list[str] | None,
        deadline: float | None,
        task: dict | None = None,
        *,
        require_specs: bool = False,
        source_name: str | None = None,
    ) -> list[Instrument]:
        errors = []
        tried: set[str] = set()
        while True:
            remaining = None if deadline is None else deadline - clock_monotonic()
            if remaining is not None and remaining <= 0:
                break
            with self._lock:
                if self._closed:
                    raise CatalogError("合约目录已关闭")
                sources = sorted(
                    (
                        (name, source)
                        for name, source in self._sources.items()
                        if name not in tried
                        and (not require_specs or source.channel == "ctp")
                        and (name == source_name if source_name is not None else source.enabled)
                    ),
                    key=lambda item: (item[1].channel == "ctp", item[1].last_execution),
                    reverse=True,
                )
                if not sources:
                    break
                name, source = sources[0]
                query = source.query
                tried.add(name)
                if task is not None:
                    task["source"] = name
                    task["attempts"] += 1
                    task["phase"] = "querying"
            try:
                timeout = (
                    None
                    if remaining is None
                    else min(remaining, self.limits.source if symbols is None else self.limits.demand_source)
                )
                rows = self._call(query, symbols, timeout)
                with self._lock:
                    current_source = self._sources.get(name)
                    if current_source is None or current_source.query is not query:
                        raise CatalogError("合约查询来源已退出")
                    if task is not None:
                        task["phase"] = "validating"
                if not rows:
                    raise CatalogError("合约查询返回空结果或合约已下市", "CATALOG.NOT_FOUND")
                for row in rows:
                    row.validate(require_specs=require_specs or source.channel == "ctp")
                if symbols is not None:
                    for symbol in symbols:
                        matches = [r for r in rows if normalize_catalog_symbol(symbol) in {r.key, r.symbol}]
                        if not matches:
                            raise CatalogError(f"合约资料不存在: {symbol}", "CATALOG.NOT_FOUND")
                        if len(matches) > 1:
                            raise CatalogError(f"合约代码存在交易所歧义: {symbol}", "CATALOG.AMBIGUOUS")
                return rows
            except Exception as exc:
                errors.append(
                    {
                        "reason_code": getattr(
                            exc,
                            "reason_code",
                            "CATALOG.TIMEOUT" if isinstance(exc, TimeoutError) else "CATALOG.UNAVAILABLE",
                        ),
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        codes = {error["reason_code"] for error in errors}
        reason = next(iter(codes)) if len(codes) == 1 else "CATALOG.MIXED_FAILURE" if codes else "CATALOG.UNAVAILABLE"
        raise CatalogError(
            "获取合约资料失败: "
            + ("; ".join(error["message"] for error in errors) if errors else "没有可用的已登录查询来源"),
            reason,
            causes=errors,
        )

    def _fill(
        self,
        symbols: list[str],
        attempt: _Attempt,
        require_specs: bool,
        source_name: str | None,
        deadline: float | None,
    ) -> None:
        try:
            with catalog_query_scope(deadline):
                rows = self._load(symbols, deadline, require_specs=require_specs, source_name=source_name)
            with self._lock:
                if not self._closed:
                    catalog_remaining(deadline)
                    for row in rows:
                        current = self._rows.get(row.key)
                        if row.has_specs or current is None or not current.has_specs:
                            self._rows[row.key] = row
            attempt.rows = rows
        except Exception as exc:
            attempt.error = exc
        finally:
            with self._lock:
                for symbol in symbols:
                    if self._pending.get((require_specs, symbol)) is attempt:
                        del self._pending[require_specs, symbol]
            attempt.done.set()

    def refresh(self, session: str | None = None) -> dict[str, Any]:
        """合并全量任务；同场自动刷新成功后不再重复。"""
        with self._lock:
            if self._active is not None:
                task = self._tasks[self._active]
                if session:
                    task["sessions"].add(session)
                return self._public_task(task)
            if session in self._sessions:
                return {"task_id": None, "status": "skipped", "reason": "该场目录已刷新"}
            task_id = uuid4().hex
            task = {
                "task_id": task_id,
                "status": "running",
                "phase": "querying",
                "created_at": clock_now_iso(),
                "finished_at": None,
                "error": None,
                "records": 0,
                "attempts": 0,
                "source": None,
                "sessions": {session} if session else set(),
            }
            self._tasks[task_id] = task
            self._active = task_id
            threading.Thread(target=self._refresh, args=(task,), daemon=True, name="instrument-refresh").start()
            return self._public_task(task)

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in task.items() if k != "sessions"}

    def _refresh(self, task: dict[str, Any]) -> None:
        try:
            rows = self._load(None, clock_monotonic() + self.limits.refresh, task)
            with self._lock:
                if self._closed:
                    raise CatalogError("合约目录已关闭")
                self._rows = {row.key: row for row in rows}
                task.update(status="succeeded", phase="ready", records=len(self._rows))
                self._last_success, self._last_error = clock_now_iso(), None
                self._sessions.update(task["sessions"])
        except Exception as exc:
            with self._lock:
                task.update(status="failed", phase="failed", error=str(exc))
                self._last_error = str(exc)
        finally:
            with self._lock:
                task["finished_at"] = clock_now_iso()
                self._active = None
                while len(self._tasks) > 100:
                    self._tasks.popitem(last=False)

    def task(self, task_id: str) -> dict[str, Any] | None:
        """只读查询刷新任务。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return self._public_task(task) if task is not None else None

    def status(self) -> dict[str, Any]:
        """返回内存目录状态，不包含凭据或版本号。"""
        with self._lock:
            return {
                "records": len(self.snapshot()),
                "last_success_at": self._last_success,
                "last_error": self._last_error,
                "sources": sum(source.enabled for source in self._sources.values()),
                "current_task": self.task(self._active) if self._active else None,
            }

    def close(self) -> None:
        """停止接收来源，阻止退出后晚到结果写入缓存。"""
        with self._lock:
            self._closed = True
            self._sources.clear()


class InstrumentCatalogProvider(Protocol):
    """本地和跨 worker 的统一目录边界。"""

    def bind(self, query: Query, *, channel: str = "") -> None:
        """注册当前已登录查询会话。"""
        ...

    def ensure(
        self,
        symbols: list[str],
        checkpoint: Callable[[], None] | None = None,
        timeout: float | None = None,
        *,
        require_specs: bool = False,
    ) -> Mapping[str, Instrument]:
        """读取或补齐少量合约。"""
        ...

    def close(self) -> None:
        """注销当前来源，不清除共享缓存。"""
        ...

    def progress(self, phase: str) -> None:
        """报告连接初始化的实际进展。"""
        ...


class LocalInstrumentCatalogProvider:
    """独立执行器共用进程级缓存，每个会话有独立的来源身份。"""

    def __init__(self, catalog: InstrumentCatalog | None = None) -> None:
        self.catalog = catalog or LOCAL_INSTRUMENT_CATALOG
        self.owner = uuid4().hex

    def bind(self, query: Query, *, channel: str = "") -> None:
        """登录完成后注册查询能力。"""
        self.catalog.register(self.owner, query, channel=channel)

    def ensure(
        self,
        symbols: list[str],
        checkpoint: Callable[[], None] | None = None,
        timeout: float | None = None,
        *,
        require_specs: bool = False,
    ) -> Mapping[str, Instrument]:
        """仅请求必要资料。"""
        return self.catalog.ensure(symbols, checkpoint, timeout, require_specs=require_specs, source_name=self.owner)

    def close(self) -> None:
        """注销来源。"""
        self.catalog.unregister(self.owner)

    def progress(self, phase: str) -> None:
        """独立执行器无需跨进程初始化看门狗。"""


LOCAL_INSTRUMENT_CATALOG = InstrumentCatalog()
