"""Structured analysis migration keeps publication reversible and invalidation precise."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa

from tests.conftest import initialize_native_database, native_store_call
from tests.fixtures.rust_store.oracle_tables import RESULT_TABLES

SOURCE_FACT_TABLES = (
    "analysis_source_transaction_fee",
    "analysis_source_transaction",
    "analysis_source_position",
    "analysis_source_attempt",
    "analysis_source_execution_symbol",
    "analysis_source_trade",
    "analysis_source_execution",
)
_REPLAY_TABLES = ("replay_observation", "replay_observation_symbol")


@contextmanager
def _connect(database):
    """原生调用前关闭测试侧连接，避免两套 SQLite 内核共享文件锁。"""
    engine = sa.create_engine(f"sqlite:///{database}", poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
            yield connection
    finally:
        engine.dispose()


def _upgrade(database, revision="0035"):
    return initialize_native_database(database, revision=revision)


def _downgrade(database, revision="0034"):
    return native_store_call(database, "migration_downgrade", {"public": revision})


@pytest.fixture
def database(tmp_path):
    """使用真实历史迁移及关闭外键的文件库验证 DDL。"""
    database = tmp_path / "structured-analysis.db"
    initialize_native_database(database, revision="0034")
    with _connect(database) as connection:
        connection.exec_driver_sql(
            "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
            "VALUES(2,'synthetic','FUTURES','tq','','2026-01-01','2026-01-01')"
        )
        connection.exec_driver_sql(
            "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at,updated_at) "
            "VALUES(11,'synthetic','FUTURES','','2026-01-01','2026-01-01')"
        )
    return database


def _ranges():
    """Include nullable values, absent optional costs and every normalized child kind."""
    settings = {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002}
    performance = {
        "settings": settings,
        "backtest_included": True,
        "engine_version": "equity-ledger-1",
        "baseline": "2026-01-01T09:00:00",
        "end": "2026-01-02T09:00:00",
        "record_count": 3,
        "observation_count": 2,
        "used_record_count": 2,
        "target_used_record_count": 1,
        "invalid_asset_count": 1,
        "skips": {
            "count": 1,
            "missing_target": 1,
            "missing_ticks": 0,
            "first_time": "2026-01-01T10:00:00",
            "last_time": "2026-01-01T10:00:00",
        },
        "target_skips": None,
        "sizing_fallback_count": 1,
        "sizing_fallback_ranges": [{"start": "2026-01-01T09:00:00", "end": "2026-01-01T09:00:00", "count": 1}],
        "points": [
            {
                "date": "2026-01-02",
                "observed_at": "2026-01-02T09:00:00",
                "record_id": 3,
                "execution_id": "three",
                "account_return": 0.02,
                "account_equity": 102.0,
                "portfolio_return": 0.01,
                "account_daily_return": 0.02,
                "portfolio_daily_return": 0.01,
                "target_portfolio_return": None,
                "target_portfolio_daily_return": None,
                "sizing_fallback": False,
                "difference": 0.01,
            }
        ],
        "bindings": [{"time": "2026-01-01T09:00:00", "portfolio_id": None}],
        "calendar": {
            "status": "partial",
            "calendar_id": "test",
            "label": "测试日历",
            "closed_ranges": [{"start": "2026-01-01", "end": "2026-01-01"}],
            "unavailable_ranges": [{"start": "2026-01-02", "end": "2026-01-02"}],
        },
    }
    ranges = {}
    for range_key in ("30", "90", "all"):
        item = deepcopy(performance)
        item["range"] = range_key
        if range_key == "90":
            item["target_skips"] = {
                "count": 2,
                "missing_target": 0,
                "missing_ticks": 2,
                "first_time": None,
                "last_time": None,
            }
        if range_key == "all":
            item["executions"] = []
        ranges[range_key] = {
            "performance": item,
            "daily_costs": {
                "2026-01-02": {
                    "value": None,
                    "cost": None,
                    "lossBp": None,
                    "coverage": 0.0,
                    "covered": 0,
                    "count": 1,
                    "amountComplete": False,
                    "fees": {"CNY": 0.0, "USD": 0.2},
                    "feeCovered": 1,
                },
            },
            "events": [{"time": "2026-01-02T09:00:00", "tag": "success", "text": "执行完成", "executionId": "three"}],
            "event_count": 17,
        }
    return ranges


def _insert_snapshot(connection, snapshot_id, ranges, *, settings=None):
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["performance_snapshot"])
    connection.execute(
        metadata.tables["performance_snapshot"].insert(),
        {
            "id": snapshot_id,
            "account_id": 2,
            "source_version": 3,
            "cost_schema_version": 1,
            "logic_version": "17",
            "engine_version": "equity-ledger-1",
            "computed_at": "2026-01-03T00:00:00",
            "settings": settings
            if settings is not None
            else {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002},
            "ranges": ranges,
        },
    )


def test_structured_result_upgrade_backfills_scalar_and_child_facts(database):
    """Legacy result semantics survive extraction without loading execution payloads."""
    with _connect(database) as connection:
        _insert_snapshot(connection, "good", _ranges())
    _upgrade(database)
    with _connect(database) as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT result_schema_version, backtest_weight_type, backtest_fee_rate FROM performance_snapshot"
                )
            )
            .mappings()
            .one()
        )
        assert row["result_schema_version"] == 2
        assert row["backtest_weight_type"] == "cs"
        assert row["backtest_fee_rate"] == pytest.approx(0.0002)
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_result_range").scalar_one() == 3
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_result_point").scalar_one() == 3
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_result_skip").scalar_one() == 4
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_result_calendar_range").scalar_one() == 6
        assert set(connection.exec_driver_sql("SELECT DISTINCT kind FROM performance_result_skip").scalars()) == {
            "portfolio",
            "target",
        }
        assert set(
            connection.exec_driver_sql("SELECT DISTINCT kind FROM performance_result_calendar_range").scalars()
        ) == {
            "closed",
            "unavailable",
        }
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_result_daily_fee").scalar_one() == 6
        assert connection.exec_driver_sql("SELECT MIN(event_count) FROM performance_result_range").scalar_one() == 17
        assert (
            connection.exec_driver_sql(
                "SELECT SUM(value) FROM performance_result_daily_fee WHERE currency='CNY'"
            ).scalar_one()
            == 0
        )
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM performance_result_binding WHERE portfolio_id IS NULL"
            ).scalar_one()
            == 3
        )


def test_structured_backfill_rejects_one_snapshot_without_partial_children(database):
    """One malformed historical result cannot publish a partial range or block good peers."""
    with _connect(database) as connection:
        malformed = _ranges()
        malformed["all"]["performance"]["points"][0]["account_equity"] = "invalid"
        _insert_snapshot(connection, "bad", malformed)
        _insert_snapshot(connection, "good", _ranges())
    _upgrade(database)
    with _connect(database) as connection:
        assert dict(
            connection.execute(sa.text("SELECT id, result_schema_version FROM performance_snapshot")).all()
        ) == {
            "bad": 0,
            "good": 2,
        }
        for table in RESULT_TABLES:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(table).where(table.c.snapshot_id == "bad")
                ).scalar_one()
                == 0
            )
        legacy = connection.execute(
            sa.text(
                "SELECT json_extract(ranges, '$.all.performance.points[0].account_equity') FROM performance_snapshot WHERE id='bad'"
            )
        ).scalar_one()
        assert legacy == "invalid"


def test_structured_snapshot_downgrade_rebuilds_legacy_json_from_sentinels(database):
    """New snapshots with empty JSON remain readable after a schema rollback."""
    with _connect(database) as connection:
        expected = _ranges()
        _insert_snapshot(connection, "snapshot", expected)
    _upgrade(database)
    with _connect(database) as connection:
        connection.exec_driver_sql("UPDATE performance_snapshot SET settings='{}', ranges='{}' WHERE id='snapshot'")
    _downgrade(database)
    with _connect(database) as connection:
        metadata = sa.MetaData()
        metadata.reflect(connection, only=["performance_snapshot"])
        row = connection.execute(sa.select(metadata.tables["performance_snapshot"])).mappings().one()
        assert row["settings"] == {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002}
        assert row["ranges"] == expected
        inspector = sa.inspect(connection)
        assert "result_schema_version" not in {
            column["name"] for column in inspector.get_columns("performance_snapshot")
        }
        assert all(table.name not in inspector.get_table_names() for table in RESULT_TABLES)
        assert all(table not in inspector.get_table_names() for table in SOURCE_FACT_TABLES)


def test_structured_snapshot_delete_cleans_children_without_foreign_keys(database):
    """Snapshot retention owns every normalized child even when SQLite FKs are off."""
    with _connect(database) as connection:
        _insert_snapshot(connection, "snapshot", _ranges())
    _upgrade(database)
    with _connect(database) as connection:
        connection.exec_driver_sql("DELETE FROM performance_snapshot WHERE id='snapshot'")
        for table in RESULT_TABLES:
            assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0, table.name


def _insert_records(connection):
    connection.exec_driver_sql(
        "INSERT INTO executerecord (id, account_id, created_at, execution_id, is_success,raw_input,raw_result) VALUES "
        "(1, 2, '2026-01-01T09:00:00', 'one', 1,'{}','{}'), (2, 2, '2026-01-01T10:00:00', 'two', 1,'{}','{}')"
    )


def _seed_replay(connection, record_id):
    connection.execute(
        sa.text(
            "INSERT INTO replay_observation(record_id,account_id,execution_id,time,sizing_fallback,"
            "target_present,target_weight_present,target_units_present,target_quantities_present) "
            "VALUES(:record_id,2,:execution_id,'2026-01-01T09:00:00',0,0,0,0,0)"
        ),
        {"record_id": record_id, "execution_id": "one" if record_id == 1 else "two"},
    )
    connection.execute(
        sa.text("INSERT INTO replay_observation_symbol(record_id,symbol,price) VALUES(:record_id,'A',100)"),
        {"record_id": record_id},
    )


def _seed_source(connection, record_id):
    values = {
        "analysis_source_execution": {"account_id": 2},
        "analysis_source_trade": {
            "trade_index": 0,
            "symbol": "A",
            "time": 1.0,
            "day": "2026-01-01",
            "estimated": False,
        },
        "analysis_source_execution_symbol": {"symbol": "A", "present": 0},
        "analysis_source_position": {"idx": 0, "symbol": "A", "quantity": 1},
        "analysis_source_attempt": {"idx": 0, "symbol": "A", "planned": 1},
        "analysis_source_transaction": {"idx": 0, "symbol": "A", "time_estimated": False, "amount_complete": False},
        "analysis_source_transaction_fee": {"transaction_idx": 0, "currency": "CNY", "value": 0},
    }
    for table in reversed(SOURCE_FACT_TABLES):
        row = {"record_id": record_id, **values[table]}
        connection.execute(
            sa.text(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join(':' + key for key in row)})"), row
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE executerecord SET raw_input='{\"changed\":true}' WHERE id=1",
        "UPDATE executerecord SET raw_result='{\"changed\":true}' WHERE id=1",
        "UPDATE executerecord SET is_success=0 WHERE id=1",
        "UPDATE executerecord SET created_at='2026-01-02T09:00:00' WHERE id=1",
        "UPDATE executerecord SET account_id=3 WHERE id=1",
        "UPDATE executerecord SET execution_id='corrected' WHERE id=1",
        "DELETE FROM executerecord WHERE id=1",
    ],
)
def test_execution_mutation_invalidates_source_and_replay_facts(database, mutation):
    """Corrections clear both projections precisely, including success-only edits."""
    _upgrade(database)
    with _connect(database) as connection:
        _insert_records(connection)
        for record_id in (1, 2):
            _seed_source(connection, record_id)
            _seed_replay(connection, record_id)
        connection.exec_driver_sql(mutation)
        for table in (*SOURCE_FACT_TABLES, *_REPLAY_TABLES):
            assert connection.exec_driver_sql(f"SELECT record_id FROM {table}").scalars().all() == [2], table


@pytest.mark.parametrize("action", ["insert", "update", "delete"])
def test_target_mutations_invalidate_only_matching_replay_facts(database, action):
    """Late or corrected targets invalidate replay without discarding frozen costs."""
    _upgrade(database)
    with _connect(database) as connection:
        _insert_records(connection)
        if action != "insert":
            connection.exec_driver_sql(
                "INSERT INTO target_weight_snapshot (id, account_id, execution_id,portfolio_id,source,calculated_at) VALUES (1, 2, 'one',11,'execution','2026-01-01')"
            )
        for record_id in (1, 2):
            _seed_source(connection, record_id)
            _seed_replay(connection, record_id)
        commands = {
            "insert": "INSERT INTO target_weight_snapshot (id, account_id, execution_id,portfolio_id,source,calculated_at) VALUES (1, 2, 'one',11,'execution','2026-01-01')",
            "update": "UPDATE target_weight_snapshot SET normalized_weights='{\"A\":0.5}' WHERE id=1",
            "delete": "DELETE FROM target_weight_snapshot WHERE id=1",
        }
        connection.exec_driver_sql(commands[action])
        for table in _REPLAY_TABLES:
            assert connection.exec_driver_sql(f"SELECT record_id FROM {table}").scalars().all() == [2], table
        for table in SOURCE_FACT_TABLES:
            assert connection.exec_driver_sql(f"SELECT record_id FROM {table} ORDER BY record_id").scalars().all() == [
                1,
                2,
            ], table


def test_structured_backfill_streams_large_point_sets_without_returning_json_documents(database):
    """大量子事实留在原生迁移内，Python 只接收版本回执。"""
    ranges = _ranges()
    for item in ranges.values():
        template = item["performance"]["points"][0]
        item["performance"]["points"] = [{**template, "record_id": index + 1} for index in range(205)]
    with _connect(database) as connection:
        _insert_snapshot(connection, "large", ranges)
    receipt = _upgrade(database)
    assert receipt["public"] == "0035"
    assert "ranges" not in receipt and "settings" not in receipt
    with _connect(database) as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_result_point").scalar_one() == 615
        assert connection.exec_driver_sql("SELECT MAX(idx) FROM performance_result_point").scalar_one() == 204


def test_source_header_delete_cleans_children_without_deleting_replay(database):
    """Replacing a cost projection never leaves old children or erases independent replay facts."""
    _upgrade(database)
    with _connect(database) as connection:
        _insert_records(connection)
        for record_id in (1, 2):
            _seed_source(connection, record_id)
            _seed_replay(connection, record_id)
        connection.exec_driver_sql("DELETE FROM analysis_source_execution WHERE record_id=1")
        for table in SOURCE_FACT_TABLES:
            assert connection.exec_driver_sql(f"SELECT record_id FROM {table}").scalars().all() == [2], table
        assert connection.exec_driver_sql("SELECT record_id FROM replay_observation").scalars().all() == [1, 2]


def test_target_identity_correction_invalidates_both_old_and_new_replay(database):
    """Moving a target between executions invalidates both associations without touching costs."""
    _upgrade(database)
    with _connect(database) as connection:
        _insert_records(connection)
        connection.exec_driver_sql(
            "INSERT INTO target_weight_snapshot (id, account_id, execution_id,portfolio_id,source,calculated_at) VALUES (1, 2, 'one',11,'execution','2026-01-01')"
        )
        for record_id in (1, 2):
            _seed_source(connection, record_id)
            _seed_replay(connection, record_id)
        connection.exec_driver_sql("UPDATE target_weight_snapshot SET execution_id='two' WHERE id=1")
        for table in _REPLAY_TABLES:
            assert connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one() == 0, table
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM analysis_source_execution").scalar_one() == 2


def test_account_delete_cleans_all_analysis_facts_without_foreign_keys(database):
    """Account removal cannot orphan source, replay or published result rows."""
    with _connect(database) as connection:
        _insert_snapshot(connection, "snapshot", _ranges())
    _upgrade(database)
    with _connect(database) as connection:
        _insert_records(connection)
        for record_id in (1, 2):
            _seed_source(connection, record_id)
            _seed_replay(connection, record_id)
        connection.exec_driver_sql("DELETE FROM account WHERE id=2")
        for table in (*SOURCE_FACT_TABLES, *(table.name for table in RESULT_TABLES), *_REPLAY_TABLES):
            assert connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one() == 0, table
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM performance_snapshot").scalar_one() == 0


def test_trade_channel_change_invalidates_calendar_result_but_noop_update_does_not(database):
    """A channel correction requests new calendar facts without churning unchanged settings."""
    _upgrade(database)
    with _connect(database) as connection:
        connection.exec_driver_sql(
            "INSERT INTO account_analysis (account_id, source_version, current_snapshot, failures, retry_at, error) "
            "VALUES (2, 5, 'snapshot', 2, 999, 'old failure')"
        )
        connection.exec_driver_sql("UPDATE account SET trade_channel='ctp' WHERE id=2")
        row = connection.exec_driver_sql(
            "SELECT source_version, requested, failures, retry_at, error FROM account_analysis WHERE account_id=2"
        ).one()
        assert tuple(row) == (6, 1, 0, 0, None)
        connection.exec_driver_sql("UPDATE account SET trade_channel='ctp' WHERE id=2")
        assert (
            connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=2").scalar_one()
            == 6
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("points", {}),
        ("settings", {"backtest_weight_type": "cs", "backtest_fee_rate": 0.3}),
        ("calendar.closed_ranges", None),
        ("range", "90"),
    ],
)
def test_structured_backfill_rejects_incompatible_shapes_and_range_settings(database, path, value):
    """Unsupported container shapes and mismatched headers keep the complete legacy fallback."""
    with _connect(database) as connection:
        malformed = _ranges()
        parent = malformed["all"]["performance"]
        names = path.split(".")
        for name in names[:-1]:
            parent = parent[name]
        parent[names[-1]] = value
        _insert_snapshot(connection, "bad", malformed)
        _insert_snapshot(connection, "good", _ranges())
    _upgrade(database)
    with _connect(database) as connection:
        assert dict(connection.exec_driver_sql("SELECT id, result_schema_version FROM performance_snapshot").all()) == {
            "bad": 0,
            "good": 2,
        }
        for table in RESULT_TABLES:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(table).where(table.c.snapshot_id == "bad")
                ).scalar_one()
                == 0
            ), table.name


def test_structured_snapshot_downgrade_preserves_all_float64_bits(database):
    """SQLite JSON reconstruction must not round financial values to fifteen digits."""
    with _connect(database) as connection:
        expected = _ranges()
        settings = {"backtest_weight_type": "cs", "backtest_fee_rate": 0.12345678901234566}
        point_values = {
            "account_return": 0.12345678901234566,
            "account_equity": 123456789.01234567,
            "portfolio_return": -0.9876543210987654,
            "account_daily_return": 0.00012345678901234567,
            "portfolio_daily_return": -0.0009876543210987654,
            "target_portfolio_return": 1.0000000000000002,
            "target_portfolio_daily_return": 1.2345678901234567e-100,
            "difference": -1.2345678901234567e100,
        }
        daily_values = {
            "value": 123456789.01234567,
            "cost": -0.12345678901234566,
            "lossBp": 9.876543210987654,
            "coverage": 0.9876543210987654,
            "coveredValue": 12345678.901234567,
        }
        fee_values = {"CNY": 0.12345678901234566, "USD": 1.0000000000000002}
        for item in expected.values():
            item["performance"]["settings"] = deepcopy(settings)
            item["performance"]["points"][0].update(point_values)
            item["daily_costs"]["2026-01-02"].update(daily_values, fees=deepcopy(fee_values))
        _insert_snapshot(connection, "precision", expected, settings=settings)
    _upgrade(database)
    with _connect(database) as connection:
        assert connection.exec_driver_sql("SELECT result_schema_version FROM performance_snapshot").scalar_one() == 2
        connection.exec_driver_sql("UPDATE performance_snapshot SET settings='{}', ranges='{}'")
    _downgrade(database)
    with _connect(database) as connection:
        metadata = sa.MetaData()
        metadata.reflect(connection, only=["performance_snapshot"])
        row = connection.execute(sa.select(metadata.tables["performance_snapshot"])).mappings().one()
        assert row["settings"]["backtest_fee_rate"].hex() == settings["backtest_fee_rate"].hex()
        for range_key in expected:
            actual = row["ranges"][range_key]
            assert actual["performance"]["settings"]["backtest_fee_rate"].hex() == settings["backtest_fee_rate"].hex()
            for name, value in point_values.items():
                assert actual["performance"]["points"][0][name].hex() == value.hex(), (range_key, name)
            day = actual["daily_costs"]["2026-01-02"]
            for name, value in daily_values.items():
                assert day[name].hex() == value.hex(), (range_key, name)
            for currency, value in fee_values.items():
                assert day["fees"][currency].hex() == value.hex(), (range_key, currency)
