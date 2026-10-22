"""组合绑定仓储查询测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.api.routes.portfolio import delete_portfolio
from axile.server.db.models import Account, Portfolio, PortfolioAccount
from axile.server.repositories import get_latest_account_id_by_portfolio_id

CODE = "def calculate_portfolio(context):\n    return {}\n"


@asynccontextmanager
async def _session_scope() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _account(name: str) -> Account:
    return Account(
        name=name,
        market="demo-market",
        trade_channel="ctp",
        account_control_preset="default",
        account_config={},
        is_started=False,
        cron_expr="",
        brokerage="ctp",
        weight_precision=0.001,
        algorithm={"method": "SINGLE-MAKER"},
    )


def test_latest_portfolio_bindings_are_returned() -> None:
    async def scenario() -> None:
        async with _session_scope() as session:
            portfolio = Portfolio(name="p", market="demo-market", custom_calc_py_code=CODE)
            first = _account("first")
            second = _account("second")
            session.add_all([portfolio, first, second])
            await session.flush()
            session.add_all(
                [
                    PortfolioAccount(account_id=first.id, portfolio_id=portfolio.id),
                    PortfolioAccount(account_id=second.id, portfolio_id=portfolio.id),
                    PortfolioAccount(account_id=first.id, portfolio_id=None),
                ]
            )
            await session.commit()

            assert await get_latest_account_id_by_portfolio_id(session, portfolio.id) == second.id
            with pytest.raises(HTTPException) as error:
                await delete_portfolio(session, portfolio.id)
            assert error.value.status_code == 422
            assert await session.get(Portfolio, portfolio.id) is portfolio

            session.add(PortfolioAccount(account_id=second.id, portfolio_id=None))
            await session.commit()
            assert await get_latest_account_id_by_portfolio_id(session, portfolio.id) is None
            await delete_portfolio(session, portfolio.id)
            assert await session.get(Portfolio, portfolio.id) is None
            with pytest.raises(HTTPException) as error:
                await delete_portfolio(session, portfolio.id)
            assert error.value.status_code == 404

    asyncio.run(scenario())
