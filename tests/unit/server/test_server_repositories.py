"""组合绑定仓储查询测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.db.models import Account, Portfolio, PortfolioAccount
from axile.server.repositories import get_latest_account_id_by_portfolio_id, get_portfolios_every_account

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

            assert await get_portfolios_every_account(session) == {first.id: None, second.id: portfolio.id}
            assert await get_latest_account_id_by_portfolio_id(session, portfolio.id) == second.id

    asyncio.run(scenario())


def test_recent_execution_records_keep_each_accounts_newest_ids() -> None:
    from axile.server.db.models import ExecuteRecord
    from axile.server.repositories import get_recent_execute_records_for_accounts

    async def scenario():
        async with _session_scope() as session:
            first, second = _account("first"), _account("second")
            session.add_all([first, second])
            await session.flush()
            expected = {}
            for account in (first, second):
                rows = [
                    ExecuteRecord(
                        account_id=account.id,
                        is_success=i % 2,
                        raw_input={"marker": i},
                        raw_result={"status": "SUCCEEDED"},
                        created_at="2026-10-01T00:00:00",
                    )
                    for i in range(8)
                ]
                session.add_all(rows)
                await session.flush()
                expected[account.id] = [row.id for row in reversed(rows[-3:])]
            await session.commit()
            actual = await get_recent_execute_records_for_accounts(session, [second.id, first.id, 999], 3)
            assert {key: [row.id for row in rows] for key, rows in actual.items()} == expected
            assert all([row.raw_input["marker"] for row in rows] == [7, 6, 5] for rows in actual.values())
            assert await get_recent_execute_records_for_accounts(session, [], 3) == {}
            assert await get_recent_execute_records_for_accounts(session, [first.id], 0) == {}

    asyncio.run(scenario())


def test_latest_assets_stop_checking_history_after_finding_valid_rows(monkeypatch) -> None:
    import sqlalchemy as sa

    from axile.server import repositories
    from axile.server.db.models import AccountAssetSnapshot

    async def scenario():
        async with _session_scope() as session:
            first, second = _account("first"), _account("second")
            session.add_all([first, second])
            await session.flush()
            expected = {}
            for account in (first, second):
                rows = [
                    AccountAssetSnapshot(account_id=account.id, assets={"total_asset": i}, source="manual")
                    for i in range(80)
                ]
                session.add_all(rows)
                await session.flush()
                expected[account.id] = rows[-1].id
                session.add(
                    AccountAssetSnapshot(
                        account_id=account.id, assets={"total_asset": 0, "source": "unavailable"}, source="manual"
                    )
                )
            await session.commit()
            visits = []

            def count_visit(identity):
                visits.append(identity)
                return 1

            connection = await session.connection()
            await connection.run_sync(
                lambda conn: conn.connection.dbapi_connection.create_function("count_snapshot_visit", 1, count_visit)
            )
            condition = repositories.valid_asset_snapshot_condition
            monkeypatch.setattr(
                repositories,
                "valid_asset_snapshot_condition",
                lambda: sa.and_(sa.func.count_snapshot_visit(AccountAssetSnapshot.id) == 1, condition()),
            )
            actual = await repositories.get_recent_account_asset_snapshots_for_accounts(
                session, [second.id, first.id, first.id], 1
            )
            assert {key: rows[0].id for key, rows in actual.items()} == expected
            assert len(visits) <= 4
            assert await repositories.get_recent_account_asset_snapshots_for_accounts(session, [], 1) == {}
            assert await repositories.get_recent_account_asset_snapshots_for_accounts(session, [first.id], 0) == {}
            assert await repositories.get_recent_account_asset_snapshots_for_accounts(session, [first.id], -1) == {}

    asyncio.run(scenario())


def test_latest_asset_query_batches_many_accounts_without_duplicates() -> None:
    import sqlalchemy as sa

    from axile.server.db.models import AccountAssetSnapshot
    from axile.server.repositories import get_recent_account_asset_snapshots_for_accounts

    async def scenario():
        async with _session_scope() as session:
            accounts = [_account(f"account-{i}") for i in range(101)]
            session.add_all(accounts)
            await session.flush()
            session.add_all(
                [
                    AccountAssetSnapshot(account_id=account.id, assets={"total_asset": 100}, source="manual")
                    for account in accounts
                ]
            )
            await session.commit()
            statements = []
            engine = session.bind.sync_engine

            def capture(_conn, _cursor, statement, _params, _context, _many):
                statements.append(statement)

            sa.event.listen(engine, "before_cursor_execute", capture)
            try:
                ids = [account.id for account in accounts]
                result = await get_recent_account_asset_snapshots_for_accounts(session, ids + ids + [999], 1)
            finally:
                sa.event.remove(engine, "before_cursor_execute", capture)
            assert set(result) == set(ids)
            assert all(len(rows) == 1 for rows in result.values())
            assert len(statements) == 2

    asyncio.run(scenario())
