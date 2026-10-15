"""同一账户运行时的额度总量与调度队列，独立于各上下文的审计增量。"""

from __future__ import annotations

from _thread import RLock as RLockType
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from threading import RLock

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import AccountControlBucketType
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot

type AccountCounterKey = tuple[str, AccountControlBucketType, str, str]
type SymbolCounterKey = tuple[str, str, AccountControlBucketType, str, str]


@dataclass(frozen=True)
class AccountControlQueuedRequest:
    """同一额度状态内按优先级和入队顺序调度的请求。"""

    seq: int
    priority: int
    operation: str
    symbol: str | None
    entered_at_ms: int


def _merge_max[Key](target: dict[Key, int], values: Mapping[Key, int]) -> None:
    for key, value in values.items():
        target[key] = max(target.get(key, 0), value)


@dataclass
class AccountControlQuotaState:
    """同一进程内账户操作共用的绝对计数，不用于生成落库增量。

    Notes
    -----
    宿主须在 guard 的首个操作前绑定本状态。所有放行在同一锁内检查并预占，
    各 guard 仍只导出自己的审计增量。后续上下文读取的数据库基线可能已经包含
    本状态计入的操作，因此按桶取较大值补齐基线，不能把持久化总量再次相加。
    """

    lock: RLockType = field(default_factory=RLock, repr=False)
    identity: tuple[int, TradeChannel] | None = None
    account_counts: dict[AccountCounterKey, int] = field(default_factory=dict)
    symbol_counts: dict[SymbolCounterKey, int] = field(default_factory=dict)
    account_timestamps: dict[tuple[str], int] = field(default_factory=dict)
    symbol_timestamps: dict[tuple[str, str], int] = field(default_factory=dict)
    group_timestamps: dict[tuple[str], int] = field(default_factory=dict)
    queue_seq: int = 0
    wait_queue: list[AccountControlQueuedRequest] = field(default_factory=list)
    monitor_waits: dict[int, dict[str, object]] = field(default_factory=dict)

    def bind(self, account_id: int, channel: TradeChannel) -> None:
        """拒绝把不同账户或渠道的额度状态混用。"""
        identity = account_id, channel
        if self.identity is not None and self.identity != identity:
            raise ValueError("共享额度状态不能跨账户或渠道复用")
        self.identity = identity

    def merge_baseline(self, control_date: str | None, baseline: AccountControlCounterSnapshot) -> None:
        """只补齐尚未知的持久化总量；重复加载或部分上下文收尾均不重复计数。"""
        if control_date is None:
            return
        _merge_max(self.account_counts, {(control_date, *key): value for key, value in baseline.account_counts.items()})
        _merge_max(self.symbol_counts, {(control_date, *key): value for key, value in baseline.symbol_counts.items()})

    def merge_timestamps(
        self, account: Mapping[tuple[str], int], symbol: Mapping[tuple[str, str], int], group: Mapping[tuple[str], int]
    ) -> None:
        """账户、品种和分组的发送间隔始终使用最近一次真实放行。"""
        _merge_max(self.account_timestamps, account)
        _merge_max(self.symbol_timestamps, symbol)
        _merge_max(self.group_timestamps, group)

    def snapshot(self) -> AccountControlQuotaState:
        """在调用方持锁时复制计数，监控渲染不观察后续放行的半更新状态。"""
        return replace(
            self,
            lock=RLock(),
            account_counts=dict(self.account_counts),
            symbol_counts=dict(self.symbol_counts),
            account_timestamps=dict(self.account_timestamps),
            symbol_timestamps=dict(self.symbol_timestamps),
            group_timestamps=dict(self.group_timestamps),
            wait_queue=list(self.wait_queue),
            monitor_waits=dict(self.monitor_waits),
        )
