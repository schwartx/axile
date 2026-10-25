"""
提供账户控制在执行期的内存态防护逻辑.

Notes
-----
该模块负责在单次 execution 内评估额度、执行等待或阻断动作，并产出待刷盘的
计数器增量与事件事实。
"""

from __future__ import annotations

from _thread import RLock as RLockType
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock, local
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.context import controlled_source
from axile.executor.account_control.diagnostics import AccountControlHit, operation_label
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlContextKind,
    AccountControlCounterDeltaWrite,
    AccountControlDecision,
    AccountControlEventWrite,
    AccountControlOperationPolicy,
    AccountControlPolicy,
    AccountControlScopePolicy,
    AccountControlScopeType,
    AccountControlSource,
    AccountControlTriggerBehavior,
)
from axile.executor.account_control.quota_state import (
    AccountControlQueuedRequest as _QueuedRequest,
)
from axile.executor.account_control.quota_state import (
    AccountControlQuotaState,
)
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)
from axile.executor.account_control.warnings import AccountControlQuotaWarning
from axile.executor.algorithms.utils.clock import Clock, clock_now, get_default_clock
from axile.executor.termination_cleanup import cleanup_guard_allowed

type AccountCounterKey = tuple[str, AccountControlBucketType, str, str]
type SymbolCounterKey = tuple[str, str, AccountControlBucketType, str, str]
type RecentAccountKey = tuple[str]
type RecentSymbolKey = tuple[str, str]
type GroupCounterKey = tuple[str, AccountControlBucketType, str, str]
type GroupRecentKey = tuple[str]


def _normalize_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    if metadata is None:
        return {}
    return {str(key): value for key, value in metadata.items()}


def _business_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """过滤调用方输入，确保风控保留字段只能由评估器写入。"""
    return {
        key: value
        for key, value in _normalize_metadata(metadata).items()
        if key not in {"account_control_hit", "account_control_wait_hits", "quota_warning"}
    }


def _merge_metadata(
    base: Mapping[str, object] | None,
    overlay: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        **_normalize_metadata(base),
        **_normalize_metadata(overlay),
    }


@dataclass(frozen=True)
class AccountControlWindow:
    """
    表示当前时刻对应的控制时间窗口.

    Attributes
    ----------
    control_date : str
        当前控制日期。
    day_bucket_start : str
        日级时间桶起始时间。
    minute_bucket_start : str
        分钟级时间桶起始时间。
    """

    control_date: str
    day_bucket_start: str
    minute_bucket_start: str


@dataclass(frozen=True)
class AccountControlEvaluationContext:
    """
    表示一次额度评估使用的时间上下文.

    Attributes
    ----------
    local_now : datetime
        当前策略时区下的本地时间。
    occurred_at_ms : int
        当前评估对应的毫秒时间戳。
    window : AccountControlWindow
        当前时间所属的控制窗口。
    """

    local_now: datetime
    occurred_at_ms: int
    window: AccountControlWindow


@dataclass(frozen=True)
class AccountControlEnforcementDecision:
    """
    表示额度评估后的执行动作.

    Attributes
    ----------
    kind : str
        动作类型，可能为 ``allow``、``wait`` 或 ``block``。
    wait_ms : int
        当需要等待时的等待毫秒数。
    message : str | None
        阻断时使用的错误消息。
    outcome : str | None
        阻断事件记录的 outcome。
    """

    kind: str
    wait_ms: int = 0
    message: str | None = None
    outcome: str | None = None
    hit_scope: str | None = None
    hit_rule: str | None = None
    details: AccountControlHit | None = None

    @classmethod
    def allow(cls) -> "AccountControlEnforcementDecision":
        """
        构造允许继续执行的决策.

        Returns
        -------
        AccountControlEnforcementDecision
            ``kind`` 为 ``allow`` 的决策对象。
        """
        return cls(kind="allow")

    @classmethod
    def wait(
        cls,
        wait_ms: int,
        *,
        hit_scope: str | None = None,
        hit_rule: str | None = None,
    ) -> "AccountControlEnforcementDecision":
        """
        构造需要等待的决策.

        Parameters
        ----------
        wait_ms : int
            建议等待的毫秒数。

        Returns
        -------
        AccountControlEnforcementDecision
            ``kind`` 为 ``wait`` 的决策对象。
        """
        return cls(
            kind="wait",
            wait_ms=max(wait_ms, 0),
            hit_scope=hit_scope,
            hit_rule=hit_rule,
        )

    @classmethod
    def block(cls, message: str, outcome: str) -> "AccountControlEnforcementDecision":
        """
        构造直接阻断的决策.

        Parameters
        ----------
        message : str
            阻断时使用的错误消息。
        outcome : str
            阻断事件记录的 outcome。

        Returns
        -------
        AccountControlEnforcementDecision
            ``kind`` 为 ``block`` 的决策对象。
        """
        return cls(kind="block", message=message, outcome=outcome)


@dataclass(frozen=True)
class AccountControlResolvedAttempt:
    """
    表示一次账户控制尝试在等待收敛后的最终结果.

    Attributes
    ----------
    context : AccountControlEvaluationContext
        最终放行或阻断时对应的时间上下文。
    decision : AccountControlEnforcementDecision
        最终执行动作。
    """

    context: AccountControlEvaluationContext
    decision: AccountControlEnforcementDecision


