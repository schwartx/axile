"""从规范业务事实补齐分析投影，并按有界标量批次读取 Python 分析历史。"""

from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.analysis_cost_reader import execution_payload, trade_payload
from axile.server.db.models.analysis_replay import replay_observation, replay_observation_symbol
from axile.server.db.models.analysis_source import (
    source_attempt,
    source_execution,
    source_execution_symbol,
    source_position,
    source_trade,
    source_transaction,
    source_transaction_fee,
)
from axile.server.performance import Observation, local_time


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    """一次活写入或显式冷重建使用的输入，不绑定 ORM 隐式文档属性。"""

    id: int
    account_id: int
    execution_id: str | None
    created_at: str
    is_success: int
    input: object
    result: object

    @classmethod
    def from_detail(cls, detail: Mapping[str, Any]) -> "AnalysisRecord":
        """接收规范业务仓库显式组装的单条详情。"""
        return cls(**{name: detail[name] for name in cls.__dataclass_fields__})


async def read_analysis_record(session, record: Any) -> AnalysisRecord:
    """仅在缺失派生事实时，显式组装一条规范业务记录。"""
    from axile.server.business_facts.repository import read_execution_detail

    return AnalysisRecord.from_detail(await read_execution_detail(session, record))


async def _children(session, record_ids: list[int]) -> dict[int, dict[str, list]]:
    """每个子表只读取当前批次，不查询业务扩展或完整详情。"""
    children: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for name, table, ordering in (
        ("symbols", source_execution_symbol, source_execution_symbol.c.symbol),
        ("positions", source_position, source_position.c.idx),
        ("attempts", source_attempt, source_attempt.c.idx),
        ("transactions", source_transaction, source_transaction.c.idx),
        ("fees", source_transaction_fee, source_transaction_fee.c.transaction_idx),
    ):
        result = await session.execute(
            sa.select(table).where(table.c.record_id.in_(record_ids)).order_by(table.c.record_id, ordering)
        )
        for row in result.mappings():
            children[row["record_id"]][name].append(row)
    return children


async def _observations(session, record_ids: list[int]) -> dict[int, Observation]:
    """还原已冻结的回放事实，存在位保留未知目标和空目标的区别。"""
    symbol_rows: dict[int, list] = defaultdict(list)
    rows = await session.execute(
        sa.select(replay_observation_symbol)
        .where(replay_observation_symbol.c.record_id.in_(record_ids))
        .order_by(replay_observation_symbol.c.record_id, replay_observation_symbol.c.symbol)
    )
    for row in rows.mappings():
        symbol_rows[row["record_id"]].append(row)
    result = await session.execute(sa.select(replay_observation).where(replay_observation.c.record_id.in_(record_ids)))
    output = {}
    for row in result.mappings():
        symbols = symbol_rows[row["record_id"]]

        def values(name):
            return {item["symbol"]: item[name] for item in symbols if item[name] is not None}

        output[row["record_id"]] = Observation(
            row["record_id"],
            row["execution_id"],
            local_time(row["time"]),
            row["asset"],
            values("target") if row["target_present"] else None,
            values("price"),
            values("target_weight") if row["target_weight_present"] else None,
            row["sizing_fallback"],
            values("target_units") if row["target_units_present"] else None,
            values("target_quantity") if row["target_quantities_present"] else None,
        )
    return output


@dataclass(frozen=True, slots=True)
class AnalysisHistoryBatch:
    """有界标量批次，不持有规范业务详情或 ORM 实例。"""

    headers: list[Mapping[str, Any]]
    payloads: list[dict]
    trades: list[Mapping[str, Any]]
    observations: dict[int, Observation]


