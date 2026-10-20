"""执行记录 API 的显式详情和有界列表投影。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from axile.server.db.models.analysis_source import source_execution, source_execution_symbol, source_trade
from axile.server.db.models.execution import ExecuteRecord, ExecuteRecordPublic


class ExecuteRecordSummaryPublic(ExecuteRecordPublic):
    """执行列表仅公开头字段与已投影的结论，不加载输入和逐单证据。"""

    symbol_results: dict[str, dict[str, Any]] | None = None
    trade_count: int | None = None
    symbol_count: int | None = None


class ExecuteRecordSummaryListPublic(SQLModel):
    """执行摘要的分页响应。"""

    data: list[ExecuteRecordSummaryPublic] = Field(default_factory=list)
    count: int


class ExecuteRecordDetailPublic(ExecuteRecordSummaryPublic):
    """显式读取一次执行的完整业务事实；不存在的输入保持缺失。"""

    input: Any
    result: Any


_SYMBOL_FIELDS = ("status", "error", "outcome", "outcome_reason", "reason_code")


async def read_execution_summaries(
    session: AsyncSession,
    records: Sequence[ExecuteRecord],
) -> dict[int, ExecuteRecordSummaryPublic]:
    """用两次批量标量查询补充当前页，绝不调用完整业务事实重建。"""
    ids = [record.id for record in records if record.id is not None]
    if not ids:
        return {}
    sources = {
        row["record_id"]: row
        for row in (
            await session.execute(
                select(
                    source_execution.c.record_id,
                    source_execution.c.projection_error,
                    source_execution.c.has_symbol_results,
                    source_execution.c.trade_count,
                    source_execution.c.symbol_count,
                ).where(source_execution.c.record_id.in_(ids))
            )
        ).mappings()
    }
    symbols: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    rows = (
        await session.execute(
            select(source_execution_symbol)
            .where(source_execution_symbol.c.record_id.in_(ids))
            .order_by(source_execution_symbol.c.record_id, source_execution_symbol.c.symbol)
        )
    ).mappings()
    for row in rows:
        symbols[row["record_id"]][row["symbol"]] = {
            name: row[name] for index, name in enumerate(_SYMBOL_FIELDS) if row["present"] & (1 << index)
        }
    result: dict[int, ExecuteRecordSummaryPublic] = {}
    for record in records:
        if record.id is None:
            continue
        summary = ExecuteRecordSummaryPublic.model_validate(record)
        source = sources.get(record.id)
        if source is not None and source["projection_error"] is None:
            summary = summary.model_copy(
                update={
                    "symbol_results": symbols.get(record.id, {}) if source["has_symbol_results"] else None,
                    "trade_count": source["trade_count"],
                    "symbol_count": source["symbol_count"],
                }
            )
        result[record.id] = summary
    return result


def projected_trade(row: Any) -> dict[str, Any]:
    """把结构化成交投影列映射为已有成本展示契约。"""
    return {
        "symbol": row["symbol"],
        "day": row["day"],
        "time": row["time"],
        "timeEstimated": row["estimated"],
        "side": row["side"],
        "quantity": row["quantity"],
        "price": row["price"],
        "reference": row["reference"],
        "referenceSource": row["reference_source"],
        "value": row["value"],
        "cost": row["cost"],
        "lossBp": row["loss_bp"],
        "fee": row["fee"],
        "feeCurrency": row["fee_currency"],
    }


async def read_projected_trades(
    session: AsyncSession,
    record_ids: Sequence[int],
) -> dict[int, list[dict[str, Any]]]:
    """只读取当前活动页的成本列，不读取订单、输入、行情或扩展。"""
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if not record_ids:
        return result
    rows = (
        await session.execute(
            select(source_trade)
            .where(source_trade.c.record_id.in_(record_ids))
            .order_by(source_trade.c.record_id, source_trade.c.trade_index)
        )
    ).mappings()
    for row in rows:
        result[row["record_id"]].append(projected_trade(row))
    return result