def resolve_control_window(
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> AccountControlWindow:
    """
    按策略时区解析当前的日级与分钟级时间桶.

    Parameters
    ----------
    timezone_name : str
        需要使用的 IANA 时区名称。
    now : datetime | None, optional
        指定的当前时间；为空时使用系统当前时间。

    Returns
    -------
    AccountControlWindow
        当前时间对应的控制窗口。
    """
    tz = ZoneInfo(timezone_name)
    current = clock_now(tz=tz) if now is None else now
    local_now = current.replace(tzinfo=tz) if current.tzinfo is None else current.astimezone(tz)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    minute_start = local_now.replace(second=0, microsecond=0, tzinfo=None)
    return AccountControlWindow(
        control_date=local_now.date().isoformat(),
        day_bucket_start=day_start.isoformat(),
        minute_bucket_start=minute_start.isoformat(),
    )


def _copy_scope_policy(scope_policy: AccountControlScopePolicy | None) -> AccountControlScopePolicy | None:
    if scope_policy is None:
        return None
    return AccountControlScopePolicy.model_validate(scope_policy.model_dump(mode="json"))


class AccountControlAttempt:
    """
    表示一次已经放行的账户控制尝试.

    Notes
    -----
    该对象用于在真实对外交互完成后回填最终 outcome 与附加元数据。
    """

    def __init__(self, guard: "AccountControlGuard | None", event_index: int | None) -> None:
        self._guard = guard
        self._event_index = event_index

    def record_outcome(self, outcome: str, *, metadata: Mapping[str, object] | None = None) -> None:
        """
        在对外交互结果落定后更新事件事实.

        Parameters
        ----------
        outcome : str
            调用完成后的最终结果。
        metadata : Mapping[str, object] | None, optional
            需要合并回事件中的附加元数据。
        """
        if self._guard is None or self._event_index is None:
            return
        self._guard.update_event_outcome(self._event_index, outcome=outcome, metadata=metadata)


class AccountControlGuard:
    """
    管理单次 execution 期间账户控制状态的守卫对象.

    Attributes
    ----------
    account_id : int | None
        当前执行关联的账户 ID。
    execution_id : str | None
        当前执行会话 ID。
    channel : TradeChannel
        当前交易渠道。
    policy : AccountControlPolicy | None
        当前生效的账户控制策略；为空时表示不启用控制。
    baseline : AccountControlCounterSnapshot
        从持久化层读取的基线计数快照。

    Notes
    -----
    阅读主线建议：

    1. 从 `begin_operation()` 进入一次额度检查。
    2. 再看 `_resolve_operation_attempt()` 如何在单轮评估中产生 allow / wait / block 决策。
    3. 最后看 `flush_records()` 如何导出本次 execution 累积的事件与计数器增量。
    """

    def __init__(
        self,
        *,
        account_id: int | None,
        execution_id: str | None,
        channel: TradeChannel,
        policy: AccountControlPolicy | None,
        baseline: AccountControlCounterSnapshot,
        recent_allowed_snapshot: AccountControlRecentAllowedSnapshot | None = None,
        clock: Callable[[], datetime] | None = None,
        wait_clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        wait_poll_interval_ms: int = 100,
        termination_checkpoint: Callable[[str | None], None] | None = None,
        context_kind: AccountControlContextKind = AccountControlContextKind.EXECUTION,
        source: AccountControlSource = AccountControlSource.ALGORITHM,
    ) -> None:
        self.instance_id = uuid4().hex
        self.monitor_incomplete = False
        # 日期明确的历史缺口不会在跨日后永久阻断新单。
        self.incomplete_control_dates: frozenset[str] = frozenset()
        self.counter_integrity_unavailable = False
        self._monitor_seq = 0
        self.monitor_outcome_sink: Callable[[AccountControlEventWrite], None] | None = None
        self._monitor_waits: dict[int, dict[str, object]] = {}
        self.account_id = account_id
        self.execution_id = execution_id
        self.context_kind = context_kind
        self.source = source
        self.channel = channel
        self.policy = policy
        self.baseline = baseline
        self._clock = clock
        self._wait_clock = get_default_clock() if wait_clock is None else wait_clock
        self._sleep = self._wait_clock.sleep if sleep is None else sleep
        self._wait_poll_interval_ms = max(wait_poll_interval_ms, 1)
        self._termination_checkpoint = termination_checkpoint
        self._state_lock = RLock()
        self._quota_state: AccountControlQuotaState | None = None
        self._event_seq = 0
        self._queue_seq = 0
        self._wait_queue: list[_QueuedRequest] = []
        self._thread_state = local()
        self._events: list[AccountControlEventWrite] = []
        self._quota_warnings: dict[str, AccountControlQuotaWarning] = {}
        self._account_counters: dict[AccountCounterKey, int] = {}
        self._symbol_counters: dict[SymbolCounterKey, int] = {}
        # 额度继续按操作合并；刷盘时按调用来源拆分，二者总量必须一致。
        self._account_counter_sources: dict[tuple[AccountCounterKey, AccountControlSource], int] = {}
        self._symbol_counter_sources: dict[tuple[SymbolCounterKey, AccountControlSource], int] = {}
        self._group_counters: dict[GroupCounterKey, int] = {}
        snapshot = AccountControlRecentAllowedSnapshot() if recent_allowed_snapshot is None else recent_allowed_snapshot
        self._recent_account_allowed_at: dict[RecentAccountKey, int] = dict(snapshot.account_timestamps)
        self._recent_symbol_allowed_at: dict[RecentSymbolKey, int] = dict(snapshot.symbol_timestamps)
        self._recent_group_allowed_at: dict[GroupRecentKey, int] = {}
        self._baseline_control_date = None if policy is None else self._current_context().window.control_date
        self._registry = ensure_default_account_control_registry_bootstrapped()

    def set_termination_checkpoint(self, callback: Callable[[str | None], None] | None) -> None:
        """
        绑定等待期间可调用的终止检查点.

        Parameters
        ----------
        callback : Callable[[str | None], None] | None
            等待轮询期间反复调用的检查函数；参数为当前 symbol。
        """
        self._termination_checkpoint = callback

    def share_group_timestamps(self, timestamps: dict[GroupRecentKey, int], lock: RLockType) -> None:
        """同一物理会话的独立任务共用发送间隔，不共用审计事件。"""
        with self._state_lock, lock:
            if self._quota_state is not None:
                raise ValueError("已共享完整额度状态，不能改为仅共享发送间隔")
            for key, value in self._recent_group_allowed_at.items():
                timestamps[key] = max(timestamps.get(key, value), value)
            self._recent_group_allowed_at = timestamps
            self._state_lock = lock

    def share_quota_state(self, state: AccountControlQuotaState) -> None:
        """首个操作前绑定账户额度；计数及调度共享，审计身份和落库增量保持独立。"""
        if not self._enabled():
            return
        with self._state_lock, state.lock:
            if self._quota_state is state:
                return
            if self._quota_state is not None or self._events or self._wait_queue:
                raise ValueError("账户控制 guard 必须在首个操作前绑定共享额度状态")
            assert self.account_id is not None
            state.bind(self.account_id, self.channel)
            state.merge_baseline(self._baseline_control_date, self.baseline)
            state.merge_timestamps(
                self._recent_account_allowed_at, self._recent_symbol_allowed_at, self._recent_group_allowed_at
            )
            self._quota_state = state
            self._wait_queue = state.wait_queue
            self._monitor_waits = state.monitor_waits
            self._recent_account_allowed_at = state.account_timestamps
            self._recent_symbol_allowed_at = state.symbol_timestamps
            self._recent_group_allowed_at = state.group_timestamps

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        """先锁定 guard 的绑定，再锁定共享总量，首绑与并发采样也遵循同一顺序。"""
        with self._state_lock:
            with self._quota_state.lock if self._quota_state is not None else nullcontext():
                yield

    def _next_queue_seq(self) -> int:
        """在调度锁内分配序号，独立上下文不能占用相同的队列身份。"""
        if self._quota_state is not None:
            self._quota_state.queue_seq += 1
            return self._quota_state.queue_seq
        self._queue_seq += 1
        return self._queue_seq

    def begin_operation(
        self,
        operation: str,
        *,
        symbol: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> AccountControlAttempt:
        """
        在真实对外交互前评估额度并记录一次尝试.

        Parameters
        ----------
        operation : str
            当前调用对应的操作键。
        symbol : str | None, optional
            本次调用关联的交易标的代码。
        metadata : Mapping[str, object] | None, optional
            需要随事件记录的附加元数据。

        Returns
        -------
        AccountControlAttempt
            已放行时返回的尝试对象；若控制未启用则返回空尝试对象。

        Raises
        ------
        AccountControlBlockedError
            当额度规则要求阻断时抛出。
        """
        if not self._enabled():
            return AccountControlAttempt(None, None)
        cleanup_guard_allowed(self, operation, symbol)
        assert self.account_id is not None
        assert self.execution_id is not None
        assert self.policy is not None
        with self._locked_state():
            queue_seq = self._next_queue_seq()
            operation_policy = self.policy.operations.get(operation, AccountControlOperationPolicy())
            queued = _QueuedRequest(
                seq=queue_seq,
                priority=operation_policy.priority,
                operation=operation,
                symbol=symbol,
                entered_at_ms=int(self._wait_clock.time() * 1000),
            )
            self._wait_queue.append(queued)

        metadata = _business_metadata(metadata)
        waited_ms = 0
        wait_hits: list[str] = []
        wait_details: dict[tuple[str, str, str | None, str], AccountControlHit] = {}
        try:
            while True:
                # 只有 policy 优先级最高且同级最早的请求可以评估并预占额度。
                with self._locked_state():
                    head = min(self._wait_queue, key=lambda item: (item.priority, item.seq))
                    if head != queued:
                        wait_ms = self._wait_poll_interval_ms
                        self._monitor_waits[queued.seq] = {"kind": "queue", "hits": []}
                    else:
                        resolved_attempt = self._resolve_operation_attempt(operation=operation, symbol=symbol)
                        scheduling_metadata: dict[str, object] = {}
                        groups = list(self._get_operation_group_keys(operation))
                        if waited_ms or groups or queued.priority != 100:
                            scheduling_metadata = {
                                "waited_ms": waited_ms,
                                "priority": queued.priority,
                                "groups": groups,
                                "rules": wait_hits,
                            }
                        if wait_details:
                            scheduling_metadata["account_control_wait_hits"] = [
                                detail.to_metadata() for detail in wait_details.values()
                            ]
                        event_metadata = _merge_metadata(metadata, scheduling_metadata)
                        if resolved_attempt.decision.kind == "block":
                            self._wait_queue.remove(queued)
                            self._record_blocked_attempt(
                                operation=operation,
                                symbol=symbol,
                                metadata=event_metadata,
                                resolved_attempt=resolved_attempt,
                            )
                        if resolved_attempt.decision.kind == "allow":
                            self._thread_state.last_waited_ms = waited_ms
                            self._thread_state.last_wait_hits = tuple(wait_hits)
                            if waited_ms:
                                logger.debug(
                                    "账户控制放行：{}，累计等待 {} 毫秒，等待原因：{}",
                                    operation_label(operation),
                                    waited_ms,
                                    [detail.rule_label for detail in wait_details.values()],
                                )
                            self._wait_queue.remove(queued)
                            return self._record_allowed_attempt(
                                operation=operation,
                                symbol=symbol,
                                metadata=event_metadata,
                                resolved_attempt=resolved_attempt,
                            )
                        hit = resolved_attempt.decision
                        self._monitor_waits[queued.seq] = {
                            "kind": "quota",
                            "estimated_wait_ms": hit.wait_ms,
                            "hits": [] if hit.details is None else [hit.details.to_metadata()],
                        }
                        if hit.details is not None and hit.details.identity not in wait_details:
                            wait_details[hit.details.identity] = hit.details
                            logger.debug(hit.details.message())
                        if hit.hit_scope and hit.hit_rule:
                            label = f"{hit.hit_scope}.{hit.hit_rule}"
                            if label not in wait_hits:
                                wait_hits.append(label)
                        wait_ms = min(hit.wait_ms, self._wait_poll_interval_ms)
                self._wait_for_quota(wait_ms, symbol, operation)
                waited_ms += wait_ms
        finally:
            with self._locked_state():
                self._monitor_waits.pop(queued.seq, None)
                if queued in self._wait_queue:
                    self._wait_queue.remove(queued)

    def monitor_snapshot(self) -> dict[str, object]:
        """复制只读监控输入；展示计算不占用交易评估锁。"""
        from copy import copy

        from axile.executor.account_control.monitor import render_guard_snapshot

        with self._locked_state():
            self._monitor_seq += 1
            frozen = copy(self)
            frozen._account_counters = dict(self._account_counters)
            frozen._symbol_counters = dict(self._symbol_counters)
            frozen._group_counters = dict(self._group_counters)
            frozen._recent_account_allowed_at = dict(self._recent_account_allowed_at)
            frozen._recent_symbol_allowed_at = dict(self._recent_symbol_allowed_at)
            frozen._recent_group_allowed_at = dict(self._recent_group_allowed_at)
            frozen._wait_queue = list(self._wait_queue)
            frozen._monitor_waits = dict(self._monitor_waits)
            frozen._events = list(self._events[-100:])
            frozen._quota_warnings = dict(self._quota_warnings)
            frozen._quota_state = None if self._quota_state is None else self._quota_state.snapshot()
        return render_guard_snapshot(frozen)

    def current_waited_ms(self) -> int:
        """返回当前线程最近一次获准请求的累计等待毫秒数。"""
        return int(getattr(self._thread_state, "last_waited_ms", 0))

    def current_wait_hits(self) -> tuple[str, ...]:
        """返回当前线程最近一次获准请求命中的规则。"""
        return tuple(getattr(self._thread_state, "last_wait_hits", ()))

    def update_event_outcome(
        self,
        event_index: int,
        *,
        outcome: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """
        更新某次已记录事件的最终结果.

        Parameters
        ----------
        event_index : int
            需要更新的事件索引。
        outcome : str
            事件最终结果。
        metadata : Mapping[str, object] | None, optional
            需要合并到原事件中的附加元数据。
        """
        active = list(getattr(self._thread_state, "active_attempts", []))
        self._thread_state.active_attempts = [index for index in active if index != event_index]
        with self._locked_state():
            event = self._events[event_index]
            # 同步返回可能晚于异步拒绝，不能把已知拒绝覆盖成“已提交”。
            if event.outcome in {"broker_rejected", "broker_sync_rejected"}:
                outcome = event.outcome
            updated = event.model_copy(
                update={
                    "outcome": outcome,
                    "metadata": _merge_metadata(event.metadata, _business_metadata(metadata)),
                }
            )
            self._events[event_index] = updated
        if self.monitor_outcome_sink is not None:
            try:
                self.monitor_outcome_sink(updated)
            except Exception:
                logger.exception("流控结果监控回填失败")

    def active_attempt_indices(self) -> tuple[int, ...]:
        """取得当前线程受控调用身份，供渠道在真实发送前关联回调。"""
        return tuple(getattr(self._thread_state, "active_attempts", ()))

    def flush_records(self) -> tuple[list[AccountControlCounterDeltaWrite], list[AccountControlEventWrite]]:
        """
        导出当前 execution 产生的计数增量与事件事实.

        Returns
        -------
        tuple[list[AccountControlCounterDeltaWrite], list[AccountControlEventWrite]]
            待刷盘的计数器增量列表与事件列表。
        """
        if not self._enabled():
            return [], []
        assert self.account_id is not None
        assert self.execution_id is not None

        with self._locked_state():
            counter_deltas = self._build_counter_deltas()
            return counter_deltas, list(self._events)

    def _enabled(self) -> bool:
        return self.account_id is not None and self.execution_id is not None and self.policy is not None

    def _current_context(self) -> AccountControlEvaluationContext:
        assert self.policy is not None
        tz = ZoneInfo(self.policy.timezone)
        if self._clock is None:
            localized = datetime.fromtimestamp(self._wait_clock.time(), tz)
        else:
            current = self._clock()
            localized = current.replace(tzinfo=tz) if current.tzinfo is None else current.astimezone(tz)
        return AccountControlEvaluationContext(
            local_now=localized,
            occurred_at_ms=int(localized.timestamp() * 1000),
            window=resolve_control_window(self.policy.timezone, now=localized),
        )

    def _next_event_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    def _build_event_uid(
        self,
        seq: int,
        operation: str,
        symbol: str | None,
        decision: AccountControlDecision,
    ) -> str:
        return f"{self.execution_id}:{self.instance_id}:{seq}:{operation}:{symbol or '-'}:{decision.value}"

    def _resolve_operation_attempt(
        self,
        *,
        operation: str,
        symbol: str | None,
    ) -> AccountControlResolvedAttempt:
        """返回当前时刻的一轮额度评估结果，不在此处执行等待。"""
        context = self._current_context()
        # 计数历史缺口只用于审计提示，操作仍按已知计数和现有额度规则评估。
        decision = self._evaluate_enforcement(
            operation=operation,
            symbol=symbol,
            context=context,
        )
        return AccountControlResolvedAttempt(context=context, decision=decision)

    def _record_blocked_attempt(
        self,
        *,
        operation: str,
        symbol: str | None,
        metadata: Mapping[str, object] | None,
        resolved_attempt: AccountControlResolvedAttempt,
    ) -> None:
        decision = resolved_attempt.decision
        assert decision.kind == "block"
        assert decision.message is not None
        assert decision.outcome is not None

        self._append_operation_event(
            operation=operation,
            symbol=symbol,
            metadata=_merge_metadata(
                self._quota_warning_metadata(operation, resolved_attempt.context, metadata),
                {} if decision.details is None else {"account_control_hit": decision.details.to_metadata()},
            ),
            control_date=resolved_attempt.context.window.control_date,
            decision=AccountControlDecision.BLOCKED,
            counted=False,
            outcome=decision.outcome,
            occurred_at_ms=resolved_attempt.context.occurred_at_ms,
        )
        raise AccountControlBlockedError(
            decision.message,
            details=decision.details,
            account_id=self.account_id,
            execution_id=self.execution_id,
            channel=self.channel,
            operation=operation,
            symbol=symbol,
        )

    def _record_allowed_attempt(
        self,
        *,
        operation: str,
        symbol: str | None,
        metadata: Mapping[str, object] | None,
        resolved_attempt: AccountControlResolvedAttempt,
    ) -> AccountControlAttempt:
        track_symbol_scope = self._should_track_symbol_scope(operation=operation, symbol=symbol)
        self._apply_allowed_side_effects(
            operation=operation,
            symbol=symbol,
            window=resolved_attempt.context.window,
            occurred_at_ms=resolved_attempt.context.occurred_at_ms,
            track_symbol_scope=track_symbol_scope,
        )
        event_index = self._append_operation_event(
            operation=operation,
            symbol=symbol,
            metadata=self._quota_warning_metadata(operation, resolved_attempt.context, metadata),
            control_date=resolved_attempt.context.window.control_date,
            decision=AccountControlDecision.ALLOWED,
            counted=True,
            outcome="pending",
            occurred_at_ms=resolved_attempt.context.occurred_at_ms,
        )
        active = list(getattr(self._thread_state, "active_attempts", []))
        active.append(event_index)
        self._thread_state.active_attempts = active
        return AccountControlAttempt(self, event_index)

    def _quota_warning_metadata(
        self,
        operation: str,
        context: AccountControlEvaluationContext,
        metadata: Mapping[str, object] | None,
    ) -> Mapping[str, object] | None:
        """在计数提交后冻结预警；不依赖轮询时刻或异步柜台结果。"""
        if self.policy is None or operation not in {"place_order", "cancel_order"}:
            return metadata
        policy = self.policy.operations.get(operation)
        if policy is None or policy.daily_warning_percent is None:
            return metadata
        rule = policy.account.per_day
        if rule is None or rule.limit == 0:
            return metadata
        used = self._get_count(
            control_date=context.window.control_date,
            bucket_type=AccountControlBucketType.DAY,
            bucket_start=context.window.day_bucket_start,
            scope_key=operation,
            symbol=None,
            is_group=False,
        )
        threshold = (rule.limit * policy.daily_warning_percent + 99) // 100
        key = f"{context.window.control_date}:{operation}"
        if used < threshold or key in self._quota_warnings:
            return metadata
        assert self.account_id is not None and self.execution_id is not None
        warning = AccountControlQuotaWarning(
            account_id=self.account_id,
            execution_id=self.execution_id,
            operation="place_order" if operation == "place_order" else "cancel_order",
            control_date=context.window.control_date,
            timezone=self.policy.timezone,
            occurred_at_ms=context.occurred_at_ms,
            used=used,
            limit=rule.limit,
            warning_percent=policy.daily_warning_percent,
            threshold_count=threshold,
            remaining=max(0, rule.limit - used),
        )
        self._quota_warnings[key] = warning
        return _merge_metadata(metadata, {"quota_warning": warning.model_dump(mode="json")})

    def _should_track_symbol_scope(self, *, operation: str, symbol: str | None) -> bool:
        if symbol is None:
            return False
        return self._get_effective_symbol_scope(operation=operation, symbol=symbol) is not None

    def _apply_allowed_side_effects(
        self,
        *,
        operation: str,
        symbol: str | None,
        window: AccountControlWindow,
        occurred_at_ms: int,
        track_symbol_scope: bool,
    ) -> None:
        self._increment_operation_counters(
            operation=operation,
            symbol=symbol,
            window=window,
            track_symbol_scope=track_symbol_scope,
        )
        self._record_allowed_timestamps(
            operation=operation,
            symbol=symbol,
            occurred_at_ms=occurred_at_ms,
            track_symbol_scope=track_symbol_scope,
        )
        self._record_group_allowance(operation=operation, window=window, occurred_at_ms=occurred_at_ms)

    def _record_allowed_timestamps(
        self,
        *,
        operation: str,
        symbol: str | None,
        occurred_at_ms: int,
        track_symbol_scope: bool,
    ) -> None:
        self._set_last_allowed_at_ms(operation=operation, occurred_at_ms=occurred_at_ms, symbol=None)
        if track_symbol_scope and symbol is not None:
            self._set_last_allowed_at_ms(operation=operation, occurred_at_ms=occurred_at_ms, symbol=symbol)

    def _record_group_allowance(
        self,
        *,
        operation: str,
        window: AccountControlWindow,
        occurred_at_ms: int,
    ) -> None:
        for group_key in self._get_operation_group_keys(operation):
            self._increment_group_counters(group_key=group_key, window=window)
            self._set_group_last_allowed_at_ms(group_key=group_key, occurred_at_ms=occurred_at_ms)

    def _append_operation_event(
        self,
        *,
        operation: str,
        symbol: str | None,
        metadata: Mapping[str, object] | None,
        control_date: str,
        decision: AccountControlDecision,
        counted: bool,
        outcome: str,
        occurred_at_ms: int,
    ) -> int:
        assert self.account_id is not None
        assert self.execution_id is not None
        seq = self._next_event_seq()
        self._events.append(
            AccountControlEventWrite(
                account_id=self.account_id,
                control_date=control_date,
                execution_id=self.execution_id,
                seq=seq,
                context_kind=self.context_kind,
                source=controlled_source(self, self.source),
                channel=self.channel,
                operation=operation,
                symbol=symbol,
                metadata=_normalize_metadata(metadata),
                decision=decision,
                counted=counted,
                outcome=outcome,
                event_uid=self._build_event_uid(seq, operation, symbol, decision),
                occurred_at_ms=occurred_at_ms,
            )
        )
        return len(self._events) - 1

    def _build_counter_deltas(self) -> list[AccountControlCounterDeltaWrite]:
        return [
            *self._build_account_counter_deltas(),
            *self._build_symbol_counter_deltas(),
        ]

    def _build_account_counter_deltas(self) -> list[AccountControlCounterDeltaWrite]:
        assert self.account_id is not None
        assert self.execution_id is not None
        counter_deltas: list[AccountControlCounterDeltaWrite] = []
        for key, source in sorted(self._account_counter_sources):
            control_date, bucket_type, bucket_start, operation = key
            delta_count = self._account_counter_sources[key, source]
            counter_deltas.append(
                AccountControlCounterDeltaWrite(
                    account_id=self.account_id,
                    execution_id=self.execution_id,
                    context_kind=self.context_kind,
                    source=source,
                    control_date=control_date,
                    bucket_type=bucket_type,
                    bucket_start=bucket_start,
                    scope_type=AccountControlScopeType.ACCOUNT,
                    symbol=None,
                    operation=operation,
                    delta_count=delta_count,
                    delta_uid=(
                        f"{self.execution_id}:{self.instance_id}:{control_date}:{bucket_type.value}:{bucket_start}:"
                        f"{AccountControlScopeType.ACCOUNT.value}:-:{operation}:{source.value}"
                    ),
                )
            )
        return counter_deltas

    def _build_symbol_counter_deltas(self) -> list[AccountControlCounterDeltaWrite]:
        assert self.account_id is not None
        assert self.execution_id is not None
        counter_deltas: list[AccountControlCounterDeltaWrite] = []
        for key, source in sorted(self._symbol_counter_sources):
            control_date, symbol, bucket_type, bucket_start, operation = key
            delta_count = self._symbol_counter_sources[key, source]
            counter_deltas.append(
                AccountControlCounterDeltaWrite(
                    account_id=self.account_id,
                    execution_id=self.execution_id,
                    context_kind=self.context_kind,
                    source=source,
                    control_date=control_date,
                    bucket_type=bucket_type,
                    bucket_start=bucket_start,
                    scope_type=AccountControlScopeType.SYMBOL,
                    symbol=symbol,
                    operation=operation,
                    delta_count=delta_count,
                    delta_uid=(
                        f"{self.execution_id}:{self.instance_id}:{control_date}:{bucket_type.value}:{bucket_start}:"
                        f"{AccountControlScopeType.SYMBOL.value}:{symbol}:{operation}:{source.value}"
                    ),
                )
            )
        return counter_deltas

    def _evaluate_enforcement(
        self,
        *,
        operation: str,
        symbol: str | None,
        context: AccountControlEvaluationContext,
    ) -> AccountControlEnforcementDecision:
        assert self.policy is not None
        operation_policy = self.policy.operations.get(operation, AccountControlOperationPolicy())
        decisions = [
            self._check_scope_limits(
                operation=operation,
                scope_policy=operation_policy.account,
                scope_key=operation,
                context=context,
                symbol=None,
                message_symbol=symbol,
                is_group=False,
            )
        ]

        effective_symbol_scope = self._get_effective_symbol_scope(operation=operation, symbol=symbol)
        if symbol is not None and effective_symbol_scope is not None:
            decisions.append(
                self._check_scope_limits(
                    operation=operation,
                    scope_policy=effective_symbol_scope,
                    scope_key=operation,
                    context=context,
                    symbol=symbol,
                    message_symbol=symbol,
                    is_group=False,
                )
            )

        for group_key in self._get_operation_group_keys(operation):
            group_policy = self.policy.groups.get(group_key)
            if group_policy is None:
                continue
            decisions.append(
                self._check_scope_limits(
                    operation=operation,
                    scope_policy=group_policy,
                    scope_key=group_key,
                    context=context,
                    symbol=None,
                    message_symbol=symbol,
                    is_group=True,
                )
            )

        return self._combine_decisions(decisions)

    def _combine_decisions(
        self,
        decisions: list[AccountControlEnforcementDecision],
    ) -> AccountControlEnforcementDecision:
        max_wait: AccountControlEnforcementDecision | None = None
        for decision in decisions:
            if decision.kind == "block":
                return decision
            if decision.kind == "wait":
                if max_wait is None or decision.wait_ms > max_wait.wait_ms:
                    max_wait = decision
        if max_wait is not None:
            return max_wait
        return AccountControlEnforcementDecision.allow()

    def _get_effective_symbol_scope(
        self,
        *,
        operation: str,
        symbol: str | None,
    ) -> AccountControlScopePolicy | None:
        if symbol is None or self.policy is None:
            return None
        operation_policy = self.policy.operations.get(operation)
        return None if operation_policy is None else _copy_scope_policy(operation_policy.symbol)

    def _check_scope_limits(
        self,
        *,
        operation: str,
        scope_policy: AccountControlScopePolicy,
        scope_key: str,
        context: AccountControlEvaluationContext,
        symbol: str | None,
        message_symbol: str | None,
        is_group: bool,
    ) -> AccountControlEnforcementDecision:
        decisions = []
        for rule_kind in ("per_day", "per_minute", "min_interval_ms"):
            rule = getattr(scope_policy, rule_kind)
            if rule is None:
                continue
            current, wait_ms = self._rule_measurement(
                rule_kind=rule_kind,
                limit=rule.limit,
                context=context,
                scope_key=scope_key,
                symbol=symbol,
                is_group=is_group,
            )
            if current is None:
                continue
            action = "block" if rule.on_trigger == AccountControlTriggerBehavior.BLOCK else "wait"
            # 保持分钟零额度的既有直接阻断行为。
            if rule_kind == "per_minute" and rule.limit == 0:
                action = "block"
            if action == "wait" and wait_ms <= 0:
                continue
            detail = self._build_hit(
                rule_kind=rule_kind,
                scope_key=scope_key,
                symbol=symbol,
                is_group=is_group,
                operation=operation,
                current=current,
                limit=rule.limit,
                action=action,
                context=context,
                message_symbol=message_symbol,
                wait_ms=wait_ms,
            )
            decision = AccountControlEnforcementDecision(
                kind=action,
                wait_ms=wait_ms if action == "wait" else 0,
                message=detail.message() if action == "block" else None,
                outcome="policy_blocked" if action == "block" else None,
                hit_scope=scope_key,
                hit_rule=rule_kind,
                details=detail,
            )
            if rule_kind == "per_day" or action == "block":
                return decision
            decisions.append(decision)
        return self._combine_decisions(decisions)

    def _build_hit(
        self,
        *,
        rule_kind: str,
        scope_key: str,
        symbol: str | None,
        is_group: bool,
        operation: str,
        current: int,
        limit: int,
        action: str,
        context: AccountControlEvaluationContext,
        message_symbol: str | None,
        wait_ms: int,
    ) -> AccountControlHit:
        """从同一轮评估生成不可变快照。"""
        assert self.policy is not None
        interval = rule_kind == "min_interval_ms"
        window_start = None
        window_end = None
        if not interval:
            start = context.local_now.replace(second=0, microsecond=0)
            if rule_kind == "per_day":
                start = start.replace(hour=0, minute=0)
            end = start + (timedelta(days=1) if rule_kind == "per_day" else timedelta(minutes=1))
            window_start, window_end = start.isoformat(), end.isoformat()
        return AccountControlHit(
            rule_kind=rule_kind,
            scope_type="group" if is_group else ("symbol" if symbol is not None else "account"),
            scope_key=scope_key,
            scope_symbol=symbol,
            operation=operation,
            current_value=current,
            limit=limit,
            unit="milliseconds" if interval else "count",
            action=action,
            evaluated_at=context.local_now.isoformat(),
            account_id=self.account_id,
            symbol=message_symbol,
            control_date=None if interval else context.window.control_date,
            timezone=self.policy.timezone,
            window_start=window_start,
            window_end=window_end,
            retry_after_ms=None if limit == 0 else wait_ms,
        )

    def _rule_measurement(
        self,
        *,
        rule_kind: str,
        limit: int,
        context: AccountControlEvaluationContext,
        scope_key: str,
        symbol: str | None,
        is_group: bool,
    ) -> tuple[int | None, int]:
        """在评估锁内读取触发值，格式化阶段不再读取可变计数。"""
        if rule_kind == "min_interval_ms":
            last = self._get_last_allowed_at_ms(scope_key=scope_key, symbol=symbol, is_group=is_group)
            if last is None:
                return None, 0
            elapsed = context.occurred_at_ms - last
            return (elapsed, limit - elapsed) if elapsed < limit else (None, 0)
        daily = rule_kind == "per_day"
        count = self._get_count(
            control_date=context.window.control_date,
            bucket_type=AccountControlBucketType.DAY if daily else AccountControlBucketType.MINUTE,
            bucket_start=context.window.day_bucket_start if daily else context.window.minute_bucket_start,
            scope_key=scope_key,
            symbol=symbol,
            is_group=is_group,
        )
        if count < limit:
            return None, 0
        wait_ms = (
            self._get_wait_ms_until_next_day(context.local_now)
            if daily
            else self._get_wait_ms_until_next_minute(context.local_now)
        )
        return count, wait_ms

    def _get_wait_ms_until_next_minute(self, local_now: datetime) -> int:
        next_minute = local_now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return max(int((next_minute - local_now).total_seconds() * 1000), 0)

    def _get_wait_ms_until_next_day(self, local_now: datetime) -> int:
        next_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return max(int((next_day - local_now).total_seconds() * 1000), 0)

    def _wait_for_quota(self, wait_ms: int, symbol: str | None, operation: str = "") -> None:
        remaining_ms = wait_ms
        while remaining_ms > 0:
            if not cleanup_guard_allowed(self, operation, symbol):
                self._run_termination_checkpoint(symbol)
            sleep_ms = min(remaining_ms, self._wait_poll_interval_ms)
            self._sleep(sleep_ms / 1000)
            remaining_ms -= sleep_ms
        if not cleanup_guard_allowed(self, operation, symbol):
            self._run_termination_checkpoint(symbol)

    def _run_termination_checkpoint(self, symbol: str | None) -> None:
        if self._termination_checkpoint is None:
            return
        self._termination_checkpoint(symbol)

    def _get_count(
        self,
        *,
        control_date: str,
        bucket_type: AccountControlBucketType,
        bucket_start: str,
        scope_key: str,
        symbol: str | None,
        is_group: bool,
    ) -> int:
        if is_group:
            return self._get_group_count(
                control_date=control_date,
                bucket_type=bucket_type,
                bucket_start=bucket_start,
                group_key=scope_key,
            )

        if self._quota_state is not None:
            if symbol is None:
                return self._quota_state.account_counts.get((control_date, bucket_type, bucket_start, scope_key), 0)
            return self._quota_state.symbol_counts.get((control_date, symbol, bucket_type, bucket_start, scope_key), 0)

        baseline = 0
        if control_date == self._baseline_control_date:
            baseline = self.baseline.get_count(
                bucket_type=bucket_type,
                bucket_start=bucket_start,
                operation=scope_key,
                symbol=symbol,
            )
        if symbol is None:
            return baseline + self._account_counters.get((control_date, bucket_type, bucket_start, scope_key), 0)
        return baseline + self._symbol_counters.get((control_date, symbol, bucket_type, bucket_start, scope_key), 0)

    def _get_group_count(
        self,
        *,
        control_date: str,
        bucket_type: AccountControlBucketType,
        bucket_start: str,
        group_key: str,
    ) -> int:
        if self._quota_state is not None:
            return sum(
                self._quota_state.account_counts.get((control_date, bucket_type, bucket_start, operation), 0)
                for operation in self._get_group_operation_keys(group_key)
            )
        baseline = 0
        if control_date == self._baseline_control_date:
            for operation_key in self._get_group_operation_keys(group_key):
                baseline += self.baseline.get_count(
                    bucket_type=bucket_type,
                    bucket_start=bucket_start,
                    operation=operation_key,
                )
        return baseline + self._group_counters.get((control_date, bucket_type, bucket_start, group_key), 0)

    def _increment_operation_counters(
        self,
        *,
        operation: str,
        symbol: str | None,
        window: AccountControlWindow,
        track_symbol_scope: bool,
    ) -> None:
        self._increment_account_counter(
            window.control_date, AccountControlBucketType.MINUTE, window.minute_bucket_start, operation
        )
        self._increment_account_counter(
            window.control_date, AccountControlBucketType.DAY, window.day_bucket_start, operation
        )
        if track_symbol_scope and symbol is not None:
            self._increment_symbol_counter(
                window.control_date,
                symbol,
                AccountControlBucketType.MINUTE,
                window.minute_bucket_start,
                operation,
            )
            self._increment_symbol_counter(
                window.control_date,
                symbol,
                AccountControlBucketType.DAY,
                window.day_bucket_start,
                operation,
            )

    def _increment_group_counters(
        self,
        *,
        group_key: str,
        window: AccountControlWindow,
    ) -> None:
        self._increment_group_counter(
            window.control_date, AccountControlBucketType.MINUTE, window.minute_bucket_start, group_key
        )
        self._increment_group_counter(
            window.control_date, AccountControlBucketType.DAY, window.day_bucket_start, group_key
        )

    def _increment_account_counter(
        self,
        control_date: str,
        bucket_type: AccountControlBucketType,
        bucket_start: str,
        operation: str,
    ) -> None:
        key = (control_date, bucket_type, bucket_start, operation)
        self._account_counters[key] = self._account_counters.get(key, 0) + 1
        if self._quota_state is not None:
            self._quota_state.account_counts[key] = self._quota_state.account_counts.get(key, 0) + 1
        attribution = key, controlled_source(self, self.source)
        self._account_counter_sources[attribution] = self._account_counter_sources.get(attribution, 0) + 1

    def _increment_symbol_counter(
        self,
        control_date: str,
        symbol: str,
        bucket_type: AccountControlBucketType,
        bucket_start: str,
        operation: str,
    ) -> None:
        key = (control_date, symbol, bucket_type, bucket_start, operation)
        self._symbol_counters[key] = self._symbol_counters.get(key, 0) + 1
        if self._quota_state is not None:
            self._quota_state.symbol_counts[key] = self._quota_state.symbol_counts.get(key, 0) + 1
        attribution = key, controlled_source(self, self.source)
        self._symbol_counter_sources[attribution] = self._symbol_counter_sources.get(attribution, 0) + 1

    def _increment_group_counter(
        self,
        control_date: str,
        bucket_type: AccountControlBucketType,
        bucket_start: str,
        group_key: str,
    ) -> None:
        key = (control_date, bucket_type, bucket_start, group_key)
        self._group_counters[key] = self._group_counters.get(key, 0) + 1

    def _get_last_allowed_at_ms(
        self,
        *,
        scope_key: str,
        symbol: str | None,
        is_group: bool,
    ) -> int | None:
        if is_group:
            return self._get_group_last_allowed_at_ms(scope_key)
        if symbol is None:
            return self._recent_account_allowed_at.get((scope_key,))
        return self._recent_symbol_allowed_at.get((symbol, scope_key))

    def _set_last_allowed_at_ms(
        self,
        *,
        operation: str,
        occurred_at_ms: int,
        symbol: str | None,
    ) -> None:
        if symbol is None:
            self._recent_account_allowed_at[(operation,)] = occurred_at_ms
            return
        self._recent_symbol_allowed_at[(symbol, operation)] = occurred_at_ms

    def _get_group_last_allowed_at_ms(self, group_key: str) -> int | None:
        values = [self._recent_group_allowed_at.get((group_key,))]
        for operation_key in self._get_group_operation_keys(group_key):
            values.append(self._recent_account_allowed_at.get((operation_key,)))
        filtered = [value for value in values if value is not None]
        if not filtered:
            return None
        return max(filtered)

    def _set_group_last_allowed_at_ms(self, *, group_key: str, occurred_at_ms: int) -> None:
        self._recent_group_allowed_at[(group_key,)] = occurred_at_ms

    def _get_group_operation_keys(self, group_key: str) -> list[str]:
        operation_keys = {
            operation_key
            for operation_key, operation in self._registry.operations.items()
            if group_key in operation.groups
        }
        if self.policy is not None:
            operation_groups = getattr(self.policy, "operation_groups", {})
            operation_keys.update(
                operation_key for operation_key, group_keys in operation_groups.items() if group_key in group_keys
            )
        return sorted(operation_keys)

    def _get_operation_group_keys(self, operation: str) -> tuple[str, ...]:
        registered_operation = self._registry.get_operation(operation)
        group_keys = set() if registered_operation is None else set(registered_operation.groups)
        if self.policy is not None:
            operation_groups = getattr(self.policy, "operation_groups", {})
            group_keys.update(operation_groups.get(operation, ()))
        return tuple(sorted(group_keys))
