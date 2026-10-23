"""独立 Python oracle：测试按单执行恢复事实，与原生直接查询比较。"""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from axile.server.business_facts.repository import read_execution_detail
from axile.server.db.models.execution import ExecuteRecord, ExecuteRecordPublic
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.performance_costs import project_execution


class ExecuteRecordSummaryPublic(ExecuteRecordPublic):
    """历史列表响应契约。"""

    symbol_results: dict[str, dict[str, Any]] | None = None
    trade_count: int | None = None
    symbol_count: int | None = None


class ExecuteRecordDetailPublic(ExecuteRecordSummaryPublic):
    """历史详情响应契约。"""

    input: Any
    result: Any


async def read_execution_summaries(
    session: AsyncSession, records: Sequence[ExecuteRecord]
) -> dict[int, ExecuteRecordSummaryPublic]:
    """测试专用参考实现，不依赖已删除的分析输入表。"""
    result = {}
    for record in records:
        if record.id is None:
            continue
        detail = await read_execution_detail(session, record)
        raw = normalize_legacy_result(detail["result"])
        payload, _ = project_execution(SimpleNamespace(**detail))
        symbols = raw.get("symbol_results") if isinstance(raw, dict) else None
        summary = ExecuteRecordSummaryPublic.model_validate(record)
        result[record.id] = summary.model_copy(
            update={
                "symbol_results": {
                    symbol: {
                        key: item[key]
                        for key in ("status", "error", "outcome", "outcome_reason", "reason_code")
                        if key in item
                    }
                    if isinstance(item, dict)
                    else {}
                    for symbol, item in symbols.items()
                }
                if isinstance(symbols, dict)
                else None,
                "trade_count": payload["tradeCount"],
                "symbol_count": payload["symbolCount"],
            }
        )
    return result


async def read_projected_trades(session: AsyncSession, record_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """旧成本算法仅作为独立测试oracle，逐执行读取。"""
    result = {}
    for record_id in record_ids:
        record = await session.get(ExecuteRecord, record_id)
        if record is not None:
            detail = await read_execution_detail(session, record)
            _, result[record_id] = project_execution(SimpleNamespace(**detail))
    return result
