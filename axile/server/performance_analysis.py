"""Single-worker, durable performance queue and atomic snapshot publication."""

from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert

from axile.executor.session_closed import session_closed_scope
from axile.server.analysis_results import RESULT_SCHEMA_VERSION, read_result_points, read_result_range
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.performance import PerformanceSummary
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.performance_costs import timestamp
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
