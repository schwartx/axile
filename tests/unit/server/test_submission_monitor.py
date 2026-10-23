"""真实 SQLite 与 HTTP 验证报撤同口径、到值当笔、重启去重和未知状态。"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from axile.executor.order_submissions import OrderSubmissionFact
from axile.server import order_submissions as submissions
from axile.server import submission_monitor as monitoring
from axile.server._native import _core
from axile.server.api.routes import account_orders as routes
from axile.server.api.routes.account_orders import router
from tests.unit.server._submission_test_support import initialize_submission_database


def test_migration_keeps_existing_submission_facts(tmp_path, monkeypatch):
    from tests.conftest import native_store_call

    path = str(tmp_path / "migration.db")
    initialize_submission_database(path, monkeypatch)
    native_store_call(path, "migration_downgrade", {"public": "0032"})
    with closing(sqlite3.connect(path)) as connection:

        def tables():
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        before = tables()
        connection.execute("INSERT INTO order_submission VALUES ('existing',1,1,0,'{}')")
        connection.commit()
        native_store_call(path, "migration_upgrade", {"public": "0033"})
        assert tables() - before == {"order_submission_monitor", "order_submission_warning"}
        native_store_call(path, "migration_downgrade", {"public": "0032"})
        assert connection.execute("SELECT submission_uid FROM order_submission").fetchone()[0] == "existing"
        assert tables() == before


@pytest.mark.parametrize("concurrent", [False, True])
def test_native_thresholds_persist_at_crossing_and_survive_replay_restart_and_configuration(
    tmp_path, monkeypatch, concurrent
):
    database = str(tmp_path / "monitor.db")
    uri = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setattr(submissions, "settings", SimpleNamespace(sqlalchemy_database_uri=uri))
    monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(database))
    monkeypatch.setattr(routes, "clock_now_ms", lambda: monitoring.clock_now_ms())
    start, end = submissions.submission_window("2026-10-19", "Asia/Shanghai")
    now = [start + 60_000]
    for module in (monitoring, submissions):
        monkeypatch.setattr(module, "clock_now_ms", lambda: now[0])
    monkeypatch.setattr("axile.executor.order_submissions.clock_now_ms", lambda: now[0])
    initialize_submission_database(database, monkeypatch)

    async def scenario():
        app = FastAPI()
        app.include_router(router, prefix="/account")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            async def configure(order, combined):
                result = await client.put(
                    "/account/1/orders/monitor", json={"order_limit": order, "combined_limit": combined}
                )
                assert result.status_code == 200, result.text
                return result.json()

            async def read():
                result = await client.get("/account/1/orders/monitor")
                assert result.status_code == 200, result.text
                return result.json()

            assert (await read())["settings"]["revision"] == 0
            assert (await configure(2, 3))["warnings"] == []

            def send(index, operation="place_order", code=0):
                # 每次新建记录器，验证去重不依赖进程内状态。
                recorder = submissions.create_order_submission_recorder(1)
                recorder.submit(
                    lambda: code,
                    symbol="ag2612",
                    order_id=f"order-{index}",
                    request_id=index,
                    operation=operation,
                    execution_id="execution",
                )
                return recorder

            if concurrent:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(send, range(8)))
                view = await read()
                assert len(view["warnings"]) == 2
                assert {event["metric"]: event["value"] for event in view["warnings"]} == {
                    "order_calls": 2,
                    "combined_calls": 3,
                }
                assert view["metrics"][0]["value"] == view["metrics"][1]["value"] == 8
                return

            first = send(1)
            send(2, "cancel_order", -3)
            assert (await read())["warnings"] == []
            second = send(3)
            second.broker_result("order-3", rejected=True, error_id=31)
            for _ in range(2):
                first.broker_result("order-1", rejected=False)
            view = await read()
            assert [metric["value"] for metric in view["metrics"]] == [2, 3]
            original = view["warnings"]
            assert {event["metric"]: event["value"] for event in original} == {
                "order_calls": 2,
                "combined_calls": 3,
            }
            assert all(event["execution_id"] == "execution" and event["submission_uid"] for event in original)
            send(4, "cancel_order")
            assert (await read())["warnings"] == original
            assert (await configure(2, 3))["settings"]["revision"] == 1
            assert (await client.get("/account/2/orders/monitor")).json()["warnings"] == []
            ledger = (await client.get("/account/1/orders/submissions?control_date=2026-10-19")).json()
            assert ledger["summary"]["recorded"] == ledger["summary"]["cancel_recorded"] == 2
            assert ledger["summary"]["cancel_sync_rejected"] == 1
            assert ledger["summary"]["broker_rejected"] == 1

            unknown = OrderSubmissionFact(
                submission_uid="unknown",
                account_id=1,
                source="script",
                symbol="ag2612",
                order_id="unknown",
                request_id=99,
                occurred_at_ms=now[0],
            )
            submissions._save_fact(database, unknown)
            raised = await configure(100, 100)
            assert all(metric["state"] == "unknown" for metric in raised["metrics"])
            assert raised["warnings"] == []
            lowered = await configure(None, 1)
            assert len(lowered["warnings"]) == 1
            assert lowered["warnings"][0]["value"] == 4
            assert lowered["warnings"][0]["source"] == "configuration"
            disabled = await configure(None, None)
            assert all(metric["state"] == "disabled" for metric in disabled["metrics"])
            assert disabled["warnings"] == []
            assert len((await configure(None, 1))["warnings"]) == 1

            now[0] = end + 60_000
            fresh = await read()
            assert fresh["warnings"] == []
            assert fresh["metrics"][1]["state"] == "below"
            send(5, "cancel_order")
            assert (await read())["warnings"][0]["control_date"] == "2026-10-20"
            assert (await read())["warnings"][0]["value"] == 1
            for invalid in (0, -1, True, 1.2, "3", 1_000_000_001):
                result = await client.put("/account/1/orders/monitor", json={"combined_limit": invalid})
                assert result.status_code == 422
            assert (await client.get("/account/99/orders/monitor")).status_code == 404

    asyncio.run(scenario())
