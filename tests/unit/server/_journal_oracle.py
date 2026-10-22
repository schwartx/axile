"""迁移前 journal 展示语义，仅用于对照测试。"""

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
