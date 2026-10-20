"""Actual Rust process and HTTP contracts for the native result boundary."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel

from axile.server import native_analysis
from axile.server.analysis_cost_facts import iter_cost_fact_batches
from axile.server.analysis_history import AnalysisRecord
from axile.server.analysis_results import iter_result_fact_batches
from axile.server.api.deps import get_db
from axile.server.api.routes import account_performance as routes
from axile.server.business_facts.repository import read_execution_detail, store_execution_facts
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis import cost_execution as executions
from axile.server.db.models.analysis import cost_trade as trades
from axile.server.db.models.performance import (
    AccountPerformance,
    PerformanceCalendar,
    PerformancePoint,
    PerformanceSettings,
    PerformanceSnapshot,
)
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION, read_snapshot
from axile.server.performance_costs import daily_costs, project_execution, summarize, timestamp
from axile.server.performance_details import CostQuery, read_costs
from tests.unit.server._execution_test_support import build_account


@pytest.fixture(autouse=True)
def native_route_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("AXILE_ANALYSIS_") and name != "AXILE_ANALYSIS_TEST_BINARY":
            monkeypatch.delenv(name)


@pytest.fixture
def binary(monkeypatch):
    root = Path(__file__).resolve().parents[3]
    override = os.environ.get("AXILE_ANALYSIS_TEST_BINARY")
    candidates = (
        [Path(override)]
        if override
        else [root / "rust/analysis/target/release/axile-analysis", root / "rust/analysis/target/debug/axile-analysis"]
    )
    selected = next((path for path in candidates if path.is_file()), None)
    if selected is None:
        pytest.skip("build rust/analysis first to run actual native HTTP contract tests")
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", str(selected))
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "required")
    return selected


@pytest.fixture(scope="module")
def native_route_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("native-route-schema") / "template.sqlite"
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            SQLModel.metadata.create_all(connection)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def dataset(tmp_path, native_route_schema):
    path = tmp_path / "native.sqlite"
    copyfile(native_route_schema, path)
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.0002)
    ranges = {}
    for range_key in ("all", "30", "90"):
        performance = AccountPerformance(
            settings=settings,
            engine_version=ENGINE_VERSION,
            range=range_key,
            baseline="2026-10-01T00:00:00+08:00",
            end="2026-10-02T00:00:00+08:00",
            used_record_count=2,
            points=[
                PerformancePoint(
                    date="2026-10-02",
                    observed_at="2026-10-02T00:00:00+08:00",
                    account_return=0.03,
                    portfolio_return=0.02,
                    portfolio_daily_return=0.01,
                    difference=0.01,
                )
            ],
            calendar=PerformanceCalendar(status="unavailable"),
        ).model_dump(mode="json")
        if range_key == "all":
            performance["executions"] = [
                {"record": {"id": 1, "created_at": "2026-09-30T15:59:59Z"}},
                {"record": {"id": 2, "created_at": "2026-10-01T00:00:00"}},
                {"record": {"id": 3, "created_at": "2026-10-01T16:00:00Z"}},
                {"record": {"id": 4, "created_at": "2026-10-02T00:00:01+08:00"}},
            ]
        else:
            performance.pop("executions")
        ranges[range_key] = {"performance": performance, "daily_costs": {}, "events": [], "event_count": 0}
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with Session(engine) as session:
            session.add(build_account(id=2))
            session.commit()
        with engine.begin() as connection:
            connection.execute(
                snapshots.insert().values(
                    id="published",
                    account_id=2,
                    source_version=1,
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                    computed_at="2026-10-02T00:00:00+08:00",
                    data_until="2026-10-02T00:00:00+08:00",
                    settings=settings.model_dump(),
                    ranges=ranges,
                )
            )
            connection.execute(
                states.insert().values(
                    account_id=2, source_version=1, published_version=1, current_snapshot="published"
                )
            )
    finally:
        engine.dispose()
    return path


def mutate(database, statement, parameters=None):
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(statement, parameters or {})
    finally:
        engine.dispose()


async def canonical_record(database, *, input_payload, result_payload):
    """Project an explicitly reconstructed record after canonical storage commits."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            record = ExecuteRecord(
                id=1,
                account_id=2,
                execution_id="typed-1",
                created_at="2026-10-01T12:00:00",
                is_success=1,
            )
            session.add(record)
            await session.flush()
            await store_execution_facts(session, record, raw_input=input_payload, raw_result=result_payload)
            await session.commit()
        async with sessions() as session:
            persisted = await session.get(ExecuteRecord, 1)
            assert persisted is not None
            assert not hasattr(persisted, "raw_input") and not hasattr(persisted, "raw_result")
            detail = await read_execution_detail(session, persisted)
            assert detail["input"] == input_payload
            assert detail["result"] == result_payload
            return AnalysisRecord.from_detail(detail)
    finally:
        await engine.dispose()


