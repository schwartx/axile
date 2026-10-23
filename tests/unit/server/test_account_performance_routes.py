"""绩效读路由:同步计算走原生绑定,状态空缺时不等待队列."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.common.trade_channel import TradeChannel
from axile.server._native import _core
from axile.server.api.deps import get_db
from axile.server.api.routes import account_performance as routes
from axile.server.db.models import Account
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION, read_snapshot_status


class _RouteSession:
    """账户存在、无分析状态行:read_snapshot_status 返回 empty,不触发等待."""

    def __init__(self, account: Account | None = None, path: str = "unused") -> None:
        self.path = path
        self.account = account

    def get_bind(self):
        return SimpleNamespace(url=SimpleNamespace(database=self.path))

    async def get(self, _model: object, account_id: int) -> Account | None:
        if self.account is not None and self.account.id == account_id:
            return self.account
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        class _Empty:
            def mappings(self) -> object:
                return self

            def first(self) -> None:
                return None

        return _Empty()


@pytest.mark.parametrize(
    ("snapshot_id", "compatible", "error", "requested", "running", "published", "expected"),
    [
        (None, True, None, False, None, 1, "empty"),
        (None, True, None, False, None, 0, "empty"),
        (None, True, None, True, None, 0, "pending"),
        (None, True, None, False, 0, 1, "pending"),
        ("", False, "", False, None, 1, "empty"),
        ("s1", True, "", False, None, 1, "ready"),
        ("s1", True, None, False, None, 0, "stale"),
        ("s1", True, None, True, 1, 1, "stale"),
        ("s1", True, "failed", True, 1, 0, "failed"),
        ("s1", False, "failed", True, 1, 0, "pending"),
        ("s1", False, None, False, None, 1, "pending"),
    ],
)
def test_wait_status_uses_native_rules_in_committed_database(
    tmp_path, snapshot_id, compatible, error, requested, running, published, expected
) -> None:
    if not hasattr(_core, "queue_status"):
        pytest.skip("Queue status requires rebuilt native extension")
    path = str(tmp_path / "status.sqlite")
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        CREATE TABLE account_analysis(account_id INTEGER, current_snapshot TEXT, error TEXT,
          requested INTEGER, running_version INTEGER, published_version INTEGER, source_version INTEGER);
        CREATE TABLE performance_snapshot(id TEXT, logic_version TEXT, engine_version TEXT);
        """)
        connection.execute(
            "INSERT INTO account_analysis VALUES (1,?,?,?,?,?,1)", (snapshot_id, error, requested, running, published)
        )
        if snapshot_id is not None:
            connection.execute(
                "INSERT INTO performance_snapshot VALUES (?,?,?)",
                (snapshot_id, LOGIC_VERSION if compatible else "old", ENGINE_VERSION),
            )
    assert asyncio.run(read_snapshot_status(path, 1)) == expected


def _build_account() -> Account:
    return Account(
        id=1,
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_config={"api_key": "k", "secret_key": "s", "is_testnet": True},
        is_started=True,
        cron_expr="*/5 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=0.001,
        long_leverage=1.0,
        short_leverage=1.0,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        empty_positions_algorithm=None,
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=1,
        write_empty_record=0,
        account_control_preset="default",
        account_control_override=None,
    )


