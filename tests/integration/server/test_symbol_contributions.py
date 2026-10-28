"""真实迁移、业务写入和快照读取共同验证品种归因。"""

import json

import pytest

from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._migration_support import _connect
from tests.unit.server._native_read_views import persist_record_fixture


def seed_account(path, *, null_conversion=False, zero_target_close=False, missing_fill=False, missing_valuation=False):
    """生成禁止调度的合成期货账户，包含期初有仓、部分平仓和费用缺失。"""
    initialize_native_database(path)
    account = build_account(trade_channel="tq", portfolio_id=None, is_started=False, backtest_fee_rate=0.0)
    native_store_call(
        path, "account_create", {"account": account.model_dump(mode="json"), "now": "2026-01-01T09:00:00"}
    )
    for day, quantity, value, equity in [(1, 2, 2000, 1000), (2, 1, 1200, 1299), (3, 0, 0, 1349)]:
        time = f"2026-01-0{day}T09:00:00"
        position = {
            "symbol": "fu2701",
            "volume": quantity,
            "direction": "多头",
            "market_value": None if missing_valuation and day == 2 else value,
            "extra": {"tq_symbol": "SHFE.fu2701"},
        }
        trades = (
            []
            if day == 1 or missing_fill and day == 2
            else [
                {
                    "symbol": "fu2701",
                    "trade_id": f"fill-{day}",
                    "order_id": f"order-{day}",
                    "trade_time": time,
                    "trade_price": 110 if day == 2 else 125,
                    "trade_volume": 1,
                    **({"value_conversion": None} if null_conversion else {}),
                    "extra": {
                        "direction": "SELL",
                        "tq_symbol": "SHFE.fu2701",
                        **({"commission": 1, "commission_asset": "CNY"} if day == 2 else {}),
                    },
                }
            ]
        )
        persist_record_fixture(
            path,
            {
                "account_id": 1,
                "execution_id": f"contribution-{day}",
                "created_at": time,
                "is_success": 1,
                "input_id": f"input-{day}",
                "result_input_id": f"result-input-{day}",
                "raw_input": {"curr_target": {}},
                "raw_result": {
                    "status": "SUCCEEDED",
                    "account_assets": {
                        "total_asset": equity,
                        "currency": "CNY",
                        "source": "real",
                        "positions": [position] if quantity else [],
                    },
                    "symbol_results": {
                        "fu2701": {
                            "status": "SUCCEEDED",
                            "sizing": {"unit_multiplier": None if zero_target_close and day == 3 else 10},
                            "trades": trades,
                        }
                    },
                },
            },
        )
    core = require_native_store("publish_store", "queue_enqueue", "queue_claim", "chart")
    core.queue_enqueue(str(path), 1, False)
    claim = core.queue_claim(str(path), 1.0, "contribution-snapshot")
    from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION

    receipt = native_store_call(
        path,
        "publish_store",
        {
            "command": "publish_store",
            "protocol": 1,
            "account_id": 1,
            "expected_source_version": claim["source_version"],
            "snapshot_id": claim["running_token"],
            "claim_token": claim["running_token"],
            "logic_version": LOGIC_VERSION,
            "engine_version": ENGINE_VERSION,
            "computed_at": "2026-01-03T09:00:00+08:00",
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
            "trade_channel": "tq",
            "include_backtest": False,
        },
    )
    assert receipt["published"] is True
    return json.loads(core.chart(core.Store(str(path)), 1, claim["running_token"], "all"))


@pytest.mark.parametrize("null_conversion", [False, True])
@pytest.mark.parametrize("zero_target_close", [False, True])
def test_published_contributions_use_account_observations_and_keep_closed_contract(
    tmp_path, null_conversion, zero_target_close
):
    result = seed_account(
        tmp_path / "contribution.db", null_conversion=null_conversion, zero_target_close=zero_target_close
    )
    table = result["tables"]["contributions"]
    rows = [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
    last = rows[-1]
    assert last == {
        "idx": len(result["tables"]["points"]["rows"]) - 1,
        "symbol": "fu2701",
        "product": "SHFE.fu",
        "currency": "CNY",
        "gross_pnl": 350.0,
        "fee": 1.0,
        "fee_missing": 1,
        "issues": 0,
        "trades": 2,
    }
    core = require_native_store("chart")
    with pytest.raises(core.NativeError, match="SNAPSHOT"):
        core.chart(core.Store(str(tmp_path / "contribution.db")), 1, "missing", "all")


@pytest.mark.parametrize("gap", ["missing_fill", "missing_valuation"])
def test_published_gap_keeps_later_observations_available_without_erasing_quality(tmp_path, gap):
    result = seed_account(tmp_path / "gap.db", **{gap: True})
    table = result["tables"]["contributions"]
    rows = [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
    assert len(rows) == len(result["tables"]["points"]["rows"])
    assert rows[0]["issues"] == 0
    before, after = rows[-2:]
    assert before["issues"] > 0
    assert after["issues"] == before["issues"]
    assert after["gross_pnl"] is not None
    if gap == "missing_fill":
        assert after["gross_pnl"] - before["gross_pnl"] == 50.0
    else:
        assert before["gross_pnl"] is None


def _inspect(path):
    with _connect(path) as connection:
        return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='performance_contribution'"
            )
        ]


def test_contribution_migration_upgrades_existing_0035_and_downgrades_cleanly(tmp_path):
    path = tmp_path / "previous.db"
    initialize_native_database(path, revision="0035")
    assert _inspect(path) == ("0035", [])
    initialize_native_database(path)
    assert _inspect(path) == ("0036", [("performance_contribution",)])
    native_store_call(path, "migration_downgrade", {"public": "0035"})
    assert _inspect(path) == ("0035", [])
    initialize_native_database(path)
    assert _inspect(path)[0] == "0036"
