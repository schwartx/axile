"""Exercise the compiled Rust worker against Python-written, isolated SQLite facts.

Build ``rust/analysis`` before running this suite. Numeric calculations may differ
by floating-point accumulation order; missing fields, nulls and list order must
match exactly. Fixtures deliberately omit the original execution history table.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.analysis_cost_facts import COST_FACT_SCHEMA_VERSION, COST_FACT_TABLES, iter_cost_fact_batches
from axile.server.analysis_observations import observation_fact_values
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis_replay import replay_observation as observations
from axile.server.db.models.analysis_replay import replay_observation_symbol as symbols
from axile.server.db.models.performance import PerformanceSettings
from axile.server.performance import calculate_performance, observation
from axile.server.performance_analysis import LOGIC_VERSION, compute_batch
from axile.server.performance_costs import timestamp
from axile.server.performance_details import CostQuery, _cursor, read_costs
from axile.server.performance_replay import ENGINE_VERSION

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def native_binary():
    explicit = os.environ.get("AXILE_ANALYSIS_BINARY")
    if explicit:
        path = Path(explicit)
        assert path.is_file() and os.access(path, os.X_OK), f"Native analysis binary is not executable: {path}"
        return path
    root = Path(__file__).resolve().parents[3]
    for profile in ("debug", "release"):
        path = root / "rust" / "analysis" / "target" / profile / "axile-analysis"
        if path.is_file() and os.access(path, os.X_OK):
            return path
    pytest.skip("Build rust/analysis or set AXILE_ANALYSIS_BINARY to exercise the compiled native contract")


def _query(binary, database, command, **request):
    result = subprocess.run(
        [str(binary), "query", "--database", str(database)],
        input=json.dumps({"command": command, **request}),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)


def _same(actual, expected, path="result"):
    """Allow f64 rounding only; do not equate null, false, zero or absent keys."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert actual.keys() == expected.keys(), path
        for name, value in expected.items():
            _same(actual[name], value, f"{path}.{name}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for index, value in enumerate(expected):
            _same(actual[index], value, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool), path
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), path
    else:
        assert type(actual) is type(expected) and actual == expected, path


def _record(index, *, day=None, price=100, quantity=1, asset=100, weight=1, multiplier=1):
    return SimpleNamespace(
        account_id=7,
        id=index,
        execution_id=f"exec-{index}",
        is_success=1,
        created_at=(datetime(2026, 1, 1, 9) + timedelta(days=index - 1 if day is None else day)).isoformat(),
        raw_input={"curr_target": {"A": weight}},
        raw_result={
            "status": "SUCCEEDED",
            "account_assets": {"total_asset": asset, "positions": []},
            "symbol_results": {
                "A": {
                    "status": "SUCCEEDED",
                    "first_tick": {"last_price": price, "bid_price": price, "ask_price": price},
                    "sizing": {
                        "sizing_mode": "weight",
                        "status": "SIZED",
                        "target_quantity": quantity,
                        "equity": asset,
                        "reference_price": price,
                        "unit_notional": price * multiplier,
                        "unit_multiplier": multiplier,
                    },
                }
            },
        },
    )


def _replay_records(scenario):
    if scenario == "empty":
        return []
    if scenario == "quantity_turnover":
        return [
            _record(i + 1, price=price, quantity=quantity)
            for i, (price, quantity) in enumerate(((100, 1), (110, 2), (120, 1), (115, -1), (105, 0), (90, 0)))
        ]
    if scenario == "currency_conversion":
        return [_record(1, multiplier=7), _record(2, multiplier=8), _record(3, price=101, multiplier=8)]
    if scenario == "zero_equity":
        return [_record(i + 1, price=price, quantity=2, weight=0) for i, price in enumerate((100, 50, 60))]
    if scenario == "no_asset":
        return [_record(1, asset=None), _record(2, asset=0), _record(3, asset=None)]
    if scenario == "same_time_and_ranges":
        return [
            _record(2, day=0, price=101, asset=101),
            _record(1, day=0, price=100),
            _record(3, day=25, price=110, asset=110),
            _record(4, day=60, price=120, asset=120),
            _record(5, day=95, price=130, asset=130),
            _record(6, day=120, price=140, asset=140),
        ]
    records = [_record(i + 1, price=100 + i * 10, asset=100 + i * 10) for i in range(5)]
    if scenario == "missing_target":
        records[1].raw_input = {}
        records[1].raw_result["symbol_results"] = {}
        records[2].raw_input = {}
        records[2].raw_result["symbol_results"] = {}
    elif scenario == "missing_exit_price":
        records[1].raw_result["symbol_results"]["A"]["first_tick"] = {}
        records[1].raw_input["curr_target"] = {}
        records[1].raw_result["symbol_results"]["A"]["sizing"]["target_quantity"] = 0
    elif scenario == "sizing_fallback":
        for record in records[1:3]:
            record.raw_result["symbol_results"]["A"]["sizing"] = {}
    elif scenario == "unknown_asset_day":
        records[1].raw_result["account_assets"]["total_asset"] = None
        records[2].raw_result["account_assets"]["total_asset"] = 0
    else:
        raise AssertionError(scenario)
    return records


