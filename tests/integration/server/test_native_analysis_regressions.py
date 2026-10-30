"""历史时间与展示规则经过真实原生发布后保持一致。"""

import asyncio
import copy
import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from axile.server import performance_analysis as analysis
from tests.conftest import native_store_call, require_native_store
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import persist_record_fixture
from tests.unit.server._runtime_db_support import to_us

_NOW = "2026-01-02T09:00:00+08:00"
_PRECISE = "2026-01-01T09:00:00.000006+08:00"
_RESULT = {
    "status": "SUCCEEDED",
    "account_assets": {"total_asset": 100.0, "positions": []},
    "symbol_results": {
        "A": {
            "status": "SUCCEEDED",
            "first_tick": {"bid_price": 99.0, "ask_price": 101.0, "last_price": 100.0},
            "sizing": {
                "sizing_mode": "weight",
                "status": "SIZED",
                "current_quantity": 0.0,
                "target_quantity": 1.0,
                "equity": 100.0,
                "unit_notional": 100.0,
                "reference_price": 100.0,
                "unit_multiplier": 1.0,
            },
            "orders": [{"order_id": "o", "direction": "BUY", "filled_volume": 1.0}],
            "trades": [
                {
                    "order_id": "o",
                    "trade_time": _PRECISE,
                    "trade_price": 100.0,
                    "trade_volume": 1.0,
                    "extra": {"commission": 0.0, "commission_asset": "CNY"},
                }
            ],
        }
    },
}


@pytest.fixture
def database(native_database: Path) -> Path:
    account = build_account(trade_channel="tq", portfolio_id=None, backtest_fee_rate=0.0)
    native_store_call(native_database, "account_create", {"account": account.model_dump(mode="json"), "now": _NOW})
    return native_database


def _persist(database: Path, created_at: str, result: dict, execution_id: str = "regression") -> dict:
    return persist_record_fixture(
        database,
        {
            "account_id": 1,
            "execution_id": execution_id,
            "created_at": created_at,
            "is_success": 1,
            "input_id": f"{execution_id}-input",
            "result_input_id": f"{execution_id}-result-input",
            "raw_input": {"curr_target": {"A": 1.0}},
            "raw_result": result,
        },
    )


def _publish(database: Path, row=None):
    core = require_native_store("analysis_update", "analysis_query")
    if row is None:
        core.queue_enqueue(str(database), 1, False)
        row = core.queue_claim(str(database), 1.0, "regression-job")
    native_store_call(
        database,
        "analysis_update",
        {
            "account_id": 1,
            "expected_source_version": row["source_version"],
            "claim_token": row["running_token"],
            "computed_at": _NOW,
        },
    )
    return core, core.Store(str(database)), str(row["source_version"])


def _read(database, version, command, **query):
    return native_store_call(
        database, "analysis_query", {"account_id": 1, "version": version, "command": command, **query}
    )


