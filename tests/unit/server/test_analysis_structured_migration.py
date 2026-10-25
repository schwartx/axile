"""Structured analysis migration keeps publication reversible and invalidation precise."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa

from tests.conftest import initialize_native_database, native_store_call

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


def _upgrade(database, revision="head"):
    return initialize_native_database(database, revision=revision)


def _downgrade(database, revision="0033"):
    return native_store_call(database, "migration_downgrade", {"public": revision})


@pytest.fixture
def database(tmp_path):
    """使用真实历史迁移及关闭外键的文件库验证 DDL。"""
    database = tmp_path / "structured-analysis.db"
    initialize_native_database(database, revision="0033")
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
