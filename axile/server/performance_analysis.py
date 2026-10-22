"""Single-worker, durable performance queue and atomic snapshot publication."""

import asyncio
import json
from uuid import uuid4

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.server._native import _core
from axile.server.db.models.account import Account
from axile.server.db.models.account_settings import AccountSettings, BacktestSettings
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.performance_costs import SHANGHAI

# 独立权益账本替代日内收益求和；重建旧回测曲线与同版卡片。
LOGIC_VERSION = "18"
ENGINE_VERSION = "equity-ledger-1"


async def enqueue(session, account_id: int, force: bool = False) -> None:
    """Coalesce refresh requests; a ready or exhausted batch can be forced again."""
    await session.execute(insert(states).values(account_id=account_id).on_conflict_do_nothing())
    if force:
        await session.execute(
            states.update()
            .where(
                states.c.account_id == account_id,
                states.c.running_version.is_(None),
                sa.or_(
                    states.c.error.is_not(None),
                    sa.and_(states.c.published_version == states.c.source_version, ~states.c.requested),
                ),
            )
            .values(source_version=states.c.source_version + 1, failures=0, retry_at=0, error=None)
        )
    await session.execute(states.update().where(states.c.account_id == account_id).values(requested=True))
    await session.commit()


async def read_snapshot_status(session, account_id: int) -> str:
    """仅读取发布状态，等待计算时不反复展开完整绩效快照。"""
    row = (
        (
            await session.execute(
                sa.select(
                    states,
                    snapshots.c.id.label("snapshot_id"),
                    snapshots.c.logic_version.label("snapshot_logic_version"),
                    snapshots.c.engine_version.label("snapshot_engine_version"),
                )
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id == account_id)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return "empty"
    return _core.publication_status(
        row["snapshot_id"],
        row["snapshot_logic_version"] == LOGIC_VERSION and row["snapshot_engine_version"] == ENGINE_VERSION,
        row["error"],
        bool(row["requested"] or row["running_version"] is not None),
        row["published_version"] == row["source_version"],
    )


RETRY_DELAYS = (5, 30, 120)


class AnalysisManager:
    """One background calculation at a time, independent of trading execution locks."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.wake = asyncio.Event()
        self.task = None
        self.stopping = False
        self.compute_lock = asyncio.Lock()

    async def start(self) -> None:
        """Recover interrupted jobs and invalidate results from older calculation engines."""
        if self.task is not None:
            return
        if not hasattr(_core, "publish_store"):
            raise RuntimeError("Native analysis extension must export publish_store; rebuild the wheel")
        async with self.sessions() as session:
            await session.execute(
                states.update()
                .where(states.c.running_version.is_not(None))
                .values(running_version=None, running_token=None, requested=True)
            )
            await session.execute(
                states.update()
                .where(
                    states.c.current_snapshot.is_not(None),
                    sa.or_(
                        states.c.logic_version.is_(None),
                        states.c.engine_version.is_(None),
                        states.c.logic_version != LOGIC_VERSION,
                        states.c.engine_version != ENGINE_VERSION,
                    ),
                )
                .values(source_version=states.c.source_version + 1, requested=True, failures=0, retry_at=0, error=None)
            )
            await session.commit()
        self.stopping = False
        self.task = asyncio.create_task(self._loop(), name="performance-analysis")

    async def stop(self) -> None:
        """Let an in-flight thread finish before releasing the single calculation slot."""
        self.stopping = True
        self.wake.set()
        if self.task:
            await self.task
            self.task = None

    async def _loop(self) -> None:
        while not self.stopping:
            self.wake.clear()
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.exception("Performance queue iteration failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _claim(self):
        async with self.sessions() as session:
            row = (
                (
                    await session.execute(
                        sa.select(states)
                        .where(
                            states.c.running_version.is_(None),
                            states.c.failures <= len(RETRY_DELAYS),
                            states.c.retry_at <= get_default_clock().time(),
                            sa.or_(
                                states.c.requested,
                                sa.and_(
                                    states.c.current_snapshot.is_not(None),
                                    states.c.source_version != states.c.published_version,
                                ),
                            ),
                        )
                        .order_by(states.c.retry_at, states.c.account_id)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row:
                token = uuid4().hex
                claimed = await session.execute(
                    states.update()
                    .where(
                        states.c.account_id == row["account_id"],
                        states.c.source_version == row["source_version"],
                        states.c.running_version.is_(None),
                    )
                    .values(running_version=states.c.source_version, running_token=token, requested=False)
                    .returning(states.c.account_id)
                )
                if claimed.scalar_one_or_none() is None:
                    await session.rollback()
                    return None
                await session.commit()
                return {**row, "running_version": row["source_version"], "running_token": token}
            return row

    async def _run_native(self, row) -> None:
        """Python 仅准备源事实；Rust 负责计算、结果暂存及成功发布事务。"""
        from axile.server.analysis_calendar import ensure_calendar_facts
        from axile.server.analysis_observations import ensure_observation_facts
        from axile.server.analysis_source import ensure_source_facts

        async with self.sessions() as session:
            path = session.get_bind().url.database
            if not path or path == ":memory:":
                raise RuntimeError("Native publication requires a file-backed SQLite database")
            metadata = (
                await session.execute(
                    sa.select(col(Account.trade_channel), col(AccountSettings.backtest))
                    .select_from(Account)
                    .outerjoin(AccountSettings, col(AccountSettings.account_id) == col(Account.id))
                    .where(col(Account.id) == row["account_id"])
                )
            ).one_or_none()
            if metadata is None:
                return
            backtest = metadata.backtest or BacktestSettings()
            settings = backtest.model_dump(mode="json")
            channel = str(metadata.trade_channel)
            await session.rollback()
            await ensure_observation_facts(session, row["account_id"])
            progress = await ensure_source_facts(session, row["account_id"])
            if progress.unsupported:
                raise ValueError("Unsupported historical analysis facts")
            await ensure_calendar_facts(session, row["account_id"], channel)
        request = {
            "command": "publish_store",
            "protocol": 1,
            "account_id": row["account_id"],
            "expected_source_version": row["source_version"],
            "snapshot_id": row["running_token"],
            "claim_token": row["running_token"],
            "logic_version": LOGIC_VERSION,
            "engine_version": ENGINE_VERSION,
            "computed_at": clock_now_iso(tz=SHANGHAI),
            "settings": settings,
            "trade_channel": channel,
        }
        receipt = await run_in_threadpool(_core.publish_store, path, json.dumps(request))
        if receipt["published"] and not await self._native_publication_committed(row):
            raise RuntimeError("Native publication receipt has no committed snapshot")

    async def _native_publication_committed(self, row) -> bool:
        """回执、进程退出或取消不决定提交结果，按本次 token 核对持久化快照。"""
        token = row.get("running_token")
        if token is None:
            return False
        async with self.sessions() as session:
            return bool(
                await session.scalar(
                    sa.select(
                        sa.exists().where(
                            snapshots.c.id == token,
                            snapshots.c.account_id == row["account_id"],
                            snapshots.c.source_version == row["source_version"],
                            snapshots.c.logic_version == LOGIC_VERSION,
                            snapshots.c.engine_version == ENGINE_VERSION,
                            snapshots.c.read_view_schema_version == 1,
                        )
                    )
                )
            )

    async def run_once(self) -> bool:
        """Claim one durable job, compute outside DB transactions, then compare-and-publish."""
        async with self.compute_lock:
            row = await self._claim()
            if row is None:
                return False
            try:
                await self._run_native(row)
            except asyncio.CancelledError:
                await asyncio.shield(self._release_claim(row))
                raise
            except Exception:
                if await self._native_publication_committed(row):
                    logger.info(
                        "Native performance publication committed before receipt loss for account {}", row["account_id"]
                    )
                else:
                    logger.exception("Performance calculation failed for account {}", row["account_id"])
                    await self._fail(row)
            return True

    async def _release_claim(self, row) -> None:
        """只释放本次仍占有的任务；已提交发布或较新任务的状态保持不变。"""
        if await self._native_publication_committed(row):
            return
        async with self.sessions() as session:
            await session.execute(
                states.update()
                .where(
                    states.c.account_id == row["account_id"],
                    states.c.running_version == row["source_version"],
                    states.c.running_token == row["running_token"],
                )
                .values(running_version=None, running_token=None, requested=True)
            )
            await session.commit()

    async def _fail(self, row) -> None:
        """仅本次仍持有的任务记失败，已经提交的原生发布保持成功。"""
        if await self._native_publication_committed(row):
            return
        async with self.sessions() as session:
            failures = row["failures"] + 1
            await session.execute(
                states.update()
                .where(
                    states.c.account_id == row["account_id"],
                    states.c.source_version == row["source_version"],
                    states.c.running_version == row["source_version"],
                    states.c.running_token == row["running_token"],
                )
                .values(
                    running_version=None,
                    running_token=None,
                    requested=True if failures <= len(RETRY_DELAYS) else False,
                    failures=failures,
                    retry_at=get_default_clock().time() + RETRY_DELAYS[min(failures - 1, len(RETRY_DELAYS) - 1)],
                    error="绩效计算失败，请重试或查看服务日志",
                )
            )
            await session.execute(
                states.update()
                .where(
                    states.c.account_id == row["account_id"],
                    states.c.source_version != row["source_version"],
                    states.c.running_version == row["source_version"],
                    states.c.running_token == row["running_token"],
                )
                .values(running_version=None, running_token=None, requested=True)
            )
            await session.commit()
