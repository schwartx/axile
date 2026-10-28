"""五表模型的增量、重放、原子提交与规范事实读取验收。"""

import json
from datetime import datetime, timedelta

import pytest

from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._incremental_analysis_support import sql
from tests.unit.server._native_read_views import persist_record_fixture

TABLES = {
    "analysis_state",
    "analysis_replay_position",
    "analysis_contribution_state",
    "analysis_day",
    "analysis_contribution_day",
}


@pytest.fixture
def account(native_database):
    a = build_account(trade_channel="tq", portfolio_id=None, is_started=False, backtest_fee_rate=0.001)
    native_store_call(
        native_database, "account_create", {"account": a.model_dump(mode="json"), "now": "2026-01-01T09:00:00"}
    )
    return native_database


def append(path, index, *, hour=9, target=True, price=None):
    time = (datetime(2026, 1, 1, hour) + timedelta(days=index - 1)).isoformat()
    identity = f"e-{index}-{hour}"
    price = 100 + index if price is None else price
    return persist_record_fixture(
        path,
        {
            "account_id": 1,
            "execution_id": identity,
            "created_at": time,
            "is_success": 1,
            "input_id": f"in-{identity}",
            "result_input_id": f"out-{identity}",
            "raw_input": {"curr_target": {"A": 1}} if target else {},
            "raw_result": {
                "status": "SUCCEEDED",
                "account_assets": {"total_asset": price, "source": "real", "currency": "CNY", "positions": []},
                "symbol_results": {
                    "A": {
                        "status": "SUCCEEDED",
                        "first_tick": {"bid_price": price - 1, "ask_price": price + 1, "last_price": price},
                        "sizing": {
                            "sizing_mode": "weight",
                            "status": "SIZED",
                            "current_quantity": 0,
                            "target_quantity": 1,
                            "equity": price,
                            "unit_notional": price,
                            "reference_price": price,
                            "unit_multiplier": 1,
                        },
                        "trades": [
                            {
                                "trade_id": identity,
                                "order_id": identity,
                                "trade_time": time,
                                "trade_price": price,
                                "trade_volume": 1,
                                "extra": {"direction": "BUY", "commission": 0, "commission_asset": "CNY"},
                            }
                        ],
                    }
                },
            },
        },
    )


def claim(path, token="test"):
    core = require_native_store()
    core.queue_enqueue(str(path), 1, False)
    row = core.queue_claim(str(path), 1, token)
    assert row
    return row


def update(path, row=None):
    row = row or claim(path)
    return native_store_call(
        path,
        "analysis_update",
        {
            "account_id": 1,
            "expected_source_version": row["source_version"],
            "claim_token": row["running_token"],
            "computed_at": "2026-05-01T10:00:00",
        },
    )


def query(path, command="chart", **kwargs):
    version = str(sql(path, "SELECT version FROM analysis_state WHERE account_id=1 AND range_key='all'")[0][0])
    return native_store_call(
        path, "analysis_query", {"account_id": 1, "command": command, "version": version, **kwargs}
    )


def assert_full_replay(path):
    core = require_native_store()
    store = core.Store(str(path))
    for key in ["30", "90", "all"]:
        full = json.loads(core.account_performance(store, 1, key))
        actual = query(path, range=key)["result"]
        for field in [
            "observation_count",
            "used_record_count",
            "target_used_record_count",
            "invalid_asset_count",
            "skips",
            "target_skips",
        ]:
            assert actual[field] == full[field], (key, field)
        assert len(actual["points"]) == len(full["points"])
        for a, b in zip(actual["points"], full["points"], strict=True):
            for field, value in b.items():
                if isinstance(value, float):
                    assert a[field] == pytest.approx(value), (key, field, a, b)
                else:
                    assert a[field] == value, (key, field, a, b)


def test_append_intraday_restart_and_gap_equal_full_replay(account):
    append(account, 1)
    append(account, 2)
    assert update(account)["processed_observations"] == 6
    assert_full_replay(account)
    closed = sql(account, "SELECT * FROM analysis_day WHERE day='2026-01-01' ORDER BY range_key")
    for day, hour, target in [(3, 9, True), (3, 12, True), (3, 15, False), (4, 9, True), (5, 9, False), (6, 9, True)]:
        require_native_store().close_database(str(account))
        append(account, day, hour=hour, target=target)
        assert update(account)["processed_observations"] == 3
        assert_full_replay(account)
    assert sql(account, "SELECT * FROM analysis_day WHERE day='2026-01-01' ORDER BY range_key") == closed


def test_late_same_time_revision_and_deletion_replay_safely(account):
    append(account, 1)
    append(account, 2)
    update(account)
    append(account, 1, hour=12)
    assert update(account)["processed_observations"] == 9
    assert_full_replay(account)
    sql(account, "UPDATE business_asset_snapshot SET total_asset=150 WHERE record_id=1")
    update(account)
    assert_full_replay(account)
    sql(account, "DELETE FROM executerecord WHERE id=2")
    update(account)
    assert_full_replay(account)
    append(account, 1, hour=9, price=120)
    update(account)
    assert_full_replay(account)


