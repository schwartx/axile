"""同账户主程序与脚本共用持久化事实，汇总按发送时间且不重复累计。"""

import asyncio
import json
from importlib import import_module
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.executor.order_submissions import OrderSubmissionFact
from axile.server import order_submissions as submissions
from axile.server._native import _core
from axile.server.api.deps import get_db
from axile.server.api.routes import account_orders as routes
from axile.server.api.routes.account_orders import router
from tests.unit.server._execution_test_support import build_account


def test_migration_creates_and_removes_only_submission_table(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    revision = import_module("axile.server.alembic.versions.0032_order_submission")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE account (id INTEGER PRIMARY KEY)")
        monkeypatch.setattr(revision, "op", Operations(MigrationContext.configure(connection)))
        revision.upgrade()
        assert "order_submission" in inspect(connection).get_table_names()
        assert inspect(connection).get_indexes("order_submission")[0]["column_names"] == [
            "account_id",
            "occurred_at_ms",
        ]
        revision.downgrade()
        assert inspect(connection).get_table_names() == ["account"]
    engine.dispose()


def test_main_and_script_share_account_day_without_recounting_callbacks(tmp_path, monkeypatch):
    database = str(tmp_path / "submissions.db")
    uri = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setattr(submissions, "settings", SimpleNamespace(sqlalchemy_database_uri=uri))
    monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(database))
    start, end = submissions.submission_window("2026-10-19", "Asia/Shanghai")

    async def scenario():
        engine = create_async_engine(uri)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(build_account(id=1, portfolio_id=None))
                session.add(build_account(id=2, portfolio_id=None))
                await session.commit()

            def fact(uid, status, timestamp, account_id=1, source="script"):
                return OrderSubmissionFact(
                    submission_uid=uid,
                    account_id=account_id,
                    source=source,
                    symbol="ag2612",
                    order_id=uid,
                    request_id=1,
                    occurred_at_ms=timestamp,
                    send_status=status,
                )

            main = fact("main", "accepted", start, source="algorithm")
            rejected = main.model_copy(update={"revision": 2, "broker_status": "rejected", "error_id": 31})
            for row in [main, rejected, main, rejected]:
                submissions._save_fact(database, row)
            for row in [
                fact("script", "rejected", start + 1),
                fact("pending", "pending", start + 2),
                fact("exception", "unknown", start + 3),
                fact("old", "accepted", start - 1),
                fact("tomorrow", "accepted", end),
                fact("another-account", "accepted", start, account_id=2),
            ]:
                submissions._save_fact(database, row)

            async with factory() as session:
                summary = _core.order_submission_summary(_core.Store(database), 1, start, end)
                assert summary["recorded"] == 4
                assert summary["sync_accepted"] == summary["sync_rejected"] == summary["broker_rejected"] == 1
                assert summary["unknown"] == 2
                rows = json.loads(
                    _core.order_submissions(_core.Store(database), 1, "2026-10-19", "Asia/Shanghai", start, end, 1)
                )["data"]
                assert len(rows) == 1
                all_rows = json.loads(
                    _core.order_submissions(_core.Store(database), 1, "2026-10-19", "Asia/Shanghai", start, end, 100)
                )["data"]
                assert next(row for row in all_rows if row["submission_uid"] == "main")["revision"] == 2

            app = FastAPI()
            app.include_router(router, prefix="/account")

            async def get_session():
                async with factory() as session:
                    yield session

            app.dependency_overrides[get_db] = get_session
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/account/1/orders/submissions?control_date=2026-10-19&limit=1")
                assert response.status_code == 200
                assert response.json()["summary"]["recorded"] == 4
                assert len(response.json()["data"]) == 1
                assert response.json()["timezone"] == "Asia/Shanghai"
                assert (await client.get("/account/99/orders/submissions?control_date=2026-10-19")).status_code == 404
                assert (await client.get("/account/1/orders/submissions?control_date=2026-02-31")).status_code == 422
                assert (
                    await client.get("/account/1/orders/submissions?control_date=2026-10-19&limit=501")
                ).status_code == 422

            recorder = submissions.create_order_submission_recorder(1)
            monkeypatch.setattr("axile.executor.order_submissions.clock_now_ms", lambda: start + 4)
            recorder.submit(lambda: 0, symbol="ag2612", order_id="raw-script", request_id=9)
            recorder.broker_result("raw-script", rejected=False)
            boundary = fact("boundary", "pending", start - 1)
            submissions._save_fact(database, boundary)
            submissions._save_fact(
                database,
                boundary.model_copy(update={"revision": 1, "occurred_at_ms": start, "send_status": "accepted"}),
            )
            async with factory() as session:
                rows = json.loads(
                    _core.order_submissions(_core.Store(database), 1, "2026-10-19", "Asia/Shanghai", start, end, 100)
                )["data"]
                raw = next(row for row in rows if row["order_id"] == "raw-script")
                assert raw["source"] == "script" and raw["broker_status"] == "acknowledged"
                assert any(row["submission_uid"] == "boundary" for row in rows)
                summary = _core.order_submission_summary(_core.Store(database), 1, start, end)
                assert summary["recorded"] == 6
                assert summary["sync_accepted"] == 3
                assert summary["broker_acknowledged"] == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(("day", "hours"), [("2026-03-08", 23), ("2026-11-01", 25)])
def test_natural_day_window_handles_dst(day, hours):
    start, end = submissions.submission_window(day, "America/New_York")
    assert end - start == hours * 3600 * 1000


def test_monitor_publishes_native_facts_separately_from_allowance(monkeypatch):
    from axile.executor.algorithms.utils.clock import clock_now_ms
    from axile.server.db.models.order_submission import OrderSubmission
    from tests.unit.server.test_account_control_monitor import database, used

    async def scenario():
        async with database(monkeypatch) as (factory, _, monitor):
            fact = OrderSubmissionFact(
                submission_uid="script-call",
                account_id=1,
                source="script",
                symbol="ag2612",
                order_id="script-order",
                request_id=1,
                occurred_at_ms=clock_now_ms(),
                send_status="accepted",
            )
            async with factory() as session:
                session.add(
                    OrderSubmission(
                        submission_uid=fact.submission_uid,
                        account_id=fact.account_id,
                        occurred_at_ms=fact.occurred_at_ms,
                        revision=fact.revision,
                        data=fact.model_dump(mode="json"),
                    )
                )
                await session.commit()
            await monitor.refresh()
            frame = monitor.snapshot()["data"][0]
            assert used(frame) == 120
            assert frame["order_submissions"]["recorded"] == 1
            assert frame["order_submissions"]["sync_accepted"] == 1

    asyncio.run(scenario())
