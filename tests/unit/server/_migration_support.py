"""真实历史数据库的迁移测试辅助，不随生产包交付。"""

from contextlib import contextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa

from tests.conftest import initialize_native_database, native_store_call
from tests.sqlite_oracle import register_time_function


@contextmanager
def _connect(database):
    """原生调用前关闭测试侧连接，避免两套 SQLite 内核共享文件锁。"""
    engine = sa.create_engine(f"sqlite:///{database}", poolclass=sa.pool.NullPool)
    sa.event.listen(engine, "connect", register_time_function)
    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
            yield connection
    finally:
        engine.dispose()


def _upgrade(database, revision="0034"):
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


def _schema(connection):
    return connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
