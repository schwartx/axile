"""Small analysis HTTP reads remain version-bound and independent of legacy JSON."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel

from axile.server.analysis_cost_facts import iter_cost_fact_batches
from axile.server.analysis_results import iter_result_fact_batches
from axile.server.api.deps import get_db
from axile.server.api.routes import account_analysis as routes
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis import (
    cost_attempt,
    cost_execution_symbol,
    cost_position,
    cost_transaction,
    cost_transaction_fee,
)
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from axile.server.performance_costs import timestamp
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_analysis_results import SETTINGS, _range

BASE_URL = "/api/v1/account/performance/2"
START = timestamp("2026-01-01T09:00:00")
PRECISE_VALUE = 1234.56789012345
REBATE = -0.00000123456789
SQLITE_OVERFLOW = 1 << 63


def _projected():
    """Use equal timestamps plus unknown and confirmed-empty position states."""
    output = []
    for record_id in (3, 1, 2):
        when = START + (record_id == 3) * 1000.125
        payload = {
            "key": f"record-{record_id}",
            "record": {
                "id": record_id,
                "execution_id": f"execution-{record_id}",
                "created_at": "2026-01-01T09:00:00.000125+08:00",
                "is_success": 1,
                "raw_result": {
                    "status": "SUCCEEDED",
                    "error": None,
                    "symbol_results": {"A": {"status": "FILLED", "error": None}},
                },
            },
            "noop": record_id != 1,
            "tradeCount": 1 if record_id == 1 else 0,
            "durationSec": 0.123456789,
            "symbolCount": 99,
            "positionCount": 1 if record_id == 1 else 0 if record_id == 3 else None,
            "positions": None if record_id == 2 else [],
            "transactions": [],
            "attempts": [],
            "reason": None,
        }
        if record_id == 1:
            payload["positions"] = [{"symbol": "A", "quantity": 0.000000123456789, "direction": "long"}]
            payload["attempts"] = [{"symbol": "A", "planned": 2, "filled": 0, "reason": None}]
            payload["transactions"] = [
                {
                    "symbol": "A",
                    "side": "buy",
                    "quantity": 0.000000123456789,
                    "price": PRECISE_VALUE,
                    "reference": None,
                    "referenceSource": None,
                    "time": START + 0.125,
                    "endTime": START + 0.25,
                    "timeEstimated": False,
                    "before": 0,
                    "target": None,
                    "summary": {
                        "value": PRECISE_VALUE,
                        "cost": REBATE,
                        "lossBp": None,
                        "coverage": 1,
                        "covered": 1,
                        "count": 1,
                        "amountComplete": True,
                        "feeCovered": 2,
                        "fees": {"USD": 0, "BTC": REBATE},
                    },
                }
            ]
        output.append(
            {"record_id": record_id, "time": when, "success": True, "noop": payload["noop"], "payload": payload}
        )
    return output


def _insert_frozen_views(connection, snapshot_id):
    """Insert results directly so readers cannot derive them from missing trades."""
    statement = sa.text(
        "INSERT INTO performance_execution_summary "
        "(snapshot_id,record_id,time,value,cost,loss_bp,coverage,covered,count,fee_covered,"
        "amount_complete,covered_value,estimated,symbol_count,has_transactions) "
        "VALUES (:snapshot_id,:record_id,:time,:value,:cost,:loss_bp,:coverage,:covered,:count,"
        ":fee_covered,:amount_complete,:covered_value,:estimated,:symbol_count,:has_transactions)"
    )
    for record_id in (1, 2, 3):
        traded = record_id == 1
        connection.execute(
            statement,
            {
                "snapshot_id": snapshot_id,
                "record_id": record_id,
                "time": START + (record_id == 3) * 1000.125,
                "value": PRECISE_VALUE if traded else None,
                "cost": REBATE if traded else None,
                "loss_bp": None,
                "coverage": 1 if traded else None,
                "covered": int(traded),
                "count": int(traded),
                "fee_covered": 2 if traded else 0,
                "amount_complete": True,
                "covered_value": PRECISE_VALUE if traded else 0,
                "estimated": 0,
                "symbol_count": 1 if traded else None,
                "has_transactions": traded,
            },
        )
    connection.execute(
        sa.text(
            "INSERT INTO performance_execution_fee(snapshot_id,record_id,currency,value,count) "
            "VALUES (:snapshot_id,1,:currency,:value,1)"
        ),
        [
            {"snapshot_id": snapshot_id, "currency": "USD", "value": 0},
            {"snapshot_id": snapshot_id, "currency": "BTC", "value": REBATE},
        ],
    )


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "analysis-views.sqlite"
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([build_account(id=2), build_account(id=3)])
            session.commit()
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            for snapshot_id, account_id, view_version in (
                ("s1", 2, 1),
                ("s2", 2, 1),
                ("foreign", 3, 1),
                ("legacy", 2, 0),
            ):
                connection.execute(
                    snapshots.insert().values(
                        id=snapshot_id,
                        account_id=account_id,
                        source_version=2 if snapshot_id == "s2" else 1,
                        logic_version=LOGIC_VERSION,
                        engine_version=ENGINE_VERSION,
                        computed_at="2026-01-03T00:00:00+08:00",
                        data_until="2026-01-02T09:00:00+08:00",
                        settings=SETTINGS,
                        ranges={},
                        cost_schema_version=1,
                        result_schema_version=2,
                        read_view_schema_version=view_version,
                        backtest_weight_type=SETTINGS["backtest_weight_type"],
                        backtest_fee_rate=SETTINGS["backtest_fee_rate"],
                    )
                )
                ranges = {key: _range(key) for key in ("all", "30", "90")}
                for selected in ranges.values():
                    selected["performance"]["engine_version"] = ENGINE_VERSION
                    if snapshot_id == "s2":
                        selected["performance"]["points"][-1]["account_return"] = 0.75
                for table, rows in iter_result_fact_batches(snapshot_id, ranges):
                    connection.execute(table.insert(), rows)
                for table, rows in iter_cost_fact_batches(snapshot_id, _projected(), [], ranges):
                    connection.execute(table.insert(), rows)
                if view_version:
                    _insert_frozen_views(connection, snapshot_id)
            connection.execute(
                states.insert().values(
                    account_id=2,
                    source_version=1,
                    published_version=1,
                    current_snapshot="s1",
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                )
            )
            # Genuine invalid JSON catches ORM column decoding as well as JSON SQL.
            for table, columns in (
                ("performance_snapshot", ("ranges", "settings")),
                ("performance_cost_execution", ("payload",)),
            ):
                for column in columns:
                    connection.exec_driver_sql(f"UPDATE {table} SET {column}='not-json'")
    finally:
        engine.dispose()
    return path


@asynccontextmanager
async def http_client(database):
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    patch = pytest.MonkeyPatch()
    patch.setattr(routes, "analysis_store", lambda: routes._core.Store(str(database)))
    app.include_router(routes.router, prefix="/api/v1/account")

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = session_dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client, sessions, engine
    finally:
        patch.undo()
        await engine.dispose()


def _rows(table):
    assert len(table["columns"]) == len(set(table["columns"]))
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


def _reject_legacy_reads(connection, cursor, statement, parameters, context, executemany):
    normalized = statement.lower().replace('"', "").replace("`", "")
    assert "json_extract" not in normalized and "json_type" not in normalized, statement
    assert ".payload" not in normalized and ".ranges" not in normalized and ".settings" not in normalized, statement
    assert "performance_cost_trade" not in normalized, "read views must not aggregate source trades"
    assert not normalized.lstrip().startswith(("insert ", "update ", "delete ")), "GET must not enqueue refreshes"


def test_chart_uses_small_typed_tables_without_json_or_source_trade_aggregation(dataset, monkeypatch):
    """A poisoned legacy projection cannot affect the independently stored chart."""

    async def check():
        async with http_client(dataset) as (client, _, engine):
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_legacy_reads)
            response = await client.get(f"{BASE_URL}/chart", params={"snapshot_id": "s1", "range": "30"})
            assert response.status_code == 200, response.text
            chart = response.json()
            assert (chart["schema_version"], chart["snapshot_id"], chart["account_id"], chart["range"]) == (
                1,
                "s1",
                2,
                "30",
            )
            assert chart["settings"] == SETTINGS
            assert "executions" not in chart["header"]
            assert _rows(chart["tables"]["points"])[1]["account_return"] == 0.03
            index = _rows(chart["tables"]["execution_index"])
            assert [row["record_id"] for row in index] == [1, 2, 3]
            assert index[0]["symbol_count"] == 1
            assert index[0]["value"] == PRECISE_VALUE
            assert index[0]["cost"] == REBATE
            assert index[0]["has_transactions"] is True
            assert index[1]["value"] is None and index[1]["covered_value"] == 0
            assert index[1]["positions_present"] is False and index[2]["positions_present"] is True
            forbidden = {"positions", "attempts", "transactions", "raw_result", "payload", "symbol_results"}
            assert not forbidden.intersection(chart["tables"]["execution_index"]["columns"])
            fees = _rows(chart["tables"]["execution_fees"])
            assert {row["currency"]: row["value"] for row in fees} == {"BTC": REBATE, "USD": 0}
            daily = _rows(chart["tables"]["daily_costs"])
            assert daily[0]["value"] is None and daily[0]["covered"] == 0
            assert daily[1]["cost"] == 0 and daily[1]["covered_value"] is None
            assert daily[0]["present"] != daily[1]["present"]
            daily_fees = _rows(chart["tables"]["daily_fees"])
            assert {(row["day"], row["currency"]): row["value"] for row in daily_fees} == {
                ("2026-01-02", "CNY"): 0,
                ("2026-01-02", "USD"): 1.5,
            }

    asyncio.run(check())


def test_status_is_small_and_reports_legacy_view_explicitly(dataset):
    async def check():
        async with http_client(dataset) as (client, sessions, engine):
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_legacy_reads)
            response = await client.get(f"{BASE_URL}/status")
            assert response.status_code == 200, response.text
            status = response.json()
            assert status["schema_version"] == 1 and status["snapshot_id"] == "s1"
            assert status["read_view_schema_version"] == 1
            assert not {"result", "tables", "executions", "points"}.intersection(status)
            sa.event.remove(engine.sync_engine, "before_cursor_execute", _reject_legacy_reads)
            async with sessions() as session:
                await session.execute(states.update().where(states.c.account_id == 2).values(current_snapshot="legacy"))
                await session.commit()
            response = await client.get(f"{BASE_URL}/status")
            assert response.status_code == 200
            assert response.json()["read_view_schema_version"] == 0

    asyncio.run(check())


@pytest.mark.parametrize("version_column", ["logic_version", "engine_version"])
@pytest.mark.parametrize("error", ["analysis retries exhausted", None], ids=["failed", "pending"])
def test_incompatible_status_preserves_exhausted_failure_and_pending_without_error(dataset, version_column, error):
    """An incompatible prior snapshot cannot hide a terminal computation error."""

    async def check():
        async with http_client(dataset) as (client, sessions, _):
            async with sessions() as session:
                await session.execute(
                    snapshots.update().where(snapshots.c.id == "s1").values(**{version_column: "old-version"})
                )
                await session.execute(
                    states.update()
                    .where(states.c.account_id == 2)
                    .values(
                        error=error,
                        failures=4,
                        requested=False,
                        running_version=None,
                        running_token=None,
                        retry_at=12345.678,
                    )
                )
                await session.commit()
            response = await client.get(f"{BASE_URL}/status")
            assert response.status_code == 200, response.text
            status = response.json()
            assert status["status"] == ("failed" if error else "pending")
            assert status["error"] == error
            assert status["retry_at"] is None
            assert status["snapshot_id"] is None
            assert status["snapshot_version"] is None
            assert status["read_view_schema_version"] == 0

    asyncio.run(check())


@pytest.mark.parametrize("endpoint", ["chart", "execution-summary", "execution-detail"])
@pytest.mark.parametrize(("snapshot_id", "expected"), [("foreign", 404), ("missing", 410), ("legacy", 409)])
def test_version_validation_never_falls_back_or_crosses_accounts(dataset, endpoint, snapshot_id, expected):
    async def check():
        async with http_client(dataset) as (client, _, _):
            params = {"snapshot_id": snapshot_id}
            if endpoint == "execution-detail":
                params["record_id"] = 1
            response = await client.get(f"{BASE_URL}/{endpoint}", params=params)
            assert response.status_code == expected, response.text
            if snapshot_id == "legacy":
                assert response.json()["detail"]["code"] == "ANALYSIS_READ_VIEW_UNAVAILABLE"

    asyncio.run(check())


def test_explicit_snapshot_is_pinned_after_current_pointer_changes(dataset):
    async def check():
        async with http_client(dataset) as (client, sessions, _):
            before = await client.get(f"{BASE_URL}/chart", params={"snapshot_id": "s1"})
            assert before.status_code == 200, before.text
            async with sessions() as session:
                await session.execute(
                    states.update()
                    .where(states.c.account_id == 2)
                    .values(source_version=2, published_version=2, current_snapshot="s2", previous_snapshot="s1")
                )
                await session.commit()
            status = await client.get(f"{BASE_URL}/status")
            assert status.json()["snapshot_id"] == "s2"
            after = await client.get(f"{BASE_URL}/chart", params={"snapshot_id": "s1"})
            assert after.json() == before.json()
            current = await client.get(f"{BASE_URL}/chart", params={"snapshot_id": "s2"})
            assert _rows(current.json()["tables"]["points"])[1]["account_return"] == 0.75

    asyncio.run(check())


def test_summary_pages_are_stable_and_use_frozen_null_zero_currency_values(dataset):
    async def check():
        async with http_client(dataset) as (client, _, engine):
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_legacy_reads)
            params = {"snapshot_id": "s1", "range": "all", "limit": 2}
            first = await client.get(f"{BASE_URL}/execution-summary", params=params)
            assert first.status_code == 200, first.text
            page = first.json()
            assert [row["record_id"] for row in _rows(page["table"])] == [1, 2]
            assert page["next_offset"] == 2
            fees = _rows(page["fees"])
            assert {row["currency"]: row["value"] for row in fees} == {"BTC": REBATE, "USD": 0}
            second = await client.get(f"{BASE_URL}/execution-summary", params=params | {"offset": 2})
            assert second.status_code == 200
            assert [row["record_id"] for row in _rows(second.json()["table"])] == [3]
            assert second.json()["next_offset"] is None
            assert _rows(second.json()["fees"]) == []
            empty = await client.get(f"{BASE_URL}/execution-summary", params=params | {"offset": 100})
            assert empty.status_code == 200 and _rows(empty.json()["table"]) == []

    asyncio.run(check())


def test_summary_viewport_applies_before_pagination(dataset):
    async def check():
        async with http_client(dataset) as (client, _, _):
            response = await client.get(
                f"{BASE_URL}/execution-summary",
                params={"snapshot_id": "s1", "limit": 1, "start": START + 1, "end": START + 2000},
            )
            assert response.status_code == 200, response.text
            assert [row["record_id"] for row in _rows(response.json()["table"])] == [3]
            assert response.json()["next_offset"] is None

    asyncio.run(check())


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"offset": SQLITE_OVERFLOW},
        {"range": "7"},
        {"start": "nan", "end": "inf"},
        {"start": START + 1, "end": START},
        {"start": START},
    ],
)
def test_summary_rejects_invalid_parameters(dataset, query):
    async def check():
        async with http_client(dataset) as (client, _, _):
            response = await client.get(f"{BASE_URL}/execution-summary", params={"snapshot_id": "s1"} | query)
            assert response.status_code == 422, response.text

    asyncio.run(check())


def test_detail_reads_only_selected_typed_record_with_precision_and_presence(dataset):
    async def check():
        async with http_client(dataset) as (client, _, engine):
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_legacy_reads)
            response = await client.get(f"{BASE_URL}/execution-detail", params={"snapshot_id": "s1", "record_id": 1})
            assert response.status_code == 200, response.text
            detail = response.json()
            assert (detail["schema_version"], detail["snapshot_id"], detail["record_id"]) == (1, "s1", 1)
            assert detail["header"]["positions_present"] is True
            assert detail["header"]["created_at"] == "2026-01-01T09:00:00.000125+08:00"
            assert "payload" not in detail["header"]
            positions = _rows(detail["tables"]["positions"])
            assert len(positions) == 1 and positions[0]["quantity"] == 0.000000123456789
            transactions = _rows(detail["tables"]["transactions"])
            assert len(transactions) == 1 and transactions[0]["price"] == PRECISE_VALUE
            assert transactions[0]["time"] == START + 0.125
            assert transactions[0]["reference"] is None
            fees = _rows(detail["tables"]["transaction_fees"])
            assert {row["currency"]: row["value"] for row in fees} == {"BTC": REBATE, "USD": 0}
            symbols = _rows(detail["tables"]["symbols"])
            assert symbols[0]["status"] == "FILLED" and symbols[0]["error"] is None
            for record_id, present in ((2, False), (3, True)):
                response = await client.get(
                    f"{BASE_URL}/execution-detail", params={"snapshot_id": "s1", "record_id": record_id}
                )
                assert response.status_code == 200
                assert response.json()["header"]["positions_present"] is present
                assert _rows(response.json()["tables"]["positions"]) == []
            missing = await client.get(f"{BASE_URL}/execution-detail", params={"snapshot_id": "s1", "record_id": 999})
            assert missing.status_code == 404

    asyncio.run(check())


@pytest.mark.parametrize("query", [{"record_id": SQLITE_OVERFLOW}, {"offset": SQLITE_OVERFLOW}])
def test_detail_rejects_integers_outside_sqlite_range(dataset, query):
    async def check():
        async with http_client(dataset) as (client, _, _):
            response = await client.get(
                f"{BASE_URL}/execution-detail", params={"snapshot_id": "s1", "record_id": 1} | query
            )
            assert response.status_code == 422, response.text

    asyncio.run(check())


def test_detail_pages_keep_all_evidence_and_only_page_transaction_fees(dataset):
    """Equal-time transactions advance by index and every bounded child has a cursor."""

    async def check():
        async with http_client(dataset) as (client, sessions, engine):
            async with sessions() as session:
                for table in (cost_position, cost_attempt, cost_transaction, cost_execution_symbol):
                    original = dict(
                        (
                            await session.execute(
                                sa.select(table).where(table.c.snapshot_id == "s1", table.c.record_id == 1)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    extra = [dict(original, symbol=chr(65 + index)) for index in (1, 2)]
                    if "idx" in table.c:
                        for index, row in enumerate(extra, 1):
                            row["idx"] = index
                    await session.execute(table.insert(), extra)
                await session.execute(
                    cost_transaction_fee.insert(),
                    [
                        {
                            "snapshot_id": "s1",
                            "record_id": 1,
                            "transaction_idx": index,
                            "currency": "USD",
                            "value": index,
                        }
                        for index in (1, 2)
                    ],
                )
                await session.commit()
            sa.event.listen(engine.sync_engine, "before_cursor_execute", _reject_legacy_reads)
            positions, attempts, transactions, symbols = [], [], [], []
            params = {"snapshot_id": "s1", "record_id": 1, "limit": 1}
            for offset in range(3):
                response = await client.get(f"{BASE_URL}/execution-detail", params=params | {"offset": offset})
                assert response.status_code == 200, response.text
                detail = response.json()
                assert detail["total"] == 3
                expected_next = offset + 1 if offset < 2 else None
                assert detail["next_offset"] == expected_next
                assert detail["next_offsets"] == dict.fromkeys(("positions", "attempts", "symbols"), expected_next)
                positions.extend(_rows(detail["tables"]["positions"]))
                attempts.extend(_rows(detail["tables"]["attempts"]))
                transactions.extend(_rows(detail["tables"]["transactions"]))
                symbols.extend(_rows(detail["tables"]["symbols"]))
                fees = _rows(detail["tables"]["transaction_fees"])
                assert {row["transaction_idx"] for row in fees} == {offset}
            assert [row["idx"] for row in positions] == [0, 1, 2]
            assert [row["idx"] for row in attempts] == [0, 1, 2]
            assert [row["idx"] for row in transactions] == [0, 1, 2]
            assert len({row["time"] for row in transactions}) == 1
            assert [row["symbol"] for row in symbols] == ["A", "B", "C"]

    asyncio.run(check())


@pytest.mark.parametrize("endpoint", ["status", "chart", "execution-summary", "execution-detail"])
def test_routes_reject_account_id_outside_sqlite_range(dataset, endpoint):
    async def check():
        async with http_client(dataset) as (client, _, _):
            params = {"snapshot_id": "s1", "record_id": 1} if endpoint == "execution-detail" else {"snapshot_id": "s1"}
            response = await client.get(f"/api/v1/account/performance/{SQLITE_OVERFLOW}/{endpoint}", params=params)
            assert response.status_code == 422, response.text

    asyncio.run(check())


@pytest.mark.parametrize("endpoint", ["status", "chart", "execution-summary", "execution-detail"])
def test_missing_account_is_rejected_before_reader(dataset, monkeypatch, endpoint):
    async def reject_reader(*_args, **_kwargs):
        raise AssertionError("missing account must be checked before reading a snapshot")

    monkeypatch.setattr(routes, "_op", reject_reader)

    async def check():
        async with http_client(dataset) as (client, _, _):
            params = {"snapshot_id": "s1", "record_id": 1} if endpoint == "execution-detail" else {"snapshot_id": "s1"}
            response = await client.get(f"/api/v1/account/performance/999/{endpoint}", params=params)
            assert response.status_code == 404
            assert response.json()["detail"] == "账户不存在"

    asyncio.run(check())