def test_lost_publication_receipt_and_old_claim_cannot_change_committed_or_new_work(database, monkeypatch):
    core = require_native_store("queue_enqueue", "queue_claim", "analysis_query")
    _persist(database, _PRECISE, _RESULT)
    monkeypatch.setattr(analysis, "database_path", lambda: str(database))
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(database))
    manager = analysis.AnalysisManager()
    claims = []

    async def publish_then_lose_receipt(row):
        _publish(database, row)
        claims.append(row)
        raise RuntimeError("receipt lost after commit")

    monkeypatch.setattr(manager, "_run_native", publish_then_lose_receipt)
    core.queue_enqueue(str(database), 1, False)

    async def run():
        assert await manager.run_once()
        row = claims[0]
        assert _read(database, str(row["source_version"]), "status")["status"] == "ready"
        _publish(database, row)
        await manager._fail(row)
        await manager._release_claim(row)
        with closing(oracle_connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM analysis_state WHERE range_key='all'").fetchone()[0] == 1
            assert connection.execute(
                "SELECT failures,running_token FROM account_analysis WHERE account_id=1"
            ).fetchone() == (0, None)
        core.queue_enqueue(str(database), 1, True)
        newer = core.queue_claim(str(database), 1.0, "newer-claim")
        assert newer["running_token"] == "newer-claim"
        await manager._fail(row)
        await manager._release_claim(row)
        with closing(oracle_connect(database)) as connection:
            assert connection.execute(
                "SELECT failures,running_token FROM account_analysis WHERE account_id=1"
            ).fetchone() == (0, "newer-claim")

    asyncio.run(run())


def test_cost_interval_keeps_the_trade_at_its_inclusive_microsecond_end(database: Path) -> None:
    _persist(database, _PRECISE, _RESULT)
    core, store, snapshot = _publish(database)
    end = to_us(_PRECISE) / 1000.0
    page = _read(database, snapshot, "costs", dimension="trade", start=end - 1000, end=end)
    assert page["count"] == 1
    assert page["data"][0]["time"] == end
    assert page["summary"]["fees"] == {"CNY": 0.0}


@pytest.mark.parametrize("created_at", ["2026-01-02 09:00:00", "2026-01-02"])
def test_legacy_time_formats_publish_without_moving_the_trade_day(database: Path, created_at: str) -> None:
    result = copy.deepcopy(_RESULT)
    result["symbol_results"]["A"]["trades"][0]["trade_time"] = "2026-01-01 09:00:00"
    _persist(database, created_at, result)
    core, store, _ = _publish(database)
    day = datetime.fromisoformat(created_at).astimezone(ZoneInfo("Asia/Shanghai")).date()
    activity = json.loads(core.activity_symbols(store, 1, day.isoformat(), (day + timedelta(days=1)).isoformat(), "A"))
    trade = activity["data"][0]["trades"][0]
    assert trade["day"] == datetime(2026, 1, 1, 9).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert trade["timeEstimated"] is False
    assert trade["time"] == datetime(2026, 1, 1, 9).timestamp() * 1000


def test_published_legacy_attempt_and_symbol_match_the_public_execution_display(database: Path) -> None:
    result = copy.deepcopy(_RESULT)
    result.pop("status")
    result["outcome"] = "not_reached"
    symbol = result["symbol_results"]["A"]
    symbol.pop("status")
    symbol.update(outcome="not_reached", error="RuntimeError: private SDK detail")
    record = _persist(database, _PRECISE, result)
    core, store, snapshot = _publish(database)
    detail = _read(database, snapshot, "detail", record_id=record["id"], limit=20, offset=0)
    tables = detail["tables"]
    symbols = [dict(zip(tables["symbols"]["columns"], row, strict=True)) for row in tables["symbols"]["rows"]]
    attempts = [dict(zip(tables["attempts"]["columns"], row, strict=True)) for row in tables["attempts"]["rows"]]
    public = json.loads(core.execution_records(store, 1, 0, 10))["data"][0]["symbol_results"]["A"]
    assert symbols[0]["status"] == public["status"] == "PARTIAL"
    assert symbols[0]["error"] == attempts[0]["reason"] == public["error"] == "执行不到位"
    assert (
        json.loads(core.execution_record_detail(store, 1, record["id"]))["result"]["symbol_results"]["A"]["error"]
        == symbol["error"]
    )


def test_daily_costs_use_execution_bounds_and_preserve_cross_day_unknown_amounts(database: Path) -> None:
    result = copy.deepcopy(_RESULT)
    symbol = result["symbol_results"]["A"]
    symbol["first_tick"] = {"bid_price": 98.0, "ask_price": 98.0}
    symbol["trades"] = [
        {
            "order_id": "o",
            "trade_time": "2026-01-01T23:59:00+08:00",
            "trade_price": 100,
            "trade_volume": 1,
            "extra": {"commission": 0, "commission_asset": "CNY"},
        },
        {
            "order_id": "o",
            "trade_time": "2026-01-02T00:01:00+08:00",
            "trade_volume": 1,
            "extra": {"commission": -0.2, "commission_asset": "USD"},
        },
        {
            "order_id": "o",
            "trade_time": "2026-01-02T00:02:00+08:00",
            "trade_volume": 1,
            "extra": {"commission": 0, "commission_asset": "USD"},
        },
    ]
    _persist(database, "2026-01-01T23:59:00+08:00", result, "old")
    for identity, when in (("middle", "2026-01-10T09:00:00"), ("latest", "2026-02-15T09:00:00")):
        current = copy.deepcopy(_RESULT)
        current["symbol_results"]["A"]["trades"][0]["trade_time"] = when
        _persist(database, when, current, identity)
    _, _, snapshot = _publish(database)
    assert _read(database, snapshot, "costs", dimension="trade")["count"] == 5
    for range_key in ["all", "90"]:
        daily = _read(database, snapshot, "chart", range=range_key)["daily_costs"]
        assert daily["2026-01-01"]["value"] == 100
        assert daily["2026-01-01"]["cost"] == 2
        assert daily["2026-01-01"]["lossBp"] == pytest.approx(2 / 98 * 10000)
        assert daily["2026-01-01"]["fees"] == {"CNY": 0}
        assert daily["2026-01-02"]["value"] is None
        assert daily["2026-01-02"]["cost"] is None
        assert daily["2026-01-02"]["count"] == 2
        assert daily["2026-01-02"]["amountComplete"] is False
        assert daily["2026-01-02"]["fees"] == {"USD": -0.2}
    assert {day: v["count"] for day, v in _read(database, snapshot, "chart", range="30")["daily_costs"].items()} == {
        "2026-01-10": 1,
        "2026-02-15": 1,
    }
