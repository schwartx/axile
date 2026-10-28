"""真实迁移与增量分页：账户隔离、稳定游标、修正、删除和大小上限。"""

import copy
import json
from contextlib import closing

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call
from tests.integration.server.test_native_analysis_regressions import _RESULT
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import persist_record_fixture


def state(path, account=1):
    return json.loads(_core.browser_analysis_state(str(path), json.dumps({"account_id": account})))


def page(path, current, cursor=None):
    return json.loads(
        _core.browser_analysis_facts(
            str(path),
            json.dumps(
                {
                    "account_id": current["account_id"],
                    "source_id": current["source_id"],
                    "until": current["cursor"],
                    "cursor": cursor or {"seq": "0", "table": 22, "after": None},
                }
            ),
        )
    )


def pages(path, current, cursor=None):
    result = []
    while True:
        part = page(path, current, cursor)
        assert sum(len(p.get("rows", [])) for p in part["parts"]) <= 4096
        assert len(json.dumps(part).encode()) <= 2 * 1024 * 1024
        result.extend(part["parts"])
        cursor = part["cursor"]
        if part["complete"]:
            return result, cursor


def write(path, sql, values=()):
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(sql, values)


def record(path, account=1, name="one"):
    return persist_record_fixture(
        path,
        {
            "account_id": account,
            "execution_id": name,
            "created_at": "2026-01-01T09:00:00",
            "is_success": 1,
            "input_id": f"input-{name}",
            "result_input_id": f"result-{name}",
            "raw_input": {"curr_target": {f"A{i:05d}": 1.0 for i in range(4200)}},
            "raw_result": copy.deepcopy(_RESULT),
        },
    )


@pytest.fixture
def database(native_database):
    for account_id in (1, 2):
        account = build_account(id=account_id, trade_channel="ctp", portfolio_id=None)
        native_store_call(
            native_database, "account_create", {"account": account.model_dump(mode="json"), "now": "2026-01-01"}
        )
    return native_database


def test_packed_pages_resume_across_tables_and_are_account_scoped(database):
    record(database)
    record(database, 2, "other")
    current = state(database)
    first = page(database, current)
    assert not first["complete"]
    assert isinstance(first["cursor"]["seq"], str)
    _core.close_database(str(database))
    assert state(database) == current  # no process identity or restart invalidation
    parts, cursor = pages(database, current, first["cursor"])
    parts = first["parts"] + parts
    assert [r for p in parts if p["table"] == "account" for r in p["rows"]] == [[1, "ctp"]]
    targets = [r for p in parts if p["table"] == "execution_input_target" for r in p["rows"] if r[0] == "input-one"]
    assert len(targets) == 4200
    assert len({tuple(row[:3]) for row in targets}) == 4200
    assert all(r[1] == 1 for p in parts if p["table"] == "executerecord" for r in p["rows"])
    assert not pages(database, current, cursor)[0]


def test_delta_includes_corrections_and_deletes_without_restarting(database):
    record(database)
    before = state(database)
    _, cursor = pages(database, before)
    write(database, "UPDATE execution_symbol_tick SET last_price=123 WHERE record_id=1")
    changed = state(database)
    assert int(changed["cursor"]) > int(before["cursor"])
    parts, cursor = pages(database, changed, cursor)
    assert {p["record_id"] for p in parts} == {"1"}
    assert any(r[6] == 123 for p in parts if p["table"] == "execution_symbol_tick" for r in p["rows"])
    write(database, "DELETE FROM executerecord WHERE id=1")
    parts, _ = pages(database, state(database), cursor)
    assert parts == [{"seq": parts[0]["seq"], "record_id": "1", "table": None, "deleted": True}]
    with pytest.raises(_core.NativeError, match="SOURCE_CHANGED"):
        page(database, {**changed, "source_id": "other-database"})


def test_revision_moving_during_partial_download_is_caught_by_next_goal(database):
    record(database)
    old = state(database)
    first = page(database, old)
    write(database, "UPDATE executerecord SET msg='corrected' WHERE id=1")
    parts, cursor = pages(database, old, first["cursor"])
    assert not any(p["record_id"] == "1" and p["table"] is None for p in parts)
    parts, _ = pages(database, state(database), cursor)
    assert any(r[12] == "corrected" for p in parts if p["table"] == "executerecord" for r in p["rows"])


def test_calendar_channel_and_noop_updates(database):
    before = state(database)
    write(database, "INSERT INTO analysis_calendar_day VALUES('ctp','2026-01-01','open','fixture','test')")
    changed = state(database)
    assert int(changed["cursor"]) > int(before["cursor"])
    write(database, "UPDATE analysis_calendar_day SET status=status")
    assert state(database) == changed
    write(database, "INSERT INTO analysis_calendar_day VALUES('binance','2026-01-01','closed','fixture','test')")
    assert state(database) == changed
    parts, _ = pages(database, changed)
    assert [r[2] for p in parts if p["table"] == "analysis_calendar_day" for r in p["rows"]] == ["open"]


def test_large_fact_fails_without_unbounded_page(database):
    record(database)
    write(database, "UPDATE execution_input SET curr_target_state=? WHERE id='input-one'", ("x" * (2 * 1024 * 1024),))
    with pytest.raises(_core.NativeError, match="FACT_TOO_LARGE"):
        page(database, state(database))


def test_0037_roundtrip_keeps_business_facts_and_calendar(database):
    write(database, "INSERT INTO analysis_calendar_day VALUES('ctp','2026-01-01','open','fixture','test')")
    before = state(database)
    native_store_call(database, "migration_downgrade", {"public": "0036"})
    initialize_native_database(database)
    after = state(database)
    assert after["source_version"] == before["source_version"]
    assert after["source_id"] != before["source_id"]
    parts, _ = pages(database, after)
    assert [r for p in parts if p["table"] == "account" for r in p["rows"]] == [[1, "ctp"]]
    with closing(oracle_connect(database)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_existing_0037_upgrades_without_changing_identity_or_facts(database):
    record(database)
    before = state(database)
    native_store_call(database, "migration_downgrade", {"public": "0037"})
    with closing(oracle_connect(database)) as connection:
        assert connection.execute("SELECT identity FROM analysis_source").fetchone()[0] == before["source_id"]
        assert connection.execute("SELECT count(*) FROM executerecord").fetchone()[0] == 1
        assert not connection.execute("SELECT name FROM sqlite_schema WHERE name='analysis_change'").fetchall()
    initialize_native_database(database)
    after = state(database)
    assert after["source_id"] == before["source_id"]
    assert after["source_version"] == before["source_version"]
    parts, _ = pages(database, after)
    assert {p["record_id"] for p in parts} == {"0", "1"}
    with closing(oracle_connect(database)) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT account_id,id FROM executerecord WHERE input_id=? OR result_input_id=?",
            ("input-one", "input-one"),
        ).fetchall()
        assert not any("SCAN executerecord" in row[-1] for row in plan)
