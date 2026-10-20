"""在 Python 写边界冻结收益语义，供 Rust 直接扫描标量事实。"""

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import SQLModel, col

from axile.server.db.models import TargetWeightSnapshot
from axile.server.db.models.analysis_replay import replay_observation as observations
from axile.server.db.models.analysis_replay import replay_observation_symbol as symbols


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    """一次有界补齐的结果；调用者提交后可在下次调用继续。"""

    processed: int
    complete: bool


def observation_fact_values(record: Any, fallback_weights: dict[str, float] | None = None) -> tuple[dict, list[dict]]:
    """保留现有观测口径，把可空映射转为存在位及逐品种标量。

    Notes
    -----
    同时刻不同执行仍分别存储，Rust 读取时按时间、记录编号排序并取最后一条。
    固定微秒精度使 SQLite 文本索引与本地时间顺序一致，不依赖源时区写法。
    """
    # 活写入与历史补齐复用同一转换，避免另写一套资产可信度和数量换算规则。
    from axile.server.performance import observation

    if record.id is None:
        raise ValueError("执行记录必须先 flush 分配 id，才能写入收益事实")
    item = observation(record, fallback_weights)
    maps = {
        "target": item.target,
        "price": item.prices,
        "target_weight": item.target_weight,
        "target_units": item.target_units,
        "target_quantity": item.target_quantities,
    }
    scalar = {
        "record_id": record.id,
        "account_id": record.account_id,
        "execution_id": item.execution_id,
        "time": item.time.isoformat(timespec="microseconds"),
        "asset": item.asset,
        "sizing_fallback": item.sizing_fallback,
        "target_present": item.target is not None,
        "target_weight_present": item.target_weight is not None,
        "target_units_present": item.target_units is not None,
        "target_quantities_present": item.target_quantities is not None,
    }
    names = set().union(*(mapping.keys() for mapping in maps.values() if mapping is not None))
    rows = [
        {
            "record_id": record.id,
            "symbol": symbol,
            **{key: (mapping or {}).get(symbol) for key, mapping in maps.items()},
        }
        for symbol in sorted(names)
    ]
    return scalar, rows


async def _write_values(session, scalar: dict, rows: list[dict]) -> None:
    """替换一个观测的全部事实，不在内部提交事务。"""
    statement = insert(observations).values(**scalar)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[observations.c.record_id],
            set_={key: statement.excluded[key] for key in scalar if key != "record_id"},
        )
    )
    await session.execute(symbols.delete().where(symbols.c.record_id == scalar["record_id"]))
    if rows:
        await session.execute(symbols.insert(), rows)


async def write_observation_fact(session, record: Any, fallback_weights: dict[str, float] | None = None) -> None:
    """在调用方事务中写入观测，未传回退权重时查询同次执行目标。

    Parameters
    ----------
    session
        当前异步事务会话；此函数不提交、不刷新执行记录。
    record
        已分配 ``id`` 的执行记录，包含原始输入、结果和账户编号。
    fallback_weights
        可选的冻结目标权重；显式空字典代表合法的空目标。
    """
    # 先让原始事实的 UPDATE 和失效触发器执行，避免提交时删掉刚生成的投影。
    await session.flush()
    if fallback_weights is None and record.execution_id:
        fallback_weights = (
            await session.execute(
                sa.select(col(TargetWeightSnapshot.normalized_weights)).where(
                    col(TargetWeightSnapshot.account_id) == record.account_id,
                    col(TargetWeightSnapshot.execution_id) == record.execution_id,
                )
            )
        ).scalar_one_or_none()
    scalar, rows = observation_fact_values(record, fallback_weights)
    await _write_values(session, scalar, rows)


async def backfill_observation_facts(
    session, account_id: int, batch_size: int = 100, *, through_record_id: int | None = None
) -> BackfillProgress:
    """补齐一批缺失历史事实；重复调用可恢复，内存不随总历史增长。

    Parameters
    ----------
    session
        调用方会话；调用方须持有 SQLite 写事务，以免并发更新被旧投影覆盖。
        每批提交由调用方控制，以免长读事务阻碍 WAL 回收。
    account_id
        待补齐账户。
    batch_size
        单批原始执行载荷数量，必须在 1 到 1000 之间。
    through_record_id
        可选的执行编号上界，固定单次任务的历史读取范围。

    Returns
    -------
    BackfillProgress
        本批写入条数，以及当前事务视图中是否全部完成。
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size 必须在 1 到 1000 之间")
    records = SQLModel.metadata.tables["executerecord"]
    targets = SQLModel.metadata.tables["target_weight_snapshot"]
    missing = ~sa.exists(sa.select(1).where(observations.c.record_id == records.c.id))
    conditions = [records.c.account_id == account_id, missing]
    if through_record_id is not None:
        conditions.append(records.c.id <= through_record_id)
    statement = (
        sa.select(
            records.c.id,
            records.c.account_id,
            records.c.execution_id,
            records.c.created_at,
            records.c.raw_input,
            records.c.raw_result,
            targets.c.normalized_weights.label("fallback_weights"),
        )
        .select_from(
            records.outerjoin(
                targets,
                sa.and_(targets.c.account_id == records.c.account_id, targets.c.execution_id == records.c.execution_id),
            )
        )
        .where(*conditions)
        .order_by(records.c.id)
        .limit(batch_size)
    )
    batch = (await session.execute(statement)).all()
    for record in batch:
        scalar, rows = observation_fact_values(record, record.fallback_weights)
        await _write_values(session, scalar, rows)
    remaining = await session.scalar(sa.select(sa.exists().where(*conditions)))
    return BackfillProgress(processed=len(batch), complete=not remaining)


async def ensure_observation_facts(session, account_id: int, batch_size: int = 100) -> int:
    """按批提交历史补齐；业务发布仍须检查源版本，不能把补齐当作业务快照。

    Notes
    -----
    此函数拥有传入会话的事务边界，应使用专门的短生命周期会话调用。
    先固定最大编号，避免持续新增执行导致初始补齐永远追赶写入端。
    每批显式申请写事务，保证旧 JSON 读取和投影写入之间没有并发业务修改。
    中途取消或崩溃只丢弃当前未提交批次，下次继续查找缺失事实。
    """
    records = SQLModel.metadata.tables["executerecord"]
    upper = await session.scalar(sa.select(sa.func.max(records.c.id)).where(records.c.account_id == account_id))
    await session.commit()
    if upper is None:
        return 0
    processed = 0
    while True:
        await session.execute(sa.text("BEGIN IMMEDIATE"))
        progress = await backfill_observation_facts(session, account_id, batch_size, through_record_id=upper)
        await session.commit()
        processed += progress.processed
        if progress.complete:
            return processed
