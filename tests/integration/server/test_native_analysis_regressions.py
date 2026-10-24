"""历史时间与展示规则经过真实原生发布后保持一致。"""

import copy
import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from tests.conftest import native_store_call, require_native_store
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._execution_test_support import build_account

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
    return native_store_call(
        database,
        "persist_execute_record",
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


def _publish(database: Path):
    core = require_native_store("publish_store", "query_costs", "execution_detail")
    core.queue_enqueue(str(database), 1, False)
    row = core.queue_claim(str(database), 1.0, "regression-snapshot")
    receipt = native_store_call(
        database,
        "publish_store",
        {
            "command": "publish_store",
            "protocol": 1,
            "account_id": 1,
            "expected_source_version": row["source_version"],
            "snapshot_id": row["running_token"],
            "claim_token": row["running_token"],
            "logic_version": LOGIC_VERSION,
            "engine_version": ENGINE_VERSION,
            "computed_at": _NOW,
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
            "trade_channel": "tq",
        },
    )
    assert receipt["published"] is True
    return core, core.Store(str(database)), receipt["snapshot_id"]


def test_cost_interval_keeps_the_trade_at_its_inclusive_microsecond_end(database: Path) -> None:
    _persist(database, _PRECISE, _RESULT)
    core, store, snapshot = _publish(database)
    end = datetime.fromisoformat(_PRECISE).timestamp() * 1000
    page = json.loads(
        core.query_costs(
            store,
            1,
            snapshot,
            "all",
            "trade",
            "time",
            None,
            end - 1000,
            end,
            None,
            None,
            None,
            None,
            20,
            "precision",
            None,
            LOGIC_VERSION,
            ENGINE_VERSION,
        )
    )
    assert page["count"] == 1
    assert page["data"][0]["time"] == end
    assert page["summary"]["fees"] == {"CNY": 0.0}


@pytest.mark.parametrize("created_at", ["2026-01-02 09:00:00", "2026-01-02"])
def test_legacy_time_formats_publish_without_moving_the_trade_day(database: Path, created_at: str) -> None:
    result = copy.deepcopy(_RESULT)
    result["symbol_results"]["A"]["trades"][0]["trade_time"] = "2026-01-01 09:00:00"
    _persist(database, created_at, result)
    core, store, _ = _publish(database)
    activity = json.loads(core.activity_symbols(store, 1, "2026-01-02", "2026-01-03", "A"))
    trade = activity["data"][0]["trades"][0]
    assert trade["day"] == "2026-01-01"
    assert trade["timeEstimated"] is False
    assert trade["time"] == datetime(2026, 1, 1, 9, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000


def test_published_legacy_attempt_and_symbol_match_the_public_execution_display(database: Path) -> None:
    result = copy.deepcopy(_RESULT)
    result.pop("status")
    result["outcome"] = "not_reached"
    symbol = result["symbol_results"]["A"]
    symbol.pop("status")
    symbol.update(outcome="not_reached", error="RuntimeError: private SDK detail")
    record = _persist(database, _PRECISE, result)
    core, store, snapshot = _publish(database)
    detail = json.loads(core.execution_detail(store, 1, snapshot, record["id"], 20, 0))
    tables = detail["tables"]
    symbols = [dict(zip(tables["symbols"]["columns"], row, strict=True)) for row in tables["symbols"]["rows"]]
    attempts = [dict(zip(tables["attempts"]["columns"], row, strict=True)) for row in tables["attempts"]["rows"]]
    public = json.loads(core.execution_records(store, 1, 0, 10))["data"][0]["symbol_results"]["A"]
    assert symbols[0]["status"] == public["status"] == "PARTIAL"
    assert symbols[0]["error"] == attempts[0]["reason"] == public["error"] == "执行不到位"
    assert (
        core.execution_business_detail(store, 1, record["id"])["result"]["symbol_results"]["A"]["error"]
        == symbol["error"]
    )


def test_daily_costs_use_execution_bounds_and_preserve_cross_day_unknown_amounts(database: Path) -> None:
    result = copy.deepcopy(_RESULT)
    symbol = result["symbol_results"]["A"]
    symbol["first_tick"] = {"bid_price": 98.0, "ask_price": 98.0}
    symbol["trades"] = [
        {
            "order_id": "o",
            "trade_time": "2026-01-01T23:59:00",
            "trade_price": 100,
            "trade_volume": 1,
            "extra": {"commission": 0, "commission_asset": "CNY"},
        },
        {
            "order_id": "o",
            "trade_time": "2026-01-02T00:01:00",
            "trade_volume": 1,
            "extra": {"commission": -0.2, "commission_asset": "USD"},
        },
        {
            "order_id": "o",
            "trade_time": "2026-01-02T00:02:00",
            "trade_volume": 1,
            "extra": {"commission": 0, "commission_asset": "USD"},
        },
    ]
    _persist(database, "2026-01-01T23:59:00", result, "old")
    for identity, when in (("middle", "2026-01-10T09:00:00"), ("latest", "2026-02-15T09:00:00")):
        current = copy.deepcopy(_RESULT)
        current["symbol_results"]["A"]["trades"][0]["trade_time"] = when
        _persist(database, when, current, identity)
    _, _, snapshot = _publish(database)
    with closing(oracle_connect(database)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM performance_cost_trade WHERE snapshot_id=?", (snapshot,)
            ).fetchone()[0]
            == 5
        )
        for range_key in ("all", "90"):
            costs = connection.execute(
                "SELECT day,value,cost,loss_bp,coverage,covered,count,fee_covered,amount_complete "
                "FROM performance_result_daily_cost WHERE snapshot_id=? AND range_key=? AND day<'2026-01-10' ORDER BY day",
                (snapshot, range_key),
            ).fetchall()
            assert costs == [
                ("2026-01-01", 100.0, 2.0, 2.0 / 98.0 * 10000.0, 1.0, 1, 1, 1, 1),
                ("2026-01-02", None, None, None, None, 0, 2, 2, 0),
            ]
            assert connection.execute(
                "SELECT day,currency,value FROM performance_result_daily_fee "
                "WHERE snapshot_id=? AND range_key=? AND day<'2026-01-10' ORDER BY day,currency",
                (snapshot, range_key),
            ).fetchall() == [("2026-01-01", "CNY", 0.0), ("2026-01-02", "USD", -0.2)]
        assert connection.execute(
            "SELECT day,count FROM performance_result_daily_cost WHERE snapshot_id=? AND range_key='30' ORDER BY day",
            (snapshot,),
        ).fetchall() == [("2026-01-10", 1), ("2026-02-15", 1)]