async def iter_analysis_observations(
    session, account_id: int, *, batch_size: int = 100
) -> AsyncIterator[list[Observation]]:
    """只读取账户权益回退所需的回放标量，省去订单与成本预览关联。"""
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    records = SQLModel.metadata.tables["executerecord"]
    mismatch = await session.scalar(
        sa.select(
            sa.exists(
                sa.select(1)
                .select_from(records.outerjoin(replay_observation, replay_observation.c.record_id == records.c.id))
                .where(
                    records.c.account_id == account_id,
                    sa.or_(
                        replay_observation.c.record_id.is_(None),
                        replay_observation.c.account_id.is_distinct_from(records.c.account_id),
                        replay_observation.c.execution_id.is_distinct_from(records.c.execution_id),
                    ),
                )
            )
        )
    )
    if mismatch:
        raise ValueError("Canonical execution observation facts are missing or stale")
    after = -1
    while True:
        rows = (
            await session.execute(
                sa.select(replay_observation.c.record_id, records.c.created_at)
                .join(records, records.c.id == replay_observation.c.record_id)
                .where(replay_observation.c.account_id == account_id, replay_observation.c.record_id > after)
                .order_by(replay_observation.c.record_id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            return
        items = await _observations(session, [row.record_id for row in rows])
        if any(items[row.record_id].time != local_time(row.created_at) for row in rows):
            raise ValueError("Execution observation time changed during analysis preparation")
        yield [items[row.record_id] for row in rows]
        after = rows[-1].record_id


async def iter_analysis_history(
    session, account_id: int, *, include_observations: bool = True, batch_size: int = 100
) -> AsyncIterator[AnalysisHistoryBatch]:
    """顺序读取结构化分析事实，限制每批表头及关联查询的记录数量。"""
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    records = SQLModel.metadata.tables["executerecord"]
    mismatch = await session.scalar(
        sa.select(
            sa.exists(
                sa.select(1)
                .select_from(records.outerjoin(source_execution, source_execution.c.record_id == records.c.id))
                .where(
                    records.c.account_id == account_id,
                    sa.or_(
                        source_execution.c.record_id.is_(None),
                        source_execution.c.projection_version != 1,
                        source_execution.c.account_id.is_distinct_from(records.c.account_id),
                        source_execution.c.execution_id.is_distinct_from(records.c.execution_id),
                        source_execution.c.created_at.is_distinct_from(records.c.created_at),
                        source_execution.c.is_success.is_distinct_from(records.c.is_success),
                    ),
                )
            )
        )
    )
    if mismatch:
        raise ValueError("Canonical execution analysis facts are missing, unsupported or stale")
    after = -1
    while True:
        headers = (
            (
                await session.execute(
                    sa.select(source_execution)
                    .where(source_execution.c.account_id == account_id, source_execution.c.record_id > after)
                    .order_by(source_execution.c.record_id)
                    .limit(batch_size)
                )
            )
            .mappings()
            .all()
        )
        if not headers:
            return
        if any(row["projection_version"] != 1 for row in headers):
            raise ValueError("Canonical execution contains unsupported analysis facts")
        record_ids = [row["record_id"] for row in headers]
        children = await _children(session, record_ids)
        trades = (
            (
                await session.execute(
                    sa.select(source_trade)
                    .where(source_trade.c.record_id.in_(record_ids))
                    .order_by(source_trade.c.record_id, source_trade.c.trade_index)
                )
            )
            .mappings()
            .all()
        )
        counts = Counter(row["record_id"] for row in trades)
        if any(row["trade_count"] != counts[row["record_id"]] for row in headers):
            raise ValueError("Execution trade facts changed during analysis preparation")
        observations = await _observations(session, record_ids) if include_observations else {}
        if include_observations and len(observations) != len(headers):
            raise ValueError("Execution observation facts changed during analysis preparation")
        if include_observations and any(
            observations[row["record_id"]].execution_id != row["execution_id"]
            or observations[row["record_id"]].time != local_time(row["created_at"])
            for row in headers
        ):
            raise ValueError("Execution observation identity changed during analysis preparation")
        yield AnalysisHistoryBatch(
            headers,
            [execution_payload(row, children[row["record_id"]]) for row in headers],
            [{**row, "payload": trade_payload(row)} for row in trades],
            observations,
        )
        after = record_ids[-1]