def _build_app(session: _RouteSession) -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)

    async def _override_get_db() -> AsyncGenerator[_RouteSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _seed_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE replay_observation (record_id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,
         execution_id TEXT,time TEXT NOT NULL,asset REAL,sizing_fallback INTEGER NOT NULL,
         target_present INTEGER NOT NULL,target_weight_present INTEGER NOT NULL,
         target_units_present INTEGER NOT NULL,target_quantities_present INTEGER NOT NULL);
        CREATE TABLE replay_observation_symbol (record_id INTEGER NOT NULL,symbol TEXT NOT NULL,
         target REAL,price REAL,target_weight REAL,target_units REAL,target_quantity REAL,
         PRIMARY KEY(record_id,symbol));
        CREATE TABLE account (id INTEGER PRIMARY KEY, trade_channel TEXT NOT NULL);
        CREATE TABLE account_settings (account_id INTEGER PRIMARY KEY,
         backtest TEXT NOT NULL DEFAULT '{"backtest_weight_type": "ts", "backtest_fee_rate": 0.0}');
        CREATE TABLE portfolioaccount (id INTEGER PRIMARY KEY, account_id INTEGER, portfolio_id INTEGER, created_at TEXT);
        CREATE TABLE analysis_calendar_day (channel TEXT, day TEXT, status TEXT, calendar_id TEXT, label TEXT,
         PRIMARY KEY(channel, day));
        INSERT INTO account VALUES (1, 'ctp');
        INSERT INTO account_settings VALUES (1, '{"backtest_weight_type": "ts", "backtest_fee_rate": 0.0}');
        INSERT INTO replay_observation VALUES (1,1,'e-1','2026-01-01T10:00:00',100.0,0,1,1,0,0);
        INSERT INTO replay_observation VALUES (2,1,'e-2','2026-01-02T10:00:00',110.0,0,1,1,0,0);
        """
    )
    connection.commit()
    connection.close()


def test_performance_route_computes_synchronously_without_backtest(tmp_path: Path, monkeypatch) -> None:
    """无回测请求在快照未就绪时立即经原生 op 同步计算,不进入等待循环."""
    database = tmp_path / "analysis.db"
    _seed_database(database)
    monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(str(database)))
    monkeypatch.setattr(routes, "read_snapshot_status", AsyncMock(return_value="empty"))
    client = TestClient(_build_app(_RouteSession(_build_account(), str(database))))

    response = client.get("/performance/1", params={"include_backtest": False})

    assert response.status_code == 200
    body = response.json()
    assert body["record_count"] == 2
    assert body["baseline"] == "2026-01-01T10:00:00"
    assert body["settings"]["backtest_weight_type"] == "cs"
    assert len(body["points"]) == 3


def test_performance_route_rejects_unknown_account(tmp_path: Path, monkeypatch) -> None:
    """账户不存在时保持 404,不触碰分析存储."""
    monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(str(tmp_path / "missing.db")))
    client = TestClient(_build_app(_RouteSession(None)))

    assert client.get("/performance/1").status_code == 404


def test_snapshot_route_reads_published_snapshot(tmp_path: Path, monkeypatch) -> None:
    """快照路由直读原生绑定;已发布结果原样返回,不再走 Python 读取器."""
    path = tmp_path / "routes-snapshot.sqlite"
    _seed_database(path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE account_analysis (account_id INTEGER PRIMARY KEY, source_version INTEGER NOT NULL DEFAULT 1,
         published_version INTEGER NOT NULL DEFAULT 0, requested INTEGER NOT NULL DEFAULT 0,
         current_snapshot TEXT, running_version INTEGER, failures INTEGER NOT NULL DEFAULT 0,
         retry_at REAL NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE performance_snapshot (id TEXT PRIMARY KEY, account_id INTEGER, source_version INTEGER,
         logic_version TEXT, engine_version TEXT, computed_at TEXT, data_until TEXT, settings TEXT,
         ranges TEXT, result_schema_version INTEGER NOT NULL DEFAULT 0);
        INSERT INTO account_analysis VALUES (1, 1, 1, 0, 'snap-1', NULL, 0, 0, NULL);
        INSERT INTO performance_snapshot VALUES ('snap-1', 1, 1, '18', 'equity-ledger-1',
         '2026-01-02T15:00:00', NULL,
         '{"backtest_weight_type": "cs", "backtest_fee_rate": 0.0}',
         '{"all": {"performance": {"settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
            "engine_version": "equity-ledger-1", "range": "all",
            "baseline": "2026-01-01T10:00:00", "end": "2026-01-02T10:00:00",
            "points": [{"date": "2026-01-02", "observed_at": "2026-01-02T10:00:00", "account_equity": 110.0}],
            "executions": [], "calendar": {"status": "not_required"}},
            "daily_costs": {}, "events": [], "event_count": 0}}', 0);
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(routes, "analysis_store", lambda: _core.Store(str(path)))
    app = _build_app(_RouteSession(_build_account()))
    data = TestClient(app).get("/performance/1/snapshot").json()
    assert data["status"] == "ready"
    assert data["snapshot_id"] == "snap-1"
    assert data["result"]["points"][-1]["account_equity"] == 110.0
    assert data["settings"] == {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0}