def _write_observations(path, records):
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            observations.create(connection)
            symbols.create(connection)
            for record in records:
                scalar, rows = observation_fact_values(record)
                connection.execute(observations.insert(), scalar)
                if rows:
                    connection.execute(symbols.insert(), rows)
            assert "executerecord" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize("include_backtest", [True, False])
@pytest.mark.parametrize(
    "scenario",
    [
        "empty",
        "quantity_turnover",
        "currency_conversion",
        "zero_equity",
        "no_asset",
        "same_time_and_ranges",
        "missing_target",
        "missing_exit_price",
        "sizing_fallback",
        "unknown_asset_day",
    ],
)
def test_native_performance_matches_python_facts(native_binary, tmp_path, scenario, include_backtest):
    records = _replay_records(scenario)
    database = tmp_path / "facts.db"
    _write_observations(database, records)
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.001)
    actual = _query(
        native_binary,
        database,
        "performance",
        account_id=7,
        settings=settings.model_dump(),
        include_backtest=include_backtest,
    )
    items = [observation(record) for record in records]
    expected = {
        key: calculate_performance(items, settings, key, include_backtest=include_backtest).model_dump(mode="json")
        for key in ("30", "90", "all")
    }
    _same(actual, expected)


@pytest.mark.parametrize("range_key", ["30", "90", "all"])
@pytest.mark.parametrize("include_backtest", [True, False])
def test_native_requested_range_matches_python(native_binary, tmp_path, range_key, include_backtest):
    records = _replay_records("same_time_and_ranges")
    database = tmp_path / "single-range.db"
    _write_observations(database, records)
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.001)
    actual = _query(
        native_binary,
        database,
        "performance",
        account_id=7,
        range=range_key,
        settings=settings.model_dump(),
        include_backtest=include_backtest,
    )
    expected = calculate_performance(
        [observation(record) for record in records], settings, range_key, include_backtest=include_backtest
    ).model_dump(mode="json")
    _same(actual, expected)


def test_native_performance_rejects_changed_source_version(native_binary, tmp_path):
    database = tmp_path / "source-version.db"
    _write_observations(database, [])
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE account_analysis (account_id INTEGER PRIMARY KEY, source_version INTEGER NOT NULL)"
            )
        request = {
            "account_id": 7,
            "range": "all",
            "include_backtest": False,
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
        }
        assert _query(native_binary, database, "performance", expected_source_version=None, **request)["points"] == []
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO account_analysis VALUES (7, 4)")
        assert _query(native_binary, database, "performance", expected_source_version=4, **request)["points"] == []
        for stale in (None, 3):
            result = subprocess.run(
                [str(native_binary), "query", "--database", str(database)],
                input=json.dumps({"command": "performance", "expected_source_version": stale, **request}),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            assert result.returncode != 0 and not result.stdout
            assert json.loads(result.stderr)["code"] == "SOURCE_VERSION_CHANGED"
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT source_version FROM account_analysis").scalar_one() == 4
    finally:
        engine.dispose()


def _cost_records():
    records = [_record(i, price=100 + i, day=i - 1) for i in range(1, 5)]
    records[0].raw_result["execution_time"] = 0
    records[0].raw_result["symbol_results"]["A"].update(
        orders=[{"order_id": "buy", "direction": "BUY", "filled_volume": 2}],
        trades=[
            {
                "order_id": "buy",
                "trade_price": 103,
                "trade_volume": 2,
                "trade_time": "2026-01-01T23:59:00",
                "extra": {"commission": 0, "commission_asset": "CNY"},
            },
            {
                "trade_price": 99,
                "trade_volume": 1,
                "trade_time": "2026-01-02T00:01:00",
                "extra": {"direction": "sell", "commission": 0.1, "commission_asset": "USD"},
            },
            {"trade_price": 100, "trade_volume": 2, "trade_time": "bad"},
        ],
    )
    records[1].raw_result.update(status="NOOP", symbol_results={})
    records[2].is_success = 0
    records[2].raw_result.update(status="PARTIAL", error="partial execution", task_status="TERMINATED")
    records[2].raw_result["symbol_results"]["A"].update(
        status="FAILED",
        error=None,
        orders=[{"order_id": "sell", "direction": "SELL", "filled_volume": None}],
        trades=[
            {"order_id": "sell", "trade_price": 102, "trade_volume": 3, "extra": {"commission": -0.01}},
            {"trade_price": "bad", "trade_volume": None, "extra": {"commission": 0.02, "commission_asset": "USD"}},
        ],
    )
    records[3].execution_id = None
    records[3].raw_result["symbol_results"] = {
        "Z_%": {
            "status": "SUCCEEDED",
            "first_tick": {"last_price": 100},
            "sizing": {"unit_multiplier": 10},
            "trades": [{"trade_price": 101, "trade_volume": 1, "extra": {"direction": "buy"}}],
        }
    }
    return records


@pytest.fixture
def cost_database(tmp_path):
    path = tmp_path / "costs.db"
    ranges, projected, cost_rows = compute_batch(
        _cost_records(), [], [], [], PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0)
    )
    batches = list(iter_cost_fact_batches("snapshot-1", projected, cost_rows, ranges, batch_size=2))
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            snapshots.create(connection)
            for table in reversed(COST_FACT_TABLES):
                table.create(connection, checkfirst=True)
            connection.execute(
                snapshots.insert(),
                {
                    "id": "snapshot-1",
                    "account_id": 7,
                    "source_version": 1,
                    "logic_version": LOGIC_VERSION,
                    "engine_version": ENGINE_VERSION,
                    "computed_at": "2026-01-05T00:00:00",
                    "data_until": ranges["all"]["performance"]["end"],
                    "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0},
                    "ranges": ranges,
                    "cost_schema_version": COST_FACT_SCHEMA_VERSION,
                },
            )
            for table, rows in batches:
                connection.execute(table.insert(), rows)
            assert "executerecord" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
    return path


