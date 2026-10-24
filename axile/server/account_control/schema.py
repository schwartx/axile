"""REST 与 SSE 共用的流控监控公开数据契约。"""

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

from axile.server.order_submissions import OrderSubmissionSummary
from axile.server.submission_monitor import (
    SubmissionMetric,
    SubmissionMonitorPublic,
    SubmissionMonitorSettings,
    SubmissionWarning,
)


@dataclass(slots=True, kw_only=True)
class ControlRulePublic:
    """规则的实际用量；未知值与无限制分别由 value 和 limit 表达。"""

    id: str
    operation: str
    scope: Literal["account", "symbol", "group"]
    symbol: str | None
    rule: Literal["per_day", "per_minute", "min_interval_ms"]
    value: int | None
    limit: int | None
    remaining: int | None
    estimated_wait_ms: int
    on_trigger: str | None
    label: str | None = None


@dataclass(slots=True, kw_only=True)
class ControlWaitingPublic:
    """当前实例的等待请求，时间与命中原因均为估计快照。"""

    id: int
    operation: str
    symbol: str | None
    priority: int
    entered_at_ms: int
    waited_ms: int
    kind: Literal["queue", "quota"]
    estimated_wait_ms: int | None = None
    hits: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class ControlRecordPublic:
    """本地流控尝试与其已知结果，不代表成交或最终受理。"""

    event_uid: str
    occurred_at_ms: int
    operation: str
    symbol: str | None
    decision: str
    outcome: str
    metadata: dict[str, Any]


@dataclass(slots=True, kw_only=True)
class AccountControlMonitorPublic:
    """统一账户监控快照，运行状态、时效和完整性相互独立。"""

    account_id: int
    execution_id: str | None
    instance_id: str | None
    seq: int
    status: Literal["running", "idle", "disabled", "unknown", "incomplete"]
    sampled_at_ms: int | None
    timezone: str | None
    control_date: str | None
    minute_start: str | None
    stale: bool
    incomplete: bool
    history_incomplete: bool = False
    snapshot_stale: bool = False
    counter_integrity: Literal["known", "unknown"] = "known"
    risk_operations_blocked: bool = False
    incomplete_instances: list[dict[str, Any]] = field(default_factory=list)
    incomplete_instances_total: int = 0
    counter_recovery: str | None = None
    pending_policy: bool
    actual_policy: dict[str, Any] | None
    actual_fingerprint: str | None
    saved_policy: dict[str, Any]
    saved_fingerprint: str
    rules: list[ControlRulePublic]
    metrics: list[ControlRulePublic]
    waiting: list[ControlWaitingPublic]
    waiting_total: int
    records: list[ControlRecordPublic]
    order_submissions: OrderSubmissionSummary | None = None
    submission_monitor: SubmissionMonitorPublic | None = None


def _row(model: Any, values: dict[str, Any]) -> Any:
    return model(**{item.name: values[item.name] for item in fields(model) if item.name in values})


def public_frame(values: dict[str, Any]) -> dict[str, Any]:
    """按展示字段构造快照；内部审计与预警字段留在运行时。"""
    frame = _row(AccountControlMonitorPublic, values)
    frame.rules = [_row(ControlRulePublic, row) for row in frame.rules]
    frame.metrics = [_row(ControlRulePublic, row) for row in frame.metrics]
    frame.waiting = [_row(ControlWaitingPublic, row) for row in frame.waiting]
    frame.records = [_row(ControlRecordPublic, row) for row in frame.records]
    if frame.order_submissions is not None:
        frame.order_submissions = _row(OrderSubmissionSummary, frame.order_submissions)
    if frame.submission_monitor is not None:
        monitor = _row(SubmissionMonitorPublic, frame.submission_monitor)
        monitor.settings = _row(SubmissionMonitorSettings, monitor.settings)
        monitor.metrics = [_row(SubmissionMetric, row) for row in monitor.metrics]
        monitor.warnings = [_row(SubmissionWarning, row) for row in monitor.warnings]
        frame.submission_monitor = monitor
    return asdict(frame)
