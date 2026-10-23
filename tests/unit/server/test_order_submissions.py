"""同账户主程序与脚本共用持久化事实，汇总按发送时间且不重复累计。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from axile.executor.order_submissions import OrderSubmissionFact
from axile.server import order_submissions as submissions
from axile.server._native import _core
from axile.server.api.routes import account_orders as routes
from axile.server.api.routes.account_orders import router
from tests.unit.server._submission_test_support import initialize_submission_database


def test_migration_creates_and_removes_only_submission_table(tmp_path):
    from tests.conftest import initialize_native_database, native_store_call

    path = tmp_path / "migration.db"
    initialize_native_database(path, revision="0031")

    def tables():
        with closing(sqlite3.connect(path)) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    before = tables()
    native_store_call(path, "migration_upgrade", {"public": "0032"})
    assert tables() - before == {"order_submission"}
    with closing(sqlite3.connect(path)) as connection:
        assert [row[2] for row in connection.execute("PRAGMA index_info(ix_order_submission_account_time)")] == [
            "account_id",
            "occurred_at_ms",
        ]
    native_store_call(path, "migration_downgrade", {"public": "0031"})
    assert tables() == before


def test_main_and_script_share_account_day_without_recounting_callbacks(tmp_path, monkeypatch):
    database = str(tmp_path / "submissions.db")
    uri = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setattr(submissions, "settings", SimpleNamespace(sqlalchemy_database_uri=uri))
    monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(database))
    start, end = submissions.submission_window("2026-10-19", "Asia/Shanghai")
    initialize_submission_database(database, monkeypatch)

    async def scenario():
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
            database, boundary.model_copy(update={"revision": 1, "occurred_at_ms": start, "send_status": "accepted"})
        )
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

    asyncio.run(scenario())


@pytest.mark.parametrize(("day", "hours"), [("2026-03-08", 23), ("2026-11-01", 25)])
def test_natural_day_window_handles_dst(day, hours):
    start, end = submissions.submission_window(day, "America/New_York")
    assert end - start == hours * 3600 * 1000


@pytest.mark.parametrize("uri", ["sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///", "postgresql:///main"])
def test_recorder_requires_configured_persistent_sqlite_database(monkeypatch, uri):
    monkeypatch.setattr(submissions, "settings", SimpleNamespace(sqlalchemy_database_uri=uri))
    with pytest.raises(ValueError, match="持久化 SQLite"):
        submissions.create_order_submission_recorder(1)


@pytest.mark.parametrize("path", ["./service.db", "/absolute/service.db", "relative db.sqlite"])
def test_recorder_keeps_exact_service_path_for_native_transaction(monkeypatch, path):
    monkeypatch.setattr(submissions, "settings", SimpleNamespace(sqlalchemy_database_uri=f"sqlite+aiosqlite:///{path}"))
    calls = []
    monkeypatch.setattr(
        submissions, "native_operation_sync", lambda name, payload, **kwargs: calls.append((name, payload, kwargs))
    )
    recorder = submissions.create_order_submission_recorder(1)

    def send():
        assert len(calls) == 1
        assert calls[0][1]["fact"]["send_status"] == "pending"
        assert calls[0][2] == {"database": path}
        return 0

    assert recorder.submit(send, symbol="ag", order_id="order", request_id=1) == 0
    assert len(calls) == 2
    assert {name for name, _, _ in calls} == {"save_order_submission"}
    assert calls[-1][1]["fact"]["send_status"] == "accepted"
    assert calls[-1][1]["fact"]["revision"] == 1


def test_monitor_publishes_native_facts_separately_from_allowance(monkeypatch):
    from axile.executor.algorithms.utils.clock import clock_now_ms
    from tests.unit.server.test_account_control_monitor import database, used

    async def scenario():
        async with database(monkeypatch) as (path, _, monitor):
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
            submissions._save_fact(str(path), fact)
            await monitor.refresh()
            frame = monitor.snapshot()["data"][0]
            assert used(frame) == 120
            assert frame["order_submissions"]["recorded"] == 1
            assert frame["order_submissions"]["sync_accepted"] == 1

    asyncio.run(scenario())