def _python_costs(database, query, monkeypatch):
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")

    async def read():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with async_sessionmaker(engine)() as session:
                return await read_costs(session, 7, query)
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.mark.parametrize("dimension", ["execution", "symbol", "trade", "summary"])
@pytest.mark.parametrize("sort", ["time", "cost"])
def test_native_costs_match_python_payloads(native_binary, cost_database, monkeypatch, dimension, sort):
    query = CostQuery(snapshot_id="snapshot-1", dimension=dimension, sort=sort, limit=2)
    expected = _python_costs(cost_database, query, monkeypatch)
    actual = _query(
        native_binary,
        cost_database,
        "costs",
        account_id=7,
        logic_version=LOGIC_VERSION,
        engine_version=ENGINE_VERSION,
        cursor_scope=_cursor(query, 7)[0],
        **query.model_dump(),
    )
    _same(actual, expected)
    if expected["next_cursor"]:
        next_query = query.model_copy(update={"cursor": expected["next_cursor"]})
        next_expected = _python_costs(cost_database, next_query, monkeypatch)
        next_actual = _query(
            native_binary,
            cost_database,
            "costs",
            account_id=7,
            logic_version=LOGIC_VERSION,
            engine_version=ENGINE_VERSION,
            cursor_scope=_cursor(next_query, 7)[0],
            **next_query.model_dump(),
        )
        _same(next_actual, next_expected)


@pytest.mark.parametrize(
    "filters",
    [
        {"day": "2026-01-02"},
        {"start": timestamp("2026-01-01T23:59:00"), "end": timestamp("2026-01-02T00:01:00")},
        {"record_id": 1},
        {"side": "none"},
        {"symbol_search": "_%"},
        {"symbol": "A", "side": "sell"},
        {"symbol": "missing"},
    ],
)
def test_native_cost_filters_preserve_unknowns_and_boundaries(native_binary, cost_database, monkeypatch, filters):
    query = CostQuery(snapshot_id="snapshot-1", dimension="execution", **filters)
    actual = _query(
        native_binary,
        cost_database,
        "costs",
        account_id=7,
        logic_version=LOGIC_VERSION,
        engine_version=ENGINE_VERSION,
        cursor_scope=_cursor(query, 7)[0],
        **query.model_dump(),
    )
    _same(actual, _python_costs(cost_database, query, monkeypatch))


def test_native_costs_do_not_decode_legacy_history_json(native_binary, cost_database, monkeypatch):
    queries = [
        CostQuery(snapshot_id="snapshot-1", dimension=dimension) for dimension in ("execution", "trade", "summary")
    ]
    expected = [_python_costs(cost_database, query, monkeypatch) for query in queries]
    engine = sa.create_engine(f"sqlite:///{cost_database}")
    try:
        with engine.begin() as connection:
            # Invalid JSON makes accidental legacy reads fail immediately instead
            # of merely hiding their allocation cost behind a small test payload.
            for table, column in (
                ("performance_snapshot", "ranges"),
                ("performance_cost_execution", "payload"),
                ("performance_cost_trade", "payload"),
            ):
                connection.exec_driver_sql(f"UPDATE {table} SET {column} = 'invalid-json'")
    finally:
        engine.dispose()
    for query, reference in zip(queries, expected, strict=True):
        actual = _query(
            native_binary,
            cost_database,
            "costs",
            account_id=7,
            logic_version=LOGIC_VERSION,
            engine_version=ENGINE_VERSION,
            cursor_scope=_cursor(query, 7)[0],
            **query.model_dump(),
        )
        _same(actual, reference)
