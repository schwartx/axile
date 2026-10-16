"""
提供账户控制事件与计数器的持久化访问层.

Notes
-----
该模块负责把账户控制运行时产出的 write 模型刷入数据库，并在执行开始前读取
账户控制所需的计数基线与最近放行时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, func, select

from axile.executor.account_control.context import control_context_kind
from axile.executor.account_control.models import (
    AccountControlContextKind,
    AccountControlCounterDeltaWrite,
    AccountControlDecision,
    AccountControlEventWrite,
)
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)
from axile.server.account_control.warnings import enqueue_recorded_warnings
from axile.server.db.models import AccountControlCounterDelta, AccountControlEvent
from axile.server.db.models.account_control_session import AccountControlSession


class AccountControlStore:
    """
    提供账户控制的 SQLite 持久化入口.

    Attributes
    ----------
    session : AsyncSession
        当前使用的异步数据库会话。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _serialize_event_write(self, event: AccountControlEventWrite) -> dict[str, object]:
        """
        将执行期事件写模型转换为数据库插入载荷.

        Parameters
        ----------
        event : AccountControlEventWrite
            待写入数据库的事件对象。

        Returns
        -------
        dict[str, object]
            可直接写入 ``AccountControlEvent`` 表的字段映射。
        """
        payload = event.model_dump(mode="json")
        payload["metadata_"] = payload.pop("metadata")
        return payload

    async def load_daily_counters(self, account_id: int, control_date: str) -> AccountControlCounterSnapshot:
        """
        读取指定日期的计数增量并聚合为内存基线.

        Parameters
        ----------
        account_id : int
            账户 ID。
        control_date : str
            控制日期，格式为 ``YYYY-MM-DD``。

        Returns
        -------
        AccountControlCounterSnapshot
            聚合后的账户与标的级计数快照。
        """
        rows = (
            await self.session.execute(
                select(AccountControlCounterDelta).where(
                    AccountControlCounterDelta.account_id == account_id,
                    AccountControlCounterDelta.control_date == control_date,
                )
            )
        ).scalars()

        snapshot = AccountControlCounterSnapshot()
        for row in rows:
            snapshot.add(
                bucket_type=row.bucket_type,
                bucket_start=row.bucket_start,
                operation=row.operation,
                delta_count=row.delta_count,
                symbol=row.symbol if row.scope_type.value == "symbol" else None,
            )
        return snapshot

    async def has_incomplete_risk_history(
        self, account_id: int, control_date: str, timezone: str, *, active_instance_id: str | None = None
    ) -> bool:
        """检查窗口重叠的未收尾交易会话；只读账户操作不推断丢失报单。"""
        start = datetime.fromisoformat(control_date).replace(tzinfo=ZoneInfo(timezone))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
        rows = (
            await self.session.execute(
                select(AccountControlSession).where(
                    AccountControlSession.account_id == account_id,
                    col(AccountControlSession.status).in_(["active", "incomplete"]),
                    AccountControlSession.started_at_ms < end_ms,
                )
            )
        ).scalars()
        for row in rows:
            if row.finished_at_ms is not None and row.finished_at_ms < start_ms:
                continue
            if row.status == "active" and row.instance_id == active_instance_id:
                continue
            if control_context_kind(row.context_kind, row.execution_id) == AccountControlContextKind.ACCOUNT_OPERATION:
                # 仅确认无新风险操作的来源可排除；人工委托等仍保守阻断。
                if row.source in {"account_assets", "connection_prepare", "instrument_catalog"}:
                    continue
            return True
        return False

    async def load_recent_allowed_timestamps(self, account_id: int) -> AccountControlRecentAllowedSnapshot:
        """
        读取账户历史上最近一次已放行且计数的时间.

        Parameters
        ----------
        account_id : int
            账户 ID。

        Returns
        -------
        AccountControlRecentAllowedSnapshot
            账户级与标的级的最近放行时间快照。
        """
        counted_column = cast(Any, AccountControlEvent.counted)
        symbol_column = cast(Any, AccountControlEvent.symbol)
        conditions = (
            AccountControlEvent.account_id == account_id,
            AccountControlEvent.decision == AccountControlDecision.ALLOWED,
            counted_column.is_(True),
        )

        account_result = await self.session.execute(
            select(
                AccountControlEvent.operation,
                func.max(AccountControlEvent.occurred_at_ms).label("occurred_at_ms"),
            )
            .where(*conditions)
            .group_by(AccountControlEvent.operation)
        )
        symbol_result = await self.session.execute(
            select(
                AccountControlEvent.symbol,
                AccountControlEvent.operation,
                func.max(AccountControlEvent.occurred_at_ms).label("occurred_at_ms"),
            )
            .where(*conditions, symbol_column.is_not(None))
            .group_by(AccountControlEvent.symbol, AccountControlEvent.operation)
        )
        account_rows = account_result.all() if hasattr(account_result, "all") else []
        symbol_rows = symbol_result.all() if hasattr(symbol_result, "all") else []

        snapshot = AccountControlRecentAllowedSnapshot()
        for operation, occurred_at_ms in account_rows:
            snapshot.add(operation=operation, occurred_at_ms=occurred_at_ms)
        for symbol, operation, occurred_at_ms in symbol_rows:
            snapshot.add(symbol=symbol, operation=operation, occurred_at_ms=occurred_at_ms)
        return snapshot

    async def flush_execution_records(
        self,
        *,
        counter_deltas: list[AccountControlCounterDeltaWrite],
        events: list[AccountControlEventWrite],
    ) -> None:
        """
        批量刷入本次执行产生的 delta 与事件.

        Parameters
        ----------
        counter_deltas : list[AccountControlCounterDeltaWrite]
            需要写入的计数器增量列表。
        events : list[AccountControlEventWrite]
            需要写入的事件列表。

        Raises
        ------
        Exception
            批量写入失败时会回滚事务并将原始异常继续抛出。
        """
        try:
            if counter_deltas:
                await self.session.execute(
                    sqlite_insert(AccountControlCounterDelta)
                    .values([row.model_dump(mode="json") for row in counter_deltas])
                    .prefix_with("OR IGNORE")
                )
            if events:
                await self.session.execute(
                    sqlite_insert(AccountControlEvent)
                    .values([self._serialize_event_write(row) for row in events])
                    .prefix_with("OR IGNORE")
                )
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        await enqueue_recorded_warnings(self.session, events)
