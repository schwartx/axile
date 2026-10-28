"""实际 main 的 0033 数据库是最终存储迁移的兼容基线。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from axile.server._native import _core
from axile.server.business_facts.artifacts import parse_artifact_reference
from tests.conftest import native_store_call
from tests.sqlite_oracle import register_time_function
from tests.unit.server._native_read_views import artifact_view, read_view


@pytest.fixture
def main_database(tmp_path):
    path = tmp_path / "main.db"
    source = Path(__file__).parents[2] / "fixtures/rust_store/main_0033.sql"
    with closing(sqlite3.connect(path)) as connection, connection:
        register_time_function(connection)
        connection.executescript(source.read_text())
    return path


def _dump(path):
    _core.close_database(str(path))
    with closing(sqlite3.connect(path)) as connection, connection:
        register_time_function(connection)
        return list(connection.iterdump())


def test_main_upgrade_downgrade_repeat_preserves_facts_and_complete_artifacts(main_database):
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        artifacts = connection.execute("SELECT content FROM execution_artifact ORDER BY id").fetchall()
    for _ in range(2):
        assert native_store_call(main_database, "migration_upgrade", {})["public"] == "0036"
        detail = read_view(main_database, "read_execution_detail", {"account_id": 1, "record_id": 1})
        assert detail["input"]["extra"]["sentinel"] == 9007199254740993
        assert detail["result"]["account_assets"]["total_asset"] == 101.0
        _core.close_database(str(main_database))
        with closing(sqlite3.connect(main_database)) as connection, connection:
            register_time_function(connection)
            assert connection.execute("SELECT content FROM execution_artifact ORDER BY id").fetchall() == artifacts
            assert not connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'analysis_source_%' "
                "OR name LIKE 'replay_observation%' OR name IN ('performance_cost_range','performance_execution_summary')"
            ).fetchall()
        assert native_store_call(main_database, "migration_downgrade", {"public": "0033"})["public"] == "0033"
        _core.close_database(str(main_database))
        with closing(sqlite3.connect(main_database)) as connection, connection:
            register_time_function(connection)
            rows = connection.execute("SELECT raw_input,raw_result FROM executerecord ORDER BY id").fetchall()
            assert len(rows) == 3
            assert json.loads(rows[0][0])["extra"]["sentinel"] == 9007199254740993
            assert json.loads(rows[0][1])["extra"]["audit"] == "main-body"
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_main_conversion_failure_rolls_back_schema_rows_and_revision(main_database):
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        connection.execute("UPDATE executerecord SET raw_input='invalid JSON' WHERE id=2")
    before = _dump(main_database)
    with pytest.raises(_core.NativeError):
        native_store_call(main_database, "migration_upgrade", {})
    assert _dump(main_database) == before


@pytest.mark.parametrize(
    "targets",
    [
        {"curr_target": {"A": 0.5}, "account_target": {"A": 0.5}},
        {"account_target": {}},
        {"curr_target": {"A": 0.5}, "account_target": {"B": 0.25}},
        {"curr_target": None, "account_target": None},
        {
            "curr_target": {"A": 0.5},
            "account_target": {"B": 0.25},
            "account_weights": {},
            "normalized_weights": None,
            "strategy_target": {"A": 0.75},
            "strategy_weights": {"B": 0.5},
            "raw_weights": {},
        },
    ],
)
def test_0034_preserves_target_aliases_and_selected_fields_on_repeated_migration(main_database, targets):
    content = {**targets, "sizing_context": {"weight_precision": None}, "label": "legacy-target"}
    rules = {"trade_rules": None, "label": "legacy-rules"}
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        for row_id, kind, payload in ((4, "TARGET_SNAPSHOT", content), (5, "TRADE_RULES_SNAPSHOT", rules)):
            connection.execute(
                "INSERT INTO execution_artifact VALUES('main-1',?,1,?,'2026-10-01',?)",
                (kind, json.dumps(payload), row_id),
            )
        connection.execute(
            "INSERT INTO execution_event(execution_id,account_id,channel,algorithm,schema_version,event_uid,"
            "event_type,status,reason_family,reason_code,ts_local_created,ts_persisted,seq,details,id) "
            "VALUES('main-1',1,'tq','synthetic',1,'legacy-target','TARGET_COMPUTED','INFO','INPUT',"
            "'COMMON.TARGET_COMPUTED','2026-10-01','2026-10-01',1,?,1)",
            (json.dumps({"decision": content}),),
        )
    for _ in range(2):
        native_store_call(main_database, "migration_upgrade", {})
        _core.close_database(str(main_database))
        with closing(sqlite3.connect(main_database)) as connection:
            register_time_function(connection)
            rows = connection.execute(
                "SELECT artifact_type,content FROM execution_artifact WHERE id IN (4,5) ORDER BY id"
            ).fetchall()
        for (kind, body), original in zip(rows, (content, rules), strict=True):
            compacted = json.loads(body)
            reference = compacted["business_facts_ref"]
            assert parse_artifact_reference(compacted) == reference
            assert set(reference["select_fields"]) == set(original) - {"label"}
            assert set(compacted) == {"label", "business_facts_ref"}
            hydrated = artifact_view(
                main_database, {"execution_id": "main-1", "artifact_type": kind, "content": compacted}
            )
            hydrated.pop("business_facts_ref")
            assert hydrated == original
        events = json.loads(
            _core.list_execution_events_json(str(main_database), json.dumps({"execution_id": "main-1"}))
        )
        decision = events["data"][0]["details"]["decision"]
        decision.pop("business_facts_ref")
        assert decision == content
        native_store_call(main_database, "migration_downgrade", {"public": "0033"})
        _core.close_database(str(main_database))
        with closing(sqlite3.connect(main_database)) as connection:
            register_time_function(connection)
            restored = connection.execute(
                "SELECT content,schema_version FROM execution_artifact WHERE id IN (4,5) ORDER BY id"
            ).fetchall()
            assert [(json.loads(body), version) for body, version in restored] == [(content, 1), (rules, 1)]
            assert json.loads(connection.execute("SELECT details FROM execution_event WHERE id=1").fetchone()[0]) == {
                "decision": content
            }


def test_0034_invalid_artifact_selector_blocks_downgrade_without_mutation(main_database):
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        connection.execute(
            "INSERT INTO execution_artifact VALUES('main-1','TARGET_SNAPSHOT',1,?,'2026-10-01',4)",
            (json.dumps({"account_target": {"A": 0.5}}),),
        )
    native_store_call(main_database, "migration_upgrade", {})
    _core.close_database(str(main_database))
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        content = json.loads(connection.execute("SELECT content FROM execution_artifact WHERE id=4").fetchone()[0])
        content["business_facts_ref"]["select_fields"] = ["channel_type"]
        connection.execute("UPDATE execution_artifact SET content=? WHERE id=4", (json.dumps(content),))
    before = _dump(main_database)
    with pytest.raises(_core.NativeError):
        native_store_call(main_database, "migration_downgrade", {"public": "0033"})
    assert _dump(main_database) == before


def test_0034_preserves_explicit_nulls_without_inventing_missing_fields(main_database):
    sizing = {"reason_code": None, "reference_price": None, "min_quantity": None}
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        raw_input, raw_result = connection.execute(
            "SELECT raw_input,raw_result FROM executerecord WHERE id=1"
        ).fetchone()
        payload, result = json.loads(raw_input), json.loads(raw_result)
        payload.update(trade_rules=None, sizing_context={"weight_precision": None})
        result.update(
            inputs=payload,
            error=None,
            reason_code=None,
            outcome_reason=None,
            termination={"reason": None},
            symbol_results={"A": {"error": None, "reason_code": None, "sizing": sizing}},
            orders=[{"order_id": "o-1", "price": None}],
            trades=[{"trade_id": "t-1", "trade_price": None}],
        )
        result["account_assets"].update(available_cash=None, positions=[{"symbol": "A", "volume": None}])
        connection.execute(
            "UPDATE executerecord SET raw_input=?,raw_result=? WHERE id=1",
            (json.dumps(payload), json.dumps(result)),
        )

    def check(actual):
        assert actual["error"] is None
        assert actual["reason_code"] is None
        assert actual["outcome_reason"] is None
        assert actual["termination"]["reason"] is None
        assert "trigger" not in actual["termination"]
        symbol = actual["symbol_results"]["A"]
        assert symbol["error"] is None and symbol["reason_code"] is None
        assert symbol["sizing"] == sizing
        assert "outcome_reason" not in symbol
        assert actual["orders"][0]["price"] is None
        assert "avg_price" not in actual["orders"][0]
        assert actual["trades"][0]["trade_price"] is None
        assert actual["account_assets"]["available_cash"] is None
        assert actual["account_assets"]["positions"][0]["volume"] is None
        assert actual["inputs"]["trade_rules"] is None
        assert actual["inputs"]["sizing_context"] == {"weight_precision": None}

    for _ in range(2):
        native_store_call(main_database, "migration_upgrade", {})
        detail = read_view(main_database, "read_execution_detail", {"account_id": 1, "record_id": 1})
        check(detail["result"])
        assert detail["input"]["sizing_context"] == {"weight_precision": None}
        native_store_call(main_database, "migration_downgrade", {"public": "0033"})
        _core.close_database(str(main_database))
        with closing(sqlite3.connect(main_database)) as connection:
            register_time_function(connection)
            restored = json.loads(connection.execute("SELECT raw_result FROM executerecord WHERE id=1").fetchone()[0])
            check(restored)


def test_old_cache_is_requeued_and_its_original_body_is_backed_up(main_database):
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        connection.execute("UPDATE performance_snapshot SET ranges='invalid JSON'")
    receipt = native_store_call(main_database, "migration_upgrade", {})
    result = json.loads(_core.snapshot(_core.Store(str(main_database)), 1, "all", "18", "equity-ledger-1"))
    assert result["status"] == "pending"
    assert result["result"] is None
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        assert connection.execute(
            "SELECT requested,current_snapshot FROM account_analysis WHERE account_id=1"
        ).fetchone() == (1, None)
    native_store_call(main_database, "migration_downgrade", {"public": "0033"})
    with closing(sqlite3.connect(receipt["backup"])) as connection, connection:
        register_time_function(connection)
        assert connection.execute("SELECT ranges FROM performance_snapshot").fetchone() == ("invalid JSON",)


def test_new_publication_and_downgrade_preserve_main_business_facts(main_database):
    native_store_call(main_database, "migration_upgrade", {})
    claim = _core.queue_claim(str(main_database), 1792890000.0, "main-publish")
    assert claim is not None
    receipt = native_store_call(
        main_database,
        "publish_store",
        {
            "command": "publish_store",
            "protocol": 1,
            "account_id": 1,
            "expected_source_version": claim["source_version"],
            "snapshot_id": "main-publish",
            "claim_token": "main-publish",
            "logic_version": "18",
            "engine_version": "equity-ledger-1",
            "computed_at": "2026-10-25T10:00:00",
            "trade_channel": "tq",
            "include_backtest": False,
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0},
        },
    )
    assert receipt["published"] is True
    native_store_call(main_database, "migration_downgrade", {"public": "0033"})
    with closing(sqlite3.connect(main_database)) as connection, connection:
        register_time_function(connection)
        assert connection.execute("SELECT COUNT(*) FROM performance_snapshot").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executerecord").fetchone() == (3,)
        result = json.loads(connection.execute("SELECT raw_result FROM executerecord WHERE id=3").fetchone()[0])
        assert result["account_assets"]["total_asset"] == 103.0


@pytest.mark.parametrize("revision", ["0037", "0038", "0039", "0040", "0041"])
def test_unpublished_revisions_are_rejected_without_creating_a_database(tmp_path, revision):
    path = tmp_path / "missing.db"
    with pytest.raises(_core.NativeError):
        native_store_call(path, "migration_upgrade", {"public": revision})
    assert not path.exists()
