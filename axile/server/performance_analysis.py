"""Single-worker, durable performance queue and atomic snapshot publication."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import uuid4

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.executor.session_closed import session_closed_scope
from axile.server.analysis_cost_facts import COST_FACT_SCHEMA_VERSION, COST_FACT_TABLES, iter_cost_fact_batches
from axile.server.analysis_results import (
    RESULT_SCHEMA_VERSION,
    iter_result_fact_batches,
    read_result_points,
    read_result_range,
)
from axile.server.analysis_view_writer import prepare_read_views
from axile.server.db.models import Account, PortfolioAccount, ScheduleSkip
from axile.server.db.models.account_settings import AccountSettings, BacktestSettings
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis import cost_execution as execution_facts
from axile.server.db.models.analysis import cost_range as cost_ranges
from axile.server.db.models.analysis import cost_trade as trade_facts
from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.db.models.analysis_view import READ_VIEW_TABLES
from axile.server.db.models.performance import PerformanceSettings, PerformanceSummary
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.performance import Observation, calculate_performance, local_time, observation, performance_calendar
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
    """在同一读快照中读取发布指针及全部规范化结果。"""
    async with session.begin_nested():
        return await _read_snapshot(session, account_id, range_key)


async def _read_snapshot(session, account_id: int, range_key: str) -> dict:
    """Read only compact persisted results and project range execution history."""
    metadata = (
        (
            await session.execute(
                sa.select(
                    states,
                    *[
                        column.label(f"snapshot_{column.name}")
                        for column in snapshots.c
                        if column.name not in {"ranges", "settings"}
                    ],
                )
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id == account_id)
            )
        )
        .mappings()
        .first()
    )
    if metadata and metadata["snapshot_id"] and metadata["snapshot_result_schema_version"] == RESULT_SCHEMA_VERSION:
        joined = dict(metadata)
        settings = {
            "backtest_weight_type": joined["snapshot_backtest_weight_type"],
            "backtest_fee_rate": joined["snapshot_backtest_fee_rate"],
        }
        compatible = (
            joined["snapshot_logic_version"] == LOGIC_VERSION and joined["snapshot_engine_version"] == ENGINE_VERSION
        )
        result = await read_result_range(session, joined["snapshot_id"], range_key, settings) if compatible else None
        if compatible and result is None:
            raise RuntimeError("已发布的结构化绩效结果缺少区间")
        joined["snapshot_settings"] = settings
        joined["snapshot_ranges"] = {range_key: result}
        return _snapshot_response(joined, range_key)
    joined = (
        (
            await session.execute(
                sa.select(
                    states,
                    *[
                        column.label(f"snapshot_{column.name}")
                        for column in snapshots.c
                        if column.name != "cost_schema_version"
                    ],
                )
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id == account_id)
            )
        )
        .mappings()
        .first()
    )
    return _snapshot_response(joined, range_key)


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
    if (
        row
        and row["snapshot_id"] is not None
        and (row["snapshot_logic_version"] != LOGIC_VERSION or row["snapshot_engine_version"] != ENGINE_VERSION)
    ):
        return "pending"
    return _snapshot_status(row, row is not None and row["snapshot_id"] is not None)


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
        {
            column.name: joined[f"snapshot_{column.name}"]
            for column in snapshots.c
            if f"snapshot_{column.name}" in joined
        }
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


async def _legacy_performance_summaries(session, account_ids: list[int]) -> dict[int, PerformanceSummary]:
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
        summaries[row["account_id"]] = PerformanceSummary.model_validate(
            {
                "snapshot_id": None if incompatible else row["snapshot_id"],
                "status": "pending" if incompatible else _snapshot_status(row, row["snapshot_id"] is not None),
                "computed_at": None if incompatible else row["computed_at"],
                "observed_at": last.get("observed_at"),
                "account_equity": last.get("account_equity"),
                "account_daily_return": last.get("account_daily_return"),
                "points": points,
            }
        )
    return summaries


async def read_performance_summaries(session, account_ids: list[int]) -> dict[int, PerformanceSummary]:
    """让批量状态和曲线共享 SQLite 读快照，避免发布回收期间读到半份结果。"""
    async with session.begin_nested():
        return await _read_performance_summaries(session, account_ids)


async def _read_performance_summaries(session, account_ids: list[int]) -> dict[int, PerformanceSummary]:
    """批量读取小型状态及规范化曲线；新快照不解析 ranges 大 JSON。"""
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
                    snapshots.c.result_schema_version,
                )
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id.in_(account_ids))
            )
        )
        .mappings()
        .all()
    )
    typed = [row for row in rows if row["snapshot_id"] and row["result_schema_version"] == RESULT_SCHEMA_VERSION]
    points_by_snapshot = await read_result_points(session, [row["snapshot_id"] for row in typed])
    legacy_ids = [
        row["account_id"]
        for row in rows
        if row["snapshot_id"] and row["result_schema_version"] != RESULT_SCHEMA_VERSION
    ]
    summaries = await _legacy_performance_summaries(session, legacy_ids) if legacy_ids else {}
    for row in rows:
        if row["account_id"] in summaries:
            continue
        incompatible = row["snapshot_id"] is not None and (
            row["snapshot_logic_version"] != LOGIC_VERSION or row["snapshot_engine_version"] != ENGINE_VERSION
        )
        points = [] if incompatible else points_by_snapshot.get(row["snapshot_id"], [])
        last = points[-1] if points else {}
        summaries[row["account_id"]] = PerformanceSummary.model_validate(
            {
                "snapshot_id": None if incompatible else row["snapshot_id"],
                "status": "pending" if incompatible else _snapshot_status(row, row["snapshot_id"] is not None),
                "computed_at": None if incompatible else row["computed_at"],
                "observed_at": last.get("observed_at"),
                "account_equity": last.get("account_equity"),
                "account_daily_return": last.get("account_daily_return"),
                "points": points,
            }
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
        raw = normalize_legacy_result(record.result)
        if not isinstance(raw, Mapping):
            raise ValueError("Execution event result must be a mapping")
        status = raw.get("status")
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


def _prepare_records(prepared: _PreparedHistory, records, fallback: dict, include_observations: bool = True) -> None:
    """沿用原转换与记录顺序；本批原始对象在返回后即可释放。"""
    if include_observations:
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


def _prepare_fact_batch(prepared: _PreparedHistory, batch, include_observations: bool = True) -> None:
    """复用冻结的派生事实，Python 回退不再展开整个规范业务历史。"""
    for row, payload in zip(batch.headers, batch.payloads, strict=True):
        record_id = row["record_id"]
        if include_observations:
            prepared.observations.append(batch.observations[record_id])
        prepared.by_record[record_id] = []
        prepared.record_times.append((record_id, row["created_at"]))
        prepared.projected.append(
            {
                "record_id": record_id,
                "time": row["time"],
                "success": row["success"],
                "noop": row["noop"],
                "payload": payload,
            }
        )
        if row["event_tag"] is not None:
            prepared.events.append(
                {
                    "time": row["created_at"],
                    "tag": row["event_tag"],
                    "text": row["event_text"],
                    "executionId": row["execution_id"],
                }
            )
    for trade in batch.trades:
        prepared.by_record[trade["record_id"]].append(trade["payload"])
        prepared.cost_rows.append(
            {
                key: value
                for key, value in {**trade, "id": len(prepared.cost_rows)}.items()
                if key not in {"trade_index", "side", "quantity", "price", "reference", "reference_source"}
            }
        )


def compute_batch(
    records,
    targets,
    bindings,
    skips,
    settings: PerformanceSettings,
    trade_channel: str | None = None,
    *,
    native_performance: dict | None = None,
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
        if native_performance is None:
            result = calculate_performance(observations, settings, range_key, trade_channel=trade_channel)
            performance = result.model_dump(mode="json")
        else:
            performance = native_performance[range_key]
            performance["calendar"] = performance_calendar(
                trade_channel, performance["baseline"], performance["end"]
            ).model_dump(mode="json")
        start = timestamp(performance["baseline"]) if performance["baseline"] else 0
        end = timestamp(performance["end"]) if performance["end"] else -1
        performance["bindings"] = [
            {"time": local_time(binding.created_at).isoformat(), "portfolio_id": binding.portfolio_id}
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
                .values(running_version=None, running_token=None, requested=True)
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

    async def _load(self, account_id, *, include_observations: bool = True):
        from axile.server.analysis_history import iter_analysis_history
        from axile.server.analysis_observations import ensure_observation_facts
        from axile.server.analysis_source import ensure_source_facts

        async with self.sessions() as session:
            # 冷补齐只读取缺失投影的单条规范事实；正常回退计算仍读取标量历史。
            if include_observations:
                await ensure_observation_facts(session, account_id)
            await ensure_source_facts(session, account_id)
            # SQLite legacy transaction mode does not begin a snapshot for SELECT.
            await session.execute(sa.text("BEGIN"))
            account = await session.get(Account, account_id)
            if account is None:
                return None
            settings = PerformanceSettings.model_validate(account, from_attributes=True)
            history = [[]]
            for model in (PortfolioAccount, ScheduleSkip):
                history.append(
                    (await session.execute(sa.select(model).where(model.account_id == account_id))).scalars().all()
                )
            prepared = _PreparedHistory()
            async for batch in iter_analysis_history(session, account_id, include_observations=include_observations):
                _prepare_fact_batch(prepared, batch, include_observations)
            return (prepared, *history, settings, str(account.trade_channel))

    async def _run_native(self, row) -> bool:
        """Python 仅准备源事实；Rust 负责计算、结果暂存及成功发布事务。"""
        from axile.server.analysis_calendar import ensure_calendar_facts
        from axile.server.analysis_observations import ensure_observation_facts
        from axile.server.analysis_source import ensure_source_facts
        from axile.server.native_analysis import get_native_analysis

        async with self.sessions() as session:
            native = get_native_analysis(session)
            if native is None:
                return False
            metadata = (
                await session.execute(
                    sa.select(col(Account.trade_channel), col(AccountSettings.backtest))
                    .select_from(Account)
                    .outerjoin(AccountSettings, col(AccountSettings.account_id) == col(Account.id))
                    .where(col(Account.id) == row["account_id"])
                )
            ).one_or_none()
            if metadata is None:
                return True
            backtest = metadata.backtest or BacktestSettings()
            settings = PerformanceSettings.model_validate(backtest.model_dump())
            channel = str(metadata.trade_channel)
            await session.rollback()
            await ensure_observation_facts(session, row["account_id"])
            progress = await ensure_source_facts(session, row["account_id"])
            if progress.unsupported:
                # 无法无损投影的历史明确选择兼容计算，不在原生出错后重跑。
                return False
            await ensure_calendar_facts(session, row["account_id"], channel)
        receipt = await native.publish_store(
            account_id=row["account_id"],
            expected_source_version=row["source_version"],
            snapshot_id=row["running_token"],
            logic_version=LOGIC_VERSION,
            engine_version=ENGINE_VERSION,
            computed_at=clock_now_iso(tz=SHANGHAI),
            settings=settings.model_dump(),
            trade_channel=channel,
        )
        if receipt["published"]:
            if not await self._native_publication_committed(row):
                from axile.server.native_analysis import NativeAnalysisProtocolError

                raise NativeAnalysisProtocolError("原生发布回执没有对应已提交快照", code="NATIVE_PUBLICATION_MISSING")
        elif receipt["reason"] == "source_changed":
            await self._release_claim(row)
        return True

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
                if await self._run_native(row):
                    return True
                inputs = await self._load(row["account_id"])
                if inputs is None:
                    return True
                records, targets, bindings, skips, settings, trade_channel = inputs
                arguments = (records, targets, bindings, skips, settings, trade_channel)
                result = await run_in_threadpool(compute_batch, *arguments)
                await self._publish(row, settings, result)
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

    async def _publish_structured(self, row, settings: PerformanceSettings, staged) -> None:
        """全部流已成功收齐后写入规范化结果，数据库写入始终由 Python 完成。"""
        from axile.server.analysis_publication import copy_source_cost_facts

        batch_id = row["running_token"]
        table_by_name = {table.name: table for table in RESULT_TABLES}
        async with self.sessions() as session:
            previous = await self._claim_publication(session, row, batch_id)
            if previous is None:
                await session.commit()
                return
            await session.execute(
                snapshots.insert(),
                {
                    "id": batch_id,
                    "account_id": row["account_id"],
                    "source_version": row["source_version"],
                    "logic_version": LOGIC_VERSION,
                    "engine_version": ENGINE_VERSION,
                    "computed_at": clock_now_iso(tz=SHANGHAI),
                    "data_until": staged.complete["data_until"],
                    "settings": {},
                    "ranges": {},
                    "cost_schema_version": COST_FACT_SCHEMA_VERSION,
                    "result_schema_version": RESULT_SCHEMA_VERSION,
                    "backtest_weight_type": settings.backtest_weight_type,
                    "backtest_fee_rate": settings.backtest_fee_rate,
                },
            )
            range_headers = []
            for name, columns, rows in staged.rows():
                values = [{"snapshot_id": batch_id, **dict(zip(columns, values, strict=True))} for values in rows]
                await session.execute(table_by_name[name].insert(), values)
                if name == "performance_result_range":
                    range_headers.extend(
                        {
                            "snapshot_id": batch_id,
                            "range_key": value["range_key"],
                            "baseline": timestamp(value["baseline"]) if value["baseline"] else None,
                            "end": timestamp(value["end"]) if value["end"] else None,
                        }
                        for value in values
                    )
            await session.execute(cost_ranges.insert(), range_headers)
            await copy_source_cost_facts(session, batch_id, row["account_id"])
            await prepare_read_views(session, batch_id)
            await self._delete_obsolete(session, row["account_id"], batch_id, previous[0])
            await session.commit()

    async def _claim_publication(self, session, row, batch_id: str):
        """比较源版本并取得写入槽；后续任一步失败均随事务整体回滚。"""
        result = await session.execute(
            states.update()
            .where(
                states.c.account_id == row["account_id"],
                states.c.source_version == row["source_version"],
                states.c.running_version == row["source_version"],
                states.c.running_token == row["running_token"],
            )
            .values(
                previous_snapshot=states.c.current_snapshot,
                current_snapshot=batch_id,
                published_version=row["source_version"],
                running_version=None,
                running_token=None,
                requested=False,
                failures=0,
                retry_at=0,
                error=None,
                logic_version=LOGIC_VERSION,
                engine_version=ENGINE_VERSION,
            )
            .returning(states.c.previous_snapshot)
        )
        previous = result.first()
        if previous is None:
            await session.execute(
                states.update()
                .where(
                    states.c.account_id == row["account_id"],
                    states.c.running_version == row["source_version"],
                    states.c.running_token == row["running_token"],
                )
                .values(running_version=None, running_token=None, requested=True)
            )
        return previous

    async def _delete_obsolete(self, session, account_id: int, batch_id: str, previous: str | None) -> None:
        """删除已退出当前与上一版本的结果及成本行，保留原始业务事实。"""
        obsolete = sa.select(snapshots.c.id).where(
            snapshots.c.account_id == account_id,
            snapshots.c.id.not_in([value for value in (batch_id, previous) if value]),
        )
        for table in (*READ_VIEW_TABLES, *RESULT_TABLES, *COST_FACT_TABLES):
            await session.execute(table.delete().where(table.c.snapshot_id.in_(obsolete)))
        await session.execute(snapshots.delete().where(snapshots.c.id.in_(obsolete)))

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

    async def _publish(self, row, settings, result) -> None:
        batch_id = row["running_token"]
        ranges, projected, cost_rows = result
        async with self.sessions() as session:
            previous = await self._claim_publication(session, row, batch_id)
            if previous is None:
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
                    settings={},
                    ranges={},
                    cost_schema_version=COST_FACT_SCHEMA_VERSION,
                    result_schema_version=RESULT_SCHEMA_VERSION,
                    backtest_weight_type=settings.backtest_weight_type,
                    backtest_fee_rate=settings.backtest_fee_rate,
                ),
            )
            try:
                async with session.begin_nested():
                    for table, values in iter_cost_fact_batches(batch_id, projected, cost_rows, ranges):
                        if table in (execution_facts, trade_facts):
                            values = [{**value, "payload": {}} for value in values]
                        await session.execute(table.insert(), values)
                    for table, values in iter_result_fact_batches(batch_id, ranges):
                        await session.execute(table.insert(), values)
            except (ValueError, TypeError, OverflowError):
                # 旧历史可能含非标量身份或错误值；保留原始投影并显式标注
                # 兼容版本，不能通过字符串强转改变现有公开语义。
                await session.execute(
                    snapshots.update()
                    .where(snapshots.c.id == batch_id)
                    .values(
                        cost_schema_version=0, result_schema_version=0, settings=settings.model_dump(), ranges=ranges
                    )
                )
                for table, values in ((execution_facts, projected), (trade_facts, cost_rows)):
                    for start in range(0, len(values), 500):
                        await session.execute(
                            table.insert(),
                            [{**value, "snapshot_id": batch_id} for value in values[start : start + 500]],
                        )
            await prepare_read_views(session, batch_id)
            await self._delete_obsolete(session, row["account_id"], batch_id, previous[0])
            await session.commit()
