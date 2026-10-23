"""账户控制的原生持久化入口，时区窗口与运行时快照留在 Python。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlCounterDeltaWrite,
    AccountControlEventWrite,
)
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.server.native_store import native_operation


def snapshots_from_baseline(raw: dict) -> tuple[AccountControlCounterSnapshot, AccountControlRecentAllowedSnapshot]:
    """将原生聚合结果装入 guard 使用的内存快照。"""
    baseline = AccountControlCounterSnapshot()
    for row in raw["counters"]:
        baseline.add(**{**row, "bucket_type": AccountControlBucketType(row["bucket_type"])})
    recent = AccountControlRecentAllowedSnapshot()
    for row in raw["recent_allowed"]:
        recent.add(**row)
    return baseline, recent


class AccountControlStore:
    """按完整业务阶段调用 Rust，不在 Python 持有数据库会话。"""

    async def load_baseline(
        self, account_id: int, control_date: str, timezone: str, *, active_instance_id: str | None = None
    ) -> tuple[AccountControlCounterSnapshot, AccountControlRecentAllowedSnapshot, bool]:
        """一次加载已聚合计数、最近放行时间和窗口内未收尾风险会话。"""
        start = datetime.fromisoformat(control_date).replace(tzinfo=ZoneInfo(timezone))
        raw = await native_operation(
            "control_load_baseline",
            {
                "account_id": account_id,
                "control_date": control_date,
                "start_ms": int(start.timestamp() * 1000),
                "end_ms": int((start + timedelta(days=1)).timestamp() * 1000),
                "active_instance_id": active_instance_id,
            },
        )
        baseline, recent = snapshots_from_baseline(raw)
        return baseline, recent, raw["incomplete"]

    async def register_session(self, **session: object) -> None:
        """在首次调用 SDK 前登记控制会话。"""
        await native_operation("control_register_session", session)

    async def flush_execution_records(
        self,
        *,
        counter_deltas: list[AccountControlCounterDeltaWrite],
        events: list[AccountControlEventWrite],
        close_session: dict[str, str | int] | None = None,
    ) -> None:
        """原子保存会话收尾与全部事实；随后单独提交额度预警。"""
        if not counter_deltas and not events and close_session is None:
            return
        receipt = await native_operation(
            "control_flush_records",
            {
                "counter_deltas": [row.model_dump(mode="json") for row in counter_deltas],
                "events": [row.model_dump(mode="json") for row in events],
                "close_session": close_session,
                "warning_created_at": clock_now_iso(),
            },
        )
        if receipt["warning_error"]:
            logger.error("额度预警入队失败，已保存的控制计数不受影响: {}", receipt["warning_error"])

    async def update_event_outcomes(self, events: list[dict]) -> None:
        """人工撤单结果按事件身份回填，不创建新事实。"""
        await native_operation(
            "control_update_event_outcomes",
            {"events": [{key: row[key] for key in ("event_uid", "outcome", "metadata")} for row in events]},
        )
