"""在 Python 写边界准备 Rust 绩效响应所需的小型日历事实。"""

from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert

from axile.server.db.models.analysis_calendar import analysis_calendar_day
from axile.server.db.models.execution import ExecuteRecord
from axile.server.performance import local_time
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_day


async def ensure_calendar_facts(session, account_id: int, trade_channel: str | None, batch_size: int = 128) -> int:
    """按有界批次更新观测区间内的渠道日历，不展开任何历史 JSON。

    Parameters
    ----------
    session
        专用异步会话；此函数拥有事务边界，每批提交后释放 SQLite 写锁。
    account_id
        读取规范执行事实的账户编号。
    trade_channel
        账户当前渠道；缺失渠道时不创建事实，读取方按不可用处理。
    batch_size
        每批最多写入的自然日数，取值为 1 到 1000。

    Returns
    -------
    int
        本次已提交的逐日事实数量，包括更新已有日期的数量。

    Notes
    -----
    仅流式读取规范执行时间并转换为上海自然日，确定包含绩效基准日的完整区间。
    日历是外部元数据，每次分析重新评估；不改变账户的业务源版本。
    共享渠道事实只更新本次覆盖日期，不删除其他账户仍可能使用的日期。
    未注册渠道使用 ``unregistered`` 标记，让 Rust 保留旧接口的不可用且无区间语义。
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size 必须在 1 到 1000 之间")
    if trade_channel is None:
        return 0
    # 原始时间允许混合时区，字符串排序不能确定上海自然日边界。
    times = await session.stream_scalars(
        sa.select(ExecuteRecord.created_at)
        .where(ExecuteRecord.account_id == account_id)
        .execution_options(yield_per=batch_size)
    )
    first_day: date | None = None
    last_day: date | None = None
    try:
        async for timestamp in times:
            day = local_time(timestamp).date()
            first_day = day if first_day is None else min(first_day, day)
            last_day = day if last_day is None else max(last_day, day)
    finally:
        await times.close()
    await session.commit()
    if first_day is None or last_day is None:
        return 0
    current, last = first_day, last_day
    written = 0
    terminal_status: str | None = None
    while current <= last:
        rows = []
        while len(rows) < batch_size and current <= last:
            calendar_id = label = None
            if terminal_status is not None:
                status = terminal_status
            else:
                try:
                    decision = evaluate_channel_calendar_day(trade_channel, current)
                except KeyError:
                    # 与 performance_calendar 的未注册渠道降级保持一致。
                    status = terminal_status = "unregistered"
                else:
                    status = decision.status
                    calendar_id, label = decision.calendar_id, decision.label
                    if status is CalendarDecisionStatus.NOT_REQUIRED:
                        status = terminal_status = CalendarDecisionStatus.NOT_REQUIRED
                        calendar_id = label = None
            rows.append(
                {
                    "channel": str(trade_channel),
                    "day": current.isoformat(),
                    "status": str(status),
                    "calendar_id": calendar_id,
                    "label": label,
                }
            )
            current += timedelta(days=1)
        statement = insert(analysis_calendar_day)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[analysis_calendar_day.c.channel, analysis_calendar_day.c.day],
                set_={name: statement.excluded[name] for name in ("status", "calendar_id", "label")},
            ),
            rows,
        )
        await session.commit()
        written += len(rows)
    return written
