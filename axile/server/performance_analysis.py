"""Single-worker, durable performance queue and atomic snapshot publication."""

import asyncio
import json
from uuid import uuid4

import sqlalchemy as sa
from loguru import logger
from sqlmodel import col
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.server._native import _core
from axile.server.db.models.account import Account
from axile.server.db.models.account_settings import AccountSettings, BacktestSettings
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.performance_costs import SHANGHAI

# 独立权益账本替代日内收益求和；重建旧回测曲线与同版卡片。
LOGIC_VERSION = "18"
ENGINE_VERSION = "equity-ledger-1"


def ensure_native_capabilities() -> None:
    """Reject obsolete extensions before any background services start."""
    required = (
        "publish_store",
        "queue_enqueue",
        "queue_status",
        "queue_recover",
        "queue_claim",
        "queue_fail",
        "queue_release",
    )
    missing = [name for name in required if not callable(getattr(_core, name, None))]
    if missing:
        raise RuntimeError(f"Native analysis extension must export {', '.join(missing)}; rebuild the wheel")


async def enqueue(path: str, account_id: int, force: bool = False) -> None:
    """Coalesce requests after the caller has closed its database transaction."""
    await run_in_threadpool(_core.queue_enqueue, path, account_id, force)


async def read_snapshot_status(path: str, account_id: int) -> str:
    """Read committed publication state on an independent native connection."""
    return await run_in_threadpool(_core.queue_status, path, account_id, LOGIC_VERSION, ENGINE_VERSION)


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
        ensure_native_capabilities()
        path = self._database_path()
        await run_in_threadpool(_core.queue_recover, path, LOGIC_VERSION, ENGINE_VERSION)
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

    def _database_path(self) -> str:
        with self.sessions().sync_session as session:
            path = session.get_bind().url.database
        if not path or path == ":memory:":
            raise RuntimeError("Native queue requires a file-backed SQLite database")
        return path

    async def _claim(self):
        return await run_in_threadpool(
            _core.queue_claim, self._database_path(), get_default_clock().time(), uuid4().hex
        )

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
        await run_in_threadpool(
            _core.queue_release, self._database_path(), row["account_id"], row["source_version"], row["running_token"]
        )

    async def _fail(self, row) -> None:
        """Only fail the claim still owned by this worker."""
        if await self._native_publication_committed(row):
            return
        await run_in_threadpool(
            _core.queue_fail,
            self._database_path(),
            row["account_id"],
            row["source_version"],
            row["running_token"],
            get_default_clock().time(),
        )