def test_stale_claim_never_advances_any_state_and_recovery_resumes(account):
    append(account, 1)
    update(account)
    previous = sql(account, "SELECT version,max_id FROM analysis_state ORDER BY range_key")
    append(account, 2)
    row = claim(account)
    append(account, 3)
    assert not update(account, row)["updated"]
    assert sql(account, "SELECT version,max_id FROM analysis_state ORDER BY range_key") == previous
    core = require_native_store()
    core.queue_recover(str(account), "24", "equity-ledger-1")
    receipt = update(account)
    assert receipt["updated"]
    assert_full_replay(account)
    assert native_store_call(
        account,
        "analysis_commit_confirmed",
        {
            "account_id": 1,
            "source_version": receipt["version"],
            "logic_version": "24",
            "engine_version": "equity-ledger-1",
        },
    )


def test_facts_read_on_demand_versions_and_filter_cursors(account):
    append(account, 1)
    record = append(account, 2)
    update(account)
    detail = query(account, "detail", record_id=record["id"])
    assert detail["header"]["record_id"] == record["id"]
    execution = query(account, "costs", dimension="execution", limit=1)["data"][0]
    assert not {"transactions", "positions", "attempts"} & execution.keys()
    assert execution["symbols"] == ["A"]
    costs = query(account, "costs", dimension="trade", limit=1)
    assert len(costs["data"]) == 1 and costs["next_cursor"]
    second = query(account, "costs", dimension="trade", limit=1, cursor=costs["next_cursor"])
    assert second["data"] != costs["data"]
    with pytest.raises(require_native_store().NativeError, match="ANALYSIS_VERSION_CHANGED"):
        query(account, "costs", dimension="trade", limit=1, cursor=costs["next_cursor"], symbol="B")
    append(account, 3)
    with pytest.raises(require_native_store().NativeError, match="ANALYSIS_VERSION_CHANGED"):
        query(account, "detail", record_id=record["id"])


def test_upgrade_removes_nineteen_tables_and_keeps_facts(tmp_path):
    path = tmp_path / "upgrade.db"
    initialize_native_database(path, revision="0038")
    old = {r[0] for r in sql(path, "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'performance_%'")}
    assert len(old) == 19
    sql(
        path,
        "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(1,'keep','期货','tq','','2026-01-01','2026-01-01')",
    )
    initialize_native_database(path)
    names = {r[0] for r in sql(path, "SELECT name FROM sqlite_schema WHERE type='table'")}
    assert not names & old
    assert TABLES <= names
    assert sql(path, "SELECT name FROM account WHERE id=1") == [("keep",)]
    assert not {"current_snapshot", "previous_snapshot", "published_version"} & {
        r[1] for r in sql(path, "PRAGMA table_info(account_analysis)")
    }
    native_store_call(path, "migration_downgrade", {"public": "0038"})
    assert old <= {r[0] for r in sql(path, "SELECT name FROM sqlite_schema WHERE type='table'")}
    initialize_native_database(path)
    assert sql(path, "PRAGMA foreign_key_check") == []


def test_writer_failure_rolls_back_all_five_tables_and_cursor(account):
    append(account, 1)
    update(account)
    before = {t: sql(account, f"SELECT * FROM {t} ORDER BY 1,2") for t in TABLES}
    append(account, 2)
    row = claim(account)
    sql(
        account,
        "CREATE TRIGGER refuse_result BEFORE INSERT ON analysis_contribution_day BEGIN SELECT RAISE(ABORT,'injected write failure'); END",
    )
    with pytest.raises(require_native_store().NativeError, match="injected write failure"):
        update(account, row)
    assert {t: sql(account, f"SELECT * FROM {t} ORDER BY 1,2") for t in TABLES} == before
    sql(account, "DROP TRIGGER refuse_result")
    assert update(account, row)["updated"]
    assert_full_replay(account)


def test_rolling_baseline_rebuilds_only_its_range(account):
    append(account, 1)
    append(account, 2)
    append(account, 31)
    update(account)
    append(account, 32)
    assert update(account)["processed_observations"] == 5  # 30-day: three observations; 90/all: one each.
    assert_full_replay(account)


def test_empty_account_restart_and_first_append(account):
    assert update(account)["processed_observations"] == 0
    assert query(account)["result"]["points"] == []
    append(account, 1)
    assert update(account)["processed_observations"] == 3
    assert_full_replay(account)


def test_duplicate_fill_identity_and_conflict_do_not_double_cash_flow(account):
    append(account, 1)
    append(account, 2)
    update(account)
    before = query(account)["contributions"][-1]
    append(account, 3, price=102)
    sql(
        account,
        "UPDATE business_trade SET trade_id=(SELECT trade_id FROM business_trade WHERE record_id=2) WHERE record_id=3",
    )
    update(account)
    last = query(account)["contributions"][-1]
    assert last["trades"] == before["trades"] == 1
    assert last["gross_pnl"] == before["gross_pnl"]
    sql(account, "UPDATE business_trade SET trade_volume=2 WHERE record_id=3")
    update(account)
    conflict = query(account)["contributions"][-1]
    assert conflict["trades"] == 1
    assert conflict["gross_pnl"] == before["gross_pnl"]
    assert conflict["issues"] > last["issues"]


def test_fill_seen_before_baseline_is_not_new_cash_flow(account):
    append(account, 1)
    update(account)
    append(account, 2, price=101)
    sql(
        account,
        "UPDATE business_trade SET trade_id=(SELECT trade_id FROM business_trade WHERE record_id=1) WHERE record_id=2",
    )
    update(account)
    last = query(account)["contributions"][-1]
    assert last["trades"] == 0 and last["gross_pnl"] == 0
