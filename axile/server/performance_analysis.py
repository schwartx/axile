"""Single-worker, durable performance queue and atomic snapshot publication."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.executor.session_closed import session_closed_scope
from axile.server.db.models import Account, ExecuteRecord, PortfolioAccount, ScheduleSkip, TargetWeightSnapshot
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis import cost_execution as executions
from axile.server.db.models.analysis import cost_trade as trades
from axile.server.db.models.performance import PerformanceBinding, PerformanceSettings, PerformanceSummary
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.performance import Observation, calculate_performance, local_time, observation
from axile.server.performance_costs import SHANGHAI, daily_costs, project_execution, summarize, timestamp
from axile.server.performance_replay import ENGINE_VERSION

# 独立权益账本替代日内收益求和；重建旧回测曲线与同版卡片。
LOGIC_VERSION = "18"
RETRY_DELAYS = (5, 30, 120)


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


async def read_snapshot(session, account_id: int, range_key: str) -> dict:
    """Read only compact persisted results and project range execution history."""
    joined = (
        (
            await session.execute(
                sa.select(states, *[column.label(f"snapshot_{column.name}") for column in snapshots.c])
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id == account_id)
            )
        )
        .mappings()
        .first()
    )
    return _snapshot_response(joined, range_key)


def _snapshot_status(row, has_snapshot: bool):
    """共享卡片和绩效页的发布状态。"""
    status = "empty"
    if row:
        if row["error"]:
            status = "failed"
        elif (
            row["requested"] or row["running_version"] is not None or row["published_version"] != row["source_version"]
        ):
            status = (
                "stale"
                if has_snapshot
                else "pending"
                if row["requested"] or row["running_version"] is not None
                else "empty"
            )
        elif has_snapshot:
            status = "ready"
    return status


def _snapshot_response(joined, range_key: str) -> dict:
    """共用单账户与批量读取的快照状态语义。"""
    row = joined
    snapshot = (
        {column.name: joined[f"snapshot_{column.name}"] for column in snapshots.c}
        if joined and joined["snapshot_id"]
        else None
    )
    incompatible = snapshot is not None and (
        snapshot["logic_version"] != LOGIC_VERSION or snapshot["engine_version"] != ENGINE_VERSION
    )
    if incompatible:
        snapshot = None
    status = "pending" if incompatible else _snapshot_status(row, snapshot is not None)
    result = snapshot["ranges"][range_key] if snapshot else None
    if result and snapshot is not None:
        result = {**result, "performance": _range_performance(snapshot["ranges"], range_key)}
    return {
        "status": status,
        "source_version": row["source_version"] if row else 0,
        "snapshot_id": snapshot["id"] if snapshot else None,
        "snapshot_version": snapshot["source_version"] if snapshot else None,
        "logic_version": snapshot["logic_version"] if snapshot else LOGIC_VERSION,
        "engine_version": snapshot["engine_version"] if snapshot else ENGINE_VERSION,
        "computed_at": snapshot["computed_at"] if snapshot else None,
        "data_until": snapshot["data_until"] if snapshot else None,
        "settings": snapshot["settings"] if snapshot else None,
        "error": row["error"] if row else None,
        "retry_at": row["retry_at"] if row and row["error"] and row["failures"] <= len(RETRY_DELAYS) else None,
        "result": result["performance"] if result else None,
        "daily_costs": result["daily_costs"] if result else {},
        "events": result["events"] if result else [],
        "event_count": result["event_count"] if result else 0,
    }


def _range_performance(ranges: dict, range_key: str) -> dict:
    """投影范围内执行列表，同时兼容已持久化旧范围列表。"""
    performance = ranges[range_key]["performance"]
    if range_key == "all" or "executions" in performance:
        return performance
    executions = ranges.get("all", {}).get("performance", {}).get("executions")
    if not isinstance(executions, list):
        return performance
    try:
        start, end = timestamp(performance["baseline"]), timestamp(performance["end"])
    except (KeyError, TypeError, ValueError):
        return performance
    return {
        **performance,
        "executions": [
            execution
            for execution in executions
            if isinstance(execution, dict)
            and isinstance(execution.get("record"), dict)
            and isinstance(execution["record"].get("created_at"), str)
            and start <= timestamp(execution["record"]["created_at"]) <= end
        ],
    }


async def read_performance_summaries(session, account_ids: list[int]) -> dict[int, PerformanceSummary]:
    """一次读取已发布绩效；卡片不读取执行历史、不触发计算。"""
    if not account_ids:
        return {}
    rows = (
        (
            await session.execute(
                sa.select(
                    states,
                    snapshots.c.id.label("snapshot_id"),
                    snapshots.c.computed_at,
                    snapshots.c.logic_version.label("snapshot_logic_version"),
                    snapshots.c.engine_version.label("snapshot_engine_version"),
                    snapshots.c.ranges["all"]["performance"]["points"].label("points"),
                )
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id.in_(account_ids))
            )
        )
        .mappings()
        .all()
    )
    summaries = {}
    for row in rows:
        incompatible = row["snapshot_id"] is not None and (
            row["snapshot_logic_version"] != LOGIC_VERSION or row["snapshot_engine_version"] != ENGINE_VERSION
        )
        points = [] if incompatible else row["points"] or []
        last = points[-1] if points else {}
        summaries[row["account_id"]] = PerformanceSummary(
            snapshot_id=None if incompatible else row["snapshot_id"],
            status="pending" if incompatible else _snapshot_status(row, row["snapshot_id"] is not None),
            computed_at=None if incompatible else row["computed_at"],
            observed_at=last.get("observed_at"),
            account_equity=last.get("account_equity"),
            account_daily_return=last.get("account_daily_return"),
            points=points,
        )
    return summaries


def _events(records, bindings, skips) -> list[dict]:
    events = [
        {
            "time": binding.created_at,
            "tag": "换绑",
            "text": "解绑组合" if binding.portfolio_id is None else f"组合 #{binding.portfolio_id}",
            "executionId": None,
        }
        for binding in bindings
    ]
    events.extend(
        {
            "time": skip.triggered_at,
            "tag": "跳过",
            "text": "执行中" if skip.reason_code == "BUSY" else "休市",
            "executionId": None,
        }
        for skip in skips
    )
    for record in records:
        if record.is_success == 1:
            continue
        raw = cast("dict[str, Any]", normalize_legacy_result(record.raw_result))
        status = raw.get("status") if isinstance(raw, dict) else None
        labels: dict[str | None, str] = {
            "SUCCEEDED": "调仓完成",
            "NOOP": "无需调仓",
            "PARTIAL": "执行未全部完成",
            "FAILED": "执行失败",
            "BLOCKED": "未执行",
        }
        label = labels.get(status, "执行状态未知")
        scope = session_closed_scope(raw)
        if scope == "all":
            label = "休市跳过"
        elif scope == "partial":
            label = "部分品种休市 · 执行未全部完成"
        if raw.get("task_status") == "TERMINATED":
            label = "执行已终止"
        reason = raw.get("error") if status in {"FAILED", "BLOCKED", "PARTIAL"} else None
        text = f"{label} · {reason}" if reason else label
        events.append(
            {
                "time": record.created_at,
                "tag": label,
                "text": text[:500],
                "executionId": record.execution_id,
            }
        )
    return sorted(events, key=lambda event: timestamp(event["time"]), reverse=True)


@dataclass
class _PreparedHistory:
    """跨批次保留计算必需的投影，不持有原始执行或 ORM 对象。"""

    observations: list[Observation] = field(default_factory=list)
    projected: list[dict] = field(default_factory=list)
    cost_rows: list[dict] = field(default_factory=list)
    by_record: dict[int | None, list[dict]] = field(default_factory=dict)
    record_times: list[tuple[int | None, str]] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


def _prepare_records(prepared: _PreparedHistory, records, fallback: dict) -> None:
    """沿用原转换与记录顺序；本批原始对象在返回后即可释放。"""
    prepared.observations.extend(observation(record, fallback.get(record.execution_id or "")) for record in records)
    for record in records:
        payload, fills = project_execution(record)
        prepared.by_record[record.id] = fills
        prepared.record_times.append((record.id, record.created_at))
        prepared.projected.append(
            {
                "record_id": record.id,
                "time": timestamp(record.created_at),
                "success": record.is_success == 1,
                "noop": payload["noop"],
                "payload": payload,
            }
        )
        for fill in fills:
            prepared.cost_rows.append(
                {
                    "id": len(prepared.cost_rows),
                    "record_id": record.id,
                    "symbol": fill["symbol"],
                    "time": fill["time"],
                    "day": fill["day"],
                    "value": fill["value"],
                    "cost": fill["cost"],
                    "loss_bp": fill["lossBp"],
                    "fee": fill["fee"],
                    "fee_currency": fill["feeCurrency"],
                    "estimated": fill["timeEstimated"],
                    "payload": fill,
                }
            )
    prepared.events.extend(_events(records, [], []))


def compute_batch(
    records, targets, bindings, skips, settings: PerformanceSettings, trade_channel: str | None = None
) -> tuple[dict, list, list]:
    """Read history once, independently rebase each range, and share daily/cumulative results."""
    if isinstance(records, _PreparedHistory):
        prepared = records
    else:
        prepared = _PreparedHistory()
        fallback = {item.execution_id: item.normalized_weights for item in targets if item.execution_id}
        _prepare_records(prepared, records, fallback)
    observations = prepared.observations
    projected, cost_rows, by_record = prepared.projected, prepared.cost_rows, prepared.by_record
    events = sorted(
        _events([], bindings, skips) + prepared.events, key=lambda event: timestamp(event["time"]), reverse=True
    )
    ranges = {}
    for range_key in ("30", "90", "all"):
        result = calculate_performance(observations, settings, range_key, trade_channel=trade_channel)
        start, end = timestamp(result.baseline) if result.baseline else 0, timestamp(result.end) if result.end else -1
        result.bindings = [
            PerformanceBinding(time=local_time(binding.created_at).isoformat(), portfolio_id=binding.portfolio_id)
            for binding in sorted(bindings, key=lambda item: local_time(item.created_at))
            if start <= timestamp(binding.created_at) <= end
        ]
        range_executions = None
        if range_key == "all":
            range_executions = [
                item["payload"] | {"summary": summarize(by_record[item["record_id"]])}
                for item in sorted(projected, key=lambda item: (item["time"], item["record_id"]))
                if start <= item["time"] <= end
            ]
        selected_trades = [
            fill
            for identity, when in prepared.record_times
            if start <= timestamp(when) <= end
            for fill in by_record[identity]
        ]
        selected_events = [event for event in events if start <= timestamp(event["time"]) <= end]
        # 执行投影已由数据库 JSON 值和标量构成，避免再次深度复制整份历史。
        performance = result.model_dump(mode="json")
        if range_executions is not None:
            performance["executions"] = range_executions
        else:
            performance.pop("executions", None)
        ranges[range_key] = {
            "performance": performance,
            "daily_costs": daily_costs(selected_trades),
            "events": selected_events[:100],
            "event_count": len(selected_events),
        }
    return ranges, projected, cost_rows


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
        async with self.sessions() as session:
            await session.execute(
                states.update()
                .where(states.c.running_version.is_not(None))
                .values(running_version=None, requested=True)
            )
            await session.execute(
                states.update()
                .where(
                    states.c.current_snapshot.is_not(None),
                    sa.or_(states.c.logic_version != LOGIC_VERSION, states.c.engine_version != ENGINE_VERSION),
                )
                .values(source_version=states.c.source_version + 1, failures=0, retry_at=0, error=None)
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
                await session.execute(
                    states.update()
                    .where(states.c.account_id == row["account_id"])
                    .values(running_version=states.c.source_version, requested=False)
                )
                await session.commit()
            return row

    async def _load(self, account_id):
        async with self.sessions() as session:
            # SQLite legacy transaction mode does not begin a snapshot for SELECT.
            await session.execute(sa.text("BEGIN"))
            account = await session.get(Account, account_id)
            if account is None:
                return None
            settings = PerformanceSettings.model_validate(account, from_attributes=True)
            history = []
            for model in (TargetWeightSnapshot, PortfolioAccount, ScheduleSkip):
                history.append(
                    (await session.execute(sa.select(model).where(model.account_id == account_id))).scalars().all()
                )
            prepared = _PreparedHistory()
            fallback = {item.execution_id: item.normalized_weights for item in history[0] if item.execution_id}
            rows = await session.stream(
                sa.select(
                    col(ExecuteRecord.id),
                    col(ExecuteRecord.execution_id),
                    col(ExecuteRecord.created_at),
                    col(ExecuteRecord.is_success),
                    col(ExecuteRecord.raw_input),
                    col(ExecuteRecord.raw_result),
                )
                .where(ExecuteRecord.account_id == account_id)
                .execution_options(yield_per=100)
            )
            try:
                async for batch in rows.partitions(100):
                    await run_in_threadpool(_prepare_records, prepared, batch, fallback)
            finally:
                await rows.close()
            return (prepared, *history, settings, str(account.trade_channel))

    async def run_once(self) -> bool:
        """Claim one durable job, compute outside DB transactions, then compare-and-publish."""
        async with self.compute_lock:
            row = await self._claim()
            if row is None:
                return False
            try:
                inputs = await self._load(row["account_id"])
                if inputs is None:
                    return True
                records, targets, bindings, skips, settings, trade_channel = inputs
                result = await run_in_threadpool(
                    compute_batch, records, targets, bindings, skips, settings, trade_channel
                )
                await self._publish(row, settings, result)
            except Exception:
                logger.exception("Performance calculation failed for account {}", row["account_id"])
                await self._fail(row)
            return True

    async def _fail(self, row) -> None:
        async with self.sessions() as session:
            failures = row["failures"] + 1
            await session.execute(
                states.update()
                .where(states.c.account_id == row["account_id"], states.c.source_version == row["source_version"])
                .values(
                    running_version=None,
                    requested=True if failures <= len(RETRY_DELAYS) else False,
                    failures=failures,
                    retry_at=get_default_clock().time() + RETRY_DELAYS[min(failures - 1, len(RETRY_DELAYS) - 1)],
                    error="绩效计算失败，请重试或查看服务日志",
                )
            )
            await session.execute(
                states.update()
                .where(states.c.account_id == row["account_id"], states.c.source_version != row["source_version"])
                .values(running_version=None, requested=True)
            )
            await session.commit()

    async def _publish(self, row, settings, result) -> None:
        batch_id = uuid4().hex
        ranges, projected, cost_rows = result
        async with self.sessions() as session:
            # Acquire the SQLite writer slot only for publication, never during WBT.
            updated = await session.execute(
                states.update()
                .where(states.c.account_id == row["account_id"], states.c.source_version == row["source_version"])
                .values(
                    previous_snapshot=states.c.current_snapshot,
                    current_snapshot=batch_id,
                    published_version=row["source_version"],
                    running_version=None,
                    requested=False,
                    failures=0,
                    retry_at=0,
                    error=None,
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                )
                .returning(states.c.previous_snapshot)
            )
            previous = updated.first()
            if previous is None:
                await session.execute(
                    states.update()
                    .where(states.c.account_id == row["account_id"])
                    .values(running_version=None, requested=True)
                )
                await session.commit()
                return
            # 执行参数不进入 SQL 编译缓存；避免缓存持有整份历史快照。
            await session.execute(
                snapshots.insert(),
                dict(
                    id=batch_id,
                    account_id=row["account_id"],
                    source_version=row["source_version"],
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                    computed_at=clock_now_iso(tz=SHANGHAI),
                    data_until=ranges["all"]["performance"]["end"],
                    settings=settings.model_dump(),
                    ranges=ranges,
                ),
            )
            for table, values in ((executions, projected), (trades, cost_rows)):
                for start in range(0, len(values), 500):
                    await session.execute(
                        table.insert(), [{**value, "snapshot_id": batch_id} for value in values[start : start + 500]]
                    )
            obsolete = sa.select(snapshots.c.id).where(
                snapshots.c.account_id == row["account_id"],
                snapshots.c.id.not_in([value for value in (batch_id, previous[0]) if value]),
            )
            for table in (trades, executions):
                await session.execute(table.delete().where(table.c.snapshot_id.in_(obsolete)))
            await session.execute(snapshots.delete().where(snapshots.c.id.in_(obsolete)))
            await session.commit()
