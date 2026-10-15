"""证据导出请求、资源预算和冻结数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

from axile.executor.algorithms.utils.clock import clock_now, get_default_clock


class EvidenceProfile(StrEnum):
    """导出材料的身份信息可见范围，两个范围都排除凭据。"""

    SHAREABLE = "shareable"
    INTERNAL = "internal"


class EvidenceError(Exception):
    """带有稳定错误码的导出失败。"""

    def __init__(self, message: str, *, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class EvidenceLimits:
    """单次导出的资源边界，超限整次失败，不截断证据。"""

    executions: int = 100
    rows: int = 100_000
    bytes: int = 64 * 1024 * 1024
    seconds: float = 60
    log_files: int = 32
    log_scan_bytes: int = 64 * 1024 * 1024


@dataclass
class ExportBudget:
    """由异步采集和压缩线程共同遵守的预算与取消信号。"""

    limits: EvidenceLimits = field(default_factory=EvidenceLimits)
    cancelled: Event = field(default_factory=Event)
    started: float = field(default_factory=lambda: get_default_clock().time())
    rows: int = 0
    bytes: int = 0

    def check(self, *, rows: int = 0, size: int = 0) -> None:
        """检查预算；读取和写出数据均有界。"""
        self.rows += rows
        self.bytes += size
        if self.cancelled.is_set():
            raise EvidenceError("证据导出已取消", code="EVIDENCE_CANCELLED", status=408)
        if get_default_clock().time() - self.started > self.limits.seconds:
            raise EvidenceError("证据导出超时，请缩小范围", code="EVIDENCE_TIMEOUT", status=408)
        if self.rows > self.limits.rows or self.bytes > self.limits.bytes:
            raise EvidenceError("证据量超过导出上限，请缩小范围", code="EVIDENCE_TOO_LARGE", status=413)


def parse_time(value: object, legacy_zone: ZoneInfo | None = None) -> datetime | None:
    """将已记录时间转换为 UTC；无时区历史时间采用声明的解释规则。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None and legacy_zone is not None:
            parsed = parsed.replace(tzinfo=legacy_zone)
        # tzinfo=None 时，astimezone 采用服务器本地时区，并按该日期处理夏令时。
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class EvidenceWindow:
    """自然日闭区间对应的 UTC 半开时间窗口。"""

    start_date: date
    end_date: date
    timezone: str = "Asia/Shanghai"
    legacy_timezone: str | None = None
    start: datetime = field(init=False)
    end: datetime = field(init=False)
    legacy_zone: ZoneInfo | None = field(init=False)

    def __post_init__(self) -> None:
        """校验日期和时区，并冻结对应 UTC 边界。"""
        if self.start_date < date.min + timedelta(days=3) or self.end_date > date.max - timedelta(days=3):
            raise EvidenceError("日期超出支持范围", code="EVIDENCE_INVALID_RANGE")
        if self.end_date < self.start_date or (self.end_date - self.start_date).days >= 31:
            raise EvidenceError("日期范围须为 1～31 天", code="EVIDENCE_INVALID_RANGE")
        try:
            zone = ZoneInfo(self.timezone)
            legacy = ZoneInfo(self.legacy_timezone) if self.legacy_timezone else None
        except (ValueError, KeyError) as exc:
            raise EvidenceError("无效的 IANA 时区", code="EVIDENCE_INVALID_TIMEZONE") from exc
        try:
            end_day = self.end_date + timedelta(days=1)
        except OverflowError as exc:
            raise EvidenceError("结束日期超出支持范围", code="EVIDENCE_INVALID_RANGE") from exc
        object.__setattr__(self, "start", datetime.combine(self.start_date, datetime.min.time(), zone).astimezone(UTC))
        object.__setattr__(self, "end", datetime.combine(end_day, datetime.min.time(), zone).astimezone(UTC))
        object.__setattr__(self, "legacy_zone", legacy)

    def contains(self, value: object) -> bool | None:
        """返回记录是否落在窗口内；无法解释的时间返回未知。"""
        parsed = parse_time(value, self.legacy_zone)
        return None if parsed is None else self.start <= parsed < self.end

    def contains_ms(self, value: int) -> bool:
        """按原始 Unix 毫秒时间判断控制事件窗口。"""
        return self.start.timestamp() * 1000 <= value < self.end.timestamp() * 1000

    def describe(self) -> dict[str, Any]:
        """返回可复核的选择口径和历史时间假设。"""
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "timezone": self.timezone,
            "start_inclusive": self.start.isoformat(),
            "end_exclusive": self.end.isoformat(),
            "legacy_timezone": self.legacy_timezone or "server_local_at_export",
            "legacy_timezone_is_assumption": True,
            "basis": "execution_activity",
        }


@dataclass
class ExecutionEvidence:
    """一次执行的持久化事实，允许只有意图或历史结果。"""

    key: str
    execution_id: str | None
    selection_reasons: list[str] = field(default_factory=list)
    intent: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    counters: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EvidenceSnapshot:
    """同一数据库读事务中冻结的全部业务证据。"""

    account: dict[str, Any]
    executions: dict[str, ExecutionEvidence]
    window: EvidenceWindow | None = None
    snapshot_at: str = field(default_factory=lambda: clock_now(tz=UTC).isoformat())
    source_counts: dict[str, int] = field(default_factory=dict)