@asynccontextmanager
async def http_client(database):
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/account")
    app.state.analysis_manager = SimpleNamespace(wake=asyncio.Event())

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = session_dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client, sessions
    finally:
        await engine.dispose()


@pytest.mark.parametrize("state", ["ready", "stale", "failed", "incompatible", "pending", "empty"])
@pytest.mark.parametrize("range_key", ["all", "30"])
def test_native_snapshot_http_matches_python_public_envelope(binary, dataset, state, range_key):
    if state == "stale":
        mutate(dataset, states.update().values(source_version=2))
    elif state == "failed":
        mutate(dataset, states.update().values(error="temporary error", failures=2, retry_at=123.5))
    elif state == "incompatible":
        mutate(dataset, snapshots.update().values(logic_version="old"))
    elif state == "pending":
        mutate(dataset, states.update().values(current_snapshot=None, requested=True))
    elif state == "empty":
        mutate(dataset, states.delete())

    async def check():
        async with http_client(dataset) as (client, sessions):
            async with sessions() as session:
                expected = PerformanceSnapshot.model_validate(await read_snapshot(session, 2, range_key)).model_dump(
                    mode="json"
                )
            response = await client.get(f"/api/v1/account/performance/2/snapshot?range={range_key}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/json"
            assert response.json() == expected

    asyncio.run(check())


def test_native_response_bytes_are_untouched_and_python_does_not_decode_results(binary, dataset, monkeypatch):
    async def check():
        async with http_client(dataset) as (client, sessions):
            async with sessions() as session:
                bridge = native_analysis.get_native_analysis(session)
                expected = await bridge.query(
                    "snapshot", account_id=2, range="30", logic_version=LOGIC_VERSION, engine_version=ENGINE_VERSION
                )

            def reject_decode(*_args, **_kwargs):
                raise AssertionError("native response must remain serialized bytes")

            with monkeypatch.context() as scoped:
                scoped.setattr(
                    native_analysis, "json", SimpleNamespace(dumps=native_analysis.json.dumps, loads=reject_decode)
                )
                response = await client.get("/api/v1/account/performance/2/snapshot?range=30")
            assert response.status_code == 200
            assert response.content == expected

    asyncio.run(check())


@pytest.mark.parametrize("include_backtest", [True, False])
def test_ready_performance_uses_native_result_without_python_model(binary, dataset, monkeypatch, include_backtest):
    async def check():
        async with http_client(dataset) as (client, sessions):
            async with sessions() as session:
                expected = AccountPerformance.model_validate((await read_snapshot(session, 2, "30"))["result"])
                if not include_backtest:
                    expected.backtest_included = False
                    expected.used_record_count = 0
                    expected.skips = None
                    for point in expected.points:
                        point.portfolio_return = point.portfolio_daily_return = point.difference = None
                expected_json = expected.model_dump(mode="json")

            def reject_model(*_args, **_kwargs):
                raise AssertionError("ready native performance must bypass Python model construction")

            async def reject_legacy(*_args, **_kwargs):
                raise AssertionError("ready native performance must not read the Python snapshot")

            with monkeypatch.context() as scoped:
                scoped.setattr(AccountPerformance, "model_validate", reject_model)
                scoped.setattr(routes, "read_snapshot", reject_legacy)
                response = await client.get(
                    "/api/v1/account/performance/2", params={"range": "30", "include_backtest": include_backtest}
                )
            assert response.status_code == 200
            assert response.json() == expected_json

    asyncio.run(check())


@pytest.mark.parametrize("suffix", ["/snapshot", "", "/costs?snapshot_id=published"])
def test_account_existence_gate_runs_before_native_dispatch(binary, dataset, monkeypatch, suffix):
    def reject_dispatch(*_args, **_kwargs):
        raise AssertionError("missing account must be rejected before native dispatch")

    monkeypatch.setattr(routes, "get_native_analysis", reject_dispatch)

    async def check():
        async with http_client(dataset) as (client, _):
            response = await client.get(f"/api/v1/account/performance/999{suffix}")
            assert response.status_code == 404
            assert response.json()["detail"] == "账户不存在"

    asyncio.run(check())


def test_cost_cursor_and_expired_snapshot_keep_http_error_codes(binary, dataset):
    async def check():
        async with http_client(dataset) as (client, _):
            cursor = await client.get(
                "/api/v1/account/performance/2/costs", params={"snapshot_id": "published", "cursor": "bad"}
            )
            assert cursor.status_code == 400
            assert cursor.json()["detail"]["code"] == "INVALID_COST_CURSOR"
            expired = await client.get("/api/v1/account/performance/2/costs", params={"snapshot_id": "missing"})
            assert expired.status_code == 410
            assert expired.json()["detail"]["code"] == "SNAPSHOT_EXPIRED"

    asyncio.run(check())


def test_corrupt_native_snapshot_returns_503_without_python_fallback(binary, dataset, monkeypatch):
    mutate(dataset, sa.text("UPDATE performance_snapshot SET ranges = :ranges"), {"ranges": "not JSON"})

    async def reject_legacy(*_args, **_kwargs):
        raise AssertionError("native failure must not silently retry in Python")

    monkeypatch.setattr(routes, "read_snapshot", reject_legacy)

    async def check():
        async with http_client(dataset) as (client, _):
            response = await client.get("/api/v1/account/performance/2/snapshot")
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "SNAPSHOT_READ"

    asyncio.run(check())


def test_legacy_cost_schema_keeps_historical_identity_types_without_native_dispatch(binary, dataset, monkeypatch):
    identities = [False, -0.0, {"broken": True}, ["legacy", 1]]
    mutate(dataset, snapshots.update().values(cost_schema_version=0))
    when = timestamp("2026-10-01T12:00:00+08:00")
    for index, identity in enumerate(identities, start=1):
        mutate(
            dataset,
            executions.insert().values(
                snapshot_id="published",
                record_id=index,
                time=when,
                success=True,
                noop=False,
                payload={"record": {"id": index, "execution_id": identity}},
            ),
        )
        mutate(
            dataset,
            trades.insert().values(
                snapshot_id="published",
                id=index,
                record_id=index,
                symbol="A",
                time=when,
                day="2026-10-01",
                value=100.0,
                cost=1.0,
                loss_bp=100.0,
                fee=0.0,
                fee_currency="CNY",
                estimated=False,
                payload={"symbol": "A", "side": "buy", "quantity": 1, "legacy": "preserved"},
            ),
        )

    def reject_native(*_args, **_kwargs):
        raise AssertionError("cost_schema_version 0 must explicitly use the legacy reader")

    monkeypatch.setattr(routes, "get_native_analysis", reject_native)

    async def check():
        async with http_client(dataset) as (client, sessions):
            async with sessions() as session:
                expected = await read_costs(session, 2, CostQuery(snapshot_id="published", dimension="trade"))
            response = await client.get(
                "/api/v1/account/performance/2/costs", params={"snapshot_id": "published", "dimension": "trade"}
            )
            assert response.status_code == 200
            actual = response.json()
            assert actual == expected
            actual_ids = [row["execution_id"] for row in actual["data"]]
            for actual_id, original in zip(actual_ids, identities, strict=True):
                assert type(actual_id) is type(original)
                assert actual_id == original
                if isinstance(original, float):
                    assert actual_id.hex() == original.hex()
            assert all(row["legacy"] == "preserved" for row in actual["data"])

    asyncio.run(check())


def test_normalized_snapshot_and_ready_result_ignore_all_poisoned_legacy_json(binary, dataset):
    record = asyncio.run(
        canonical_record(
            dataset,
            input_payload={"curr_target": {"A": 1}},
            result_payload={
                "account_assets": {"total_asset": 100},
                "symbol_results": {
                    "A": {
                        "first_tick": {"bid_price": 100, "ask_price": 100},
                        "sizing": {"unit_multiplier": 1},
                        "orders": [{"order_id": "o", "direction": "BUY"}],
                        "trades": [
                            {
                                "order_id": "o",
                                "trade_price": 101,
                                "trade_volume": 2,
                                "extra": {"commission": 0.1, "commission_asset": "CNY"},
                            }
                        ],
                    }
                },
            },
        )
    )
    payload, fills = project_execution(record)
    preview = payload | {"summary": summarize(fills)}
    projected = [
        {
            "record_id": record.id,
            "time": timestamp(record.created_at),
            "success": True,
            "noop": payload["noop"],
            "payload": payload,
        }
    ]
    cost_rows = [
        {
            "id": index,
            "record_id": record.id,
            "symbol": fill["symbol"],
            "time": fill["time"],
            "day": fill["day"],
            "value": fill["value"],
            "cost": fill["cost"],
            "loss_bp": fill["lossBp"],
            "fee": fill["fee"],
            "fee_currency": fill["feeCurrency"],
            "estimated": fill["timeEstimated"],
            "payload": fill,
        }
        for index, fill in enumerate(fills)
    ]
    expected = {}
    engine = sa.create_engine(f"sqlite:///{dataset}")
    try:
        with engine.begin() as connection:
            snapshot = connection.execute(sa.select(snapshots)).mappings().one()
            ranges = snapshot["ranges"]
            for range_key, selected in ranges.items():
                performance = selected["performance"]
                performance["executions"] = [preview]
                performance["skips"] = {
                    "count": 2,
                    "missing_target": 1,
                    "missing_ticks": 1,
                    "first_time": record.created_at,
                    "last_time": record.created_at,
                }
                performance["target_skips"] = {
                    "count": 0,
                    "missing_target": 0,
                    "missing_ticks": 0,
                    "first_time": None,
                    "last_time": None,
                }
                performance["sizing_fallback_count"] = 1
                performance["sizing_fallback_ranges"] = [
                    {"start": record.created_at, "end": record.created_at, "count": 1}
                ]
                performance["bindings"] = [{"time": record.created_at, "portfolio_id": None}]
                performance["calendar"] = {
                    "status": "partial",
                    "calendar_id": "test-calendar",
                    "label": "测试交易日历",
                    "closed_ranges": [{"start": "2026-10-01", "end": "2026-10-01"}],
                    "unavailable_ranges": [{"start": "2026-10-02", "end": "2026-10-02"}],
                }
                selected["daily_costs"] = daily_costs(fills)
                selected["events"] = [
                    {"time": record.created_at, "tag": "执行", "text": "已完成", "executionId": "typed-1"}
                ]
                selected["event_count"] = 103
                expected[range_key] = PerformanceSnapshot.model_validate(
                    {
                        "status": "ready",
                        "source_version": 1,
                        "snapshot_id": "published",
                        "snapshot_version": 1,
                        "logic_version": LOGIC_VERSION,
                        "engine_version": ENGINE_VERSION,
                        "computed_at": snapshot["computed_at"],
                        "data_until": snapshot["data_until"],
                        "settings": snapshot["settings"],
                        "error": None,
                        "retry_at": None,
                        "result": performance,
                        "daily_costs": selected["daily_costs"],
                        "events": selected["events"],
                        "event_count": selected["event_count"],
                    }
                ).model_dump(mode="json")
            for table, rows in iter_result_fact_batches("published", ranges):
                connection.execute(table.insert(), rows)
            for table, rows in iter_cost_fact_batches("published", projected, cost_rows, ranges):
                connection.execute(table.insert(), rows)
            connection.execute(
                snapshots.update().values(
                    result_schema_version=2,
                    cost_schema_version=1,
                    backtest_weight_type="ts",
                    backtest_fee_rate=snapshot["settings"]["backtest_fee_rate"],
                )
            )
            for table, column in (
                ("performance_snapshot", "ranges"),
                ("performance_snapshot", "settings"),
                ("performance_cost_execution", "payload"),
                ("performance_cost_trade", "payload"),
            ):
                connection.execute(sa.text(f"UPDATE {table} SET {column} = :poison"), {"poison": "not JSON"})
    finally:
        engine.dispose()

    async def check():
        async with http_client(dataset) as (client, _):
            for range_key in ("all", "30", "90"):
                snapshot = await client.get(f"/api/v1/account/performance/2/snapshot?range={range_key}")
                assert snapshot.status_code == 200
                assert snapshot.json() == expected[range_key]
                result = await client.get(f"/api/v1/account/performance/2?range={range_key}")
                assert result.status_code == 200
                assert result.json() == expected[range_key]["result"]
            without_backtest = await client.get("/api/v1/account/performance/2?range=30&include_backtest=false")
            assert without_backtest.status_code == 200
            stripped = without_backtest.json()
            assert stripped["backtest_included"] is False
            assert stripped["skips"] is None
            assert stripped["used_record_count"] == 0
            assert stripped["points"][0]["portfolio_return"] is None
            assert stripped["points"][0]["account_return"] == 0.03

    asyncio.run(check())
