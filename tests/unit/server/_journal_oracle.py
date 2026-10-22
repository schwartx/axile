"""迁移前 journal 展示语义，仅用于对照测试。"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.business_facts.assets import asset_binding, asset_snapshot
from axile.server.db.models import ExecuteRecord, ExecutionActivity
from axile.server.db.models.performance import CostSummary
from axile.server.db.models.schedule import ActivityExecutionRecord
from axile.server.performance_costs import summarize
from tests.unit.server._execution_read_oracle import ExecuteRecordSummaryPublic


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _compact_execution_activity(
    row: ExecuteRecord, summary: ExecuteRecordSummaryPublic, trades: list[dict], total: float | None
) -> ExecutionActivity:
    """列表只发布类型化头字段、摘要数字和结果权益。"""
    duration = row.execution_time
    costs = CostSummary.model_validate(summarize(trades)) if summary.trade_count is not None else None
    return ExecutionActivity(
        occurred_at=row.created_at,
        record=ActivityExecutionRecord(
            id=row.id,
            execution_id=row.execution_id,
            created_at=row.created_at,
            is_success=row.is_success,
            status=_text_or_none(row.display_status),
            task_status=_text_or_none(row.task_status),
            error=_text_or_none(row.display_error),
            outcome=_text_or_none(row.display_outcome),
            outcome_reason=_text_or_none(row.display_outcome_reason),
            reason_code=_text_or_none(row.reason_code),
            execution_kind=_text_or_none(row.execution_kind),
            symbol_results=summary.symbol_results or {},
            total_asset=total,
            summary=costs,
            duration_sec=duration if duration is not None and duration >= 0 else None,
            trade_count=summary.trade_count,
        ),
    )


async def read_result_asset_totals(session: Any, record_ids: Sequence[int]) -> dict[int, float | None]:
    """只读取终态执行后的有效权益，不加载扩展或持仓。"""
    if not record_ids:
        return {}
    records = SQLModel.metadata.tables["executerecord"]
    inputs = SQLModel.metadata.tables["execution_input"]
    record_owned = sa.and_(
        asset_snapshot.c.record_id == records.c.id,
        asset_snapshot.c.path == asset_binding.c.path,
        asset_snapshot.c.scope == asset_binding.c.scope,
        asset_snapshot.c.ordinal == asset_binding.c.ordinal,
        asset_snapshot.c.map_key.is_not_distinct_from(asset_binding.c.map_key),
    )
    input_owned = sa.and_(
        asset_snapshot.c.input_id.in_([records.c.input_id, records.c.result_input_id]),
        inputs.c.account_id == records.c.account_id,
        sa.func.coalesce(inputs.c.owner_execution_id, inputs.c.execution_id).is_not_distinct_from(
            records.c.execution_id
        ),
        sa.or_(inputs.c.execution_id.is_(None), inputs.c.execution_id == records.c.execution_id),
        asset_snapshot.c.path == "/after_account_assets",
        asset_snapshot.c.scope == "after_pending",
        asset_binding.c.scope == "after",
    )
    owner_valid = sa.or_(record_owned, input_owned)
    statement = (
        sa.select(
            asset_binding.c.record_id,
            sa.case((owner_valid, asset_snapshot.c.total_asset), else_=None).label("total_asset"),
            asset_snapshot.c.observation_valid,
            owner_valid.label("owner_valid"),
        )
        .select_from(
            asset_binding.join(asset_snapshot, asset_binding.c.asset_id == asset_snapshot.c.asset_id)
            .join(records, records.c.id == asset_binding.c.record_id)
            .outerjoin(inputs, inputs.c.id == asset_snapshot.c.input_id)
        )
        .where(asset_binding.c.record_id.in_(record_ids), asset_binding.c.path == "/account_assets")
    )
    values = (await session.execute(statement)).all()
    if any(not row.owner_valid for row in values):
        raise ValueError("资产观测属于其他执行或账户")
    return {row.record_id: row.total_asset if row.observation_valid else None for row in values}
