"""账户、组合与执行关系相关的数据库查询辅助函数."""

from typing import Dict, List, Optional, cast

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import and_, col, desc, func, select

from axile.server.asset_observations import valid_asset_snapshot_condition
from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord, Portfolio, PortfolioAccount


async def get_recent_account_asset_snapshots_for_accounts(
    session: AsyncSession,
    account_ids: List[int],
    limit: int = 20,
) -> Dict[int, List[AccountAssetSnapshot]]:
    """批量获取多个账户各自最近的资产快照（最新在前）."""
    if not account_ids:
        return {}
    row_number = (
        func.row_number().over(
            partition_by=col(AccountAssetSnapshot.account_id),
            order_by=desc(col(AccountAssetSnapshot.id)),
        )
    ).label("rn")
    ranked = (
        select(AccountAssetSnapshot, row_number)
        .where(col(AccountAssetSnapshot.account_id).in_(account_ids), valid_asset_snapshot_condition())
        .subquery()
    )
    stmt = select(AccountAssetSnapshot).from_statement(
        select(ranked).where(ranked.c.rn <= limit).order_by(ranked.c.account_id, desc(ranked.c.id))
    )
    grouped: Dict[int, List[AccountAssetSnapshot]] = {}
    for snapshot in (await session.execute(stmt)).scalars().all():
        grouped.setdefault(snapshot.account_id, []).append(snapshot)
    return grouped


async def add_record_portfolio_account(
    session: AsyncSession,
    account_id: int,
    portfolio_id: Optional[int],
) -> None:
    """新增账户/组合记录, portfolio_id为None说明账户解绑组合."""
    if portfolio_id is not None:
        db_portfolio = await session.get(Portfolio, portfolio_id)
        if not db_portfolio:
            raise HTTPException(status_code=404, detail="组合不存在")

    record = PortfolioAccount(
        account_id=account_id,
        portfolio_id=portfolio_id,
    )
    session.add(record)


async def get_latest_account_id_by_portfolio_id(session: AsyncSession, portfolio_id: int) -> Optional[int]:
    """获取最近绑定且当前仍指向该组合的账户 ID."""
    latest_binding_ids = (
        select(func.max(PortfolioAccount.id).label("max_id")).group_by(col(PortfolioAccount.account_id)).subquery()
    )
    return await session.scalar(
        select(PortfolioAccount.account_id)
        .join(latest_binding_ids, col(PortfolioAccount.id) == latest_binding_ids.c.max_id)
        .where(PortfolioAccount.portfolio_id == portfolio_id)
        .order_by(desc(PortfolioAccount.id))
        .limit(1)
    )


async def get_latest_portfolio_id_by_account_id(session: AsyncSession, account_id: int) -> Optional[int]:
    """获取账户的当前组合的id."""
    return await session.scalar(
        select(PortfolioAccount.portfolio_id)
        .where(PortfolioAccount.account_id == account_id)
        .order_by(desc(PortfolioAccount.id))
        .limit(1)
    )


async def get_portfolios_every_account(
    session: AsyncSession,
) -> Dict[int, Optional[int]]:
    """获取每个账户最新的portfolio_id, 返回{account_id: portfolio_id}字典."""
    # 子查询获取每个account_id对应的最大id
    subquery = (
        select(PortfolioAccount.account_id, func.max(PortfolioAccount.id).label("max_id")).group_by(
            col(PortfolioAccount.account_id)
        )
    ).subquery()

    # 主查询关联子查询获取最新记录的portfolio_id
    stmt = select(PortfolioAccount.account_id, PortfolioAccount.portfolio_id).join(
        subquery, and_(PortfolioAccount.id == subquery.c.max_id)
    )

    # 执行查询并转换为字典
    results = (await session.execute(stmt)).all()
    if not results:
        return {}
    return {row[0]: row[1] for row in results}


async def get_latest_success_execute_record_by_account_id(
    session: AsyncSession, account_id: Optional[int]
) -> Optional[ExecuteRecord]:
    """获取账户的最新成功的执行记录."""
    return (
        await session.execute(
            select(ExecuteRecord)
            .where(ExecuteRecord.account_id == cast("int", account_id))
            .where(ExecuteRecord.is_success == 1)
            .order_by(desc(ExecuteRecord.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_recent_execute_records_by_account_id(
    session: AsyncSession, account_id: Optional[int], limit: int = 20
) -> List[ExecuteRecord]:
    """
    获取账户最近若干条执行记录（按 id 倒序，不限成败）.

    Parameters
    ----------
    session : AsyncSession
        当前数据库会话。
    account_id : Optional[int]
        账户 ID。
    limit : int, optional
        返回的最大条数，默认 20。

    Returns
    -------
    List[ExecuteRecord]
        最近的执行记录列表（最新在前）；无记录时为空列表。
    """
    result = await session.execute(
        select(ExecuteRecord)
        .where(ExecuteRecord.account_id == cast("int", account_id))
        .order_by(desc(ExecuteRecord.id))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_recent_execute_records_for_accounts(
    session: AsyncSession, account_ids: List[int], limit: int = 20
) -> Dict[int, List[ExecuteRecord]]:
    """
    批量获取多个账户各自最近若干条执行记录（按 id 倒序）.

    Parameters
    ----------
    session : AsyncSession
        当前数据库会话。
    account_ids : List[int]
        账户 ID 列表。
    limit : int, optional
        每个账户返回的最大条数，默认 20。

    Returns
    -------
    Dict[int, List[ExecuteRecord]]
        账户 ID 到其最近执行记录（最新在前）的映射；无记录的账户不出现在结果中。

    Notes
    -----
    用窗口函数 ``ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY id DESC)``
    一次取回所有账户的近端记录，替代「在账户循环里逐账户查询」的 N+1 写法，
    使仪表盘的查询数不再随账户数线性增长。

    与逐账户版本语义一致：每账户各自取最近 ``limit`` 条，而非全局前 ``limit`` 条。
    """
    if not account_ids:
        return {}

    row_number = (
        func.row_number().over(partition_by=col(ExecuteRecord.account_id), order_by=desc(col(ExecuteRecord.id)))
    ).label("rn")

    ranked = select(ExecuteRecord, row_number).where(col(ExecuteRecord.account_id).in_(account_ids)).subquery()
    stmt = select(ExecuteRecord).from_statement(
        select(ranked).where(ranked.c.rn <= limit).order_by(ranked.c.account_id, desc(ranked.c.id))
    )

    grouped: Dict[int, List[ExecuteRecord]] = {}
    for record in (await session.execute(stmt)).scalars().all():
        grouped.setdefault(record.account_id, []).append(record)
    return grouped
