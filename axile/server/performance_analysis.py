"""后端增量分析队列；状态与日结果原子更新。"""

import asyncio
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.server._native import _core
from axile.server.core.db import database_path
from axile.server.native_store import native_call, native_operation

# 标记贡献估值缺口并允许后续分段恢复；重建旧分析。
LOGIC_VERSION = "24"
ENGINE_VERSION = "equity-ledger-1"


def ensure_native_capabilities() -> None:
    """Reject obsolete extensions before any background services start."""
    required = (
        "close_database",
        "account_public",
        "connection_accounts",
        "execution_read",
        "supplement_has_execution_steps",
        "analysis_update",
        "analysis_query",
        "persist_execution_state",
        "freeze_execution_input",
        "freeze_execution_after_assets",
        "queue_enqueue",
        "queue_recover",
        "queue_claim",
        "queue_fail",
        "queue_release",
        "analysis_context",
        "analysis_calendar_save",
        "analysis_commit_confirmed",
    )
    if getattr(_core, "ANALYSIS_FACT_SCHEMA", None) != 40:
        raise RuntimeError("Native analysis extension requires direct business facts schema 0034; rebuild the wheel")
    if getattr(_core, "PUBLIC_HEAD", None) != "0039":
        raise RuntimeError("Native analysis extension requires migration head 0039; rebuild the wheel")
    missing = [name for name in required if not callable(getattr(_core, name, None))]
    if missing:
        raise RuntimeError(f"Native analysis extension must export {', '.join(missing)}; rebuild the wheel")


async def enqueue(path: str, account_id: int, force: bool = False) -> None:
    """Coalesce requests after the caller has closed its database transaction."""
    await native_call(_core.queue_enqueue, path, account_id, force)


class AnalysisManager:
    """One background calculation at a time, independent of trading execution locks."""

    def __init__(self):
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
        await native_call(_core.queue_recover, path, LOGIC_VERSION, ENGINE_VERSION)
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
        return database_path()

    async def _claim(self):
        return await native_call(_core.queue_claim, self._database_path(), get_default_clock().time(), uuid4().hex)

    async def _run_native(self, row) -> None:
        """Python 准备渠道日历；Rust 直接读取业务事实并原子发布。"""
        from axile.server.analysis_calendar import ensure_calendar_facts

        metadata = await native_operation("analysis_context", {"account_id": row["account_id"]})
        if metadata is None:
            return
        channel = metadata["trade_channel"]
        await ensure_calendar_facts(channel, metadata["first_day"], metadata["last_day"])
        receipt = await native_operation(
            "analysis_update",
            {
                "account_id": row["account_id"],
                "expected_source_version": row["source_version"],
                "claim_token": row["running_token"],
                "computed_at": clock_now_iso(tz=ZoneInfo("Asia/Shanghai")),
            },
            database=self._database_path(),
        )
        if not receipt["updated"]:
            await self._release_claim(row)

    async def _native_update_committed(self, row) -> bool:
        """回执、进程退出或取消不决定提交结果，按本次 token 核对持久化快照。"""
        token = row.get("running_token")
        if token is None:
            return False
        return await native_operation(
            "analysis_commit_confirmed",
            {
                "account_id": row["account_id"],
                "source_version": row["source_version"],
                "logic_version": LOGIC_VERSION,
                "engine_version": ENGINE_VERSION,
            },
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
                await self._release_claim(row)
                raise
            except Exception:
                if await self._native_update_committed(row):
                    logger.info(
                        "Native analysis update committed before receipt loss for account {}", row["account_id"]
                    )
                else:
                    logger.exception("Performance calculation failed for account {}", row["account_id"])
                    await self._fail(row)
            return True

    async def _release_claim(self, row) -> None:
        """只释放本次仍占有的任务；已提交发布或较新任务的状态保持不变。"""
        await native_call(
            _core.queue_release, self._database_path(), row["account_id"], row["source_version"], row["running_token"]
        )

    async def _fail(self, row) -> None:
        """Only fail the claim still owned by this worker."""
        await native_call(
            _core.queue_fail,
            self._database_path(),
            row["account_id"],
            row["source_version"],
            row["running_token"],
            get_default_clock().time(),
        )
