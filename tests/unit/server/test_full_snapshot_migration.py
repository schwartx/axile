"""0041 展开旧引用，提交后摘要与通知均独立于被引用记录。"""

import json
from contextlib import closing
from copy import deepcopy

import pytest

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows, sql
from tests.unit.server._legacy_snapshot_support import notification_reference


def _schema(path):
    with closing(oracle_connect(path)) as connection:
        return connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def _legacy_snapshot(path, execution_id="snapshot-1"):
    capture = {
        "execution": {"status": "PARTIAL", "notified_at": "original"},
        "orders": [{"id": "0"}],
        "assets": {"total_assets": 0.0, "market_value": None},
        "positions": [],
    }
    event = deepcopy(capture)
    event["execution"].update(id=execution_id, notified_at="later")
    event_id = f"execution:{execution_id}"
    event["event_id"] = event_id
    payload = {
        "symbol_results": {"A": {"status": "PARTIAL", "orders": [], "sizing": {"target_quantity": 1}}},
        "extra": {"server_notification_context": capture},
    }
    journal = {
        "symbol_results": {"A": {"orders": [], "sizing": {"target_quantity": 2}}},
        "independent": {"null": None, "zero": 0},
    }
    native_store_call(
        path,
        "append_execution_artifact",
        {
            "execution_id": execution_id,
            "artifact_type": "execution_summary",
            "schema_version": 2,
            "content": journal,
            "created_at": "original",
        },
    )
    record = persist_result(path, payload, execution_id=execution_id)
    full_summary = rows(path, "execution_artifact")[-1]["content"]
    full_summary["symbol_results"]["A"] = {
        **read_result(path, record)["symbol_results"]["A"],
        **full_summary["symbol_results"]["A"],
    }
    compact = deepcopy(full_summary)
    compact["symbol_results"]["A"].pop("orders")
    compact["business_facts_ref"] = {
        "version": 1,
        "view": "result",
        "record_id": record["id"],
        "projection_version": 2,
        "legacy_schema_version": 2,
    }
    sql(
        path,
        "UPDATE execution_artifact SET content=?,schema_version=3 WHERE execution_id=?",
        (json.dumps(compact), execution_id),
    )
    sql(
        path,
        "INSERT INTO notification_event(id,account_id,event_type,context,created_at,status) VALUES(?,1,'execution.finished',?,'later','pending')",
        (event_id, json.dumps(event)),
    )
    reference = notification_reference(capture, event, event_id)
    assert reference is not None
    sql(
        path,
        "UPDATE execution_extension SET extra=? WHERE record_id=?",
        (json.dumps({"server_notification_context": reference}), record["id"]),
    )
    return record, capture, full_summary


def test_expand_old_references_and_keep_snapshots_after_deleting_sources(tmp_path):
    path = tmp_path / "snapshots.db"
    initialize_native_database(path, revision="0040")
    record, capture, summary = _legacy_snapshot(path)
    initialize_native_database(path)
    assert read_result(path, record)["extra"]["server_notification_context"] == capture
    artifact = rows(path, "execution_artifact")[0]
    assert artifact["content"] == summary and artifact["schema_version"] == 2
    assert not [
        name
        for kind, name, _ in _schema(path)
        if kind == "trigger" and ("_summary_" in name or "notification_outbox_" in name)
    ]
    for target in ("0040", "head"):
        operation = "migration_downgrade" if target == "0040" else "migration_upgrade"
        native_store_call(path, operation, {"public": target})
        assert rows(path, "execution_artifact")[0]["content"] == summary
        assert read_result(path, record)["extra"]["server_notification_context"] == capture
    sql(path, "DELETE FROM notification_event")
    assert native_store_call(path, "notifications_preview", {"account_id": 1})["context"] == capture
    sql(path, "DELETE FROM executerecord WHERE id=?", (record["id"],))
    assert rows(path, "execution_artifact")[0] == artifact
    assert native_store_call(path, "resolve_execution_artifact", {"artifact": artifact, "account_id": 1}) == summary


def test_old_references_can_downgrade_directly_and_upgrade_again(tmp_path):
    path = tmp_path / "direct-rollback.db"
    initialize_native_database(path, revision="0040")
    record, capture, summary = _legacy_snapshot(path)
    native_store_call(path, "migration_downgrade", {"public": "0033"})
    with closing(oracle_connect(path)) as connection:
        result = json.loads(
            connection.execute("SELECT raw_result FROM executerecord WHERE id=?", (record["id"],)).fetchone()[0]
        )
    assert result["extra"]["server_notification_context"] == capture
    assert rows(path, "execution_artifact")[0]["content"] == summary
    initialize_native_database(path)
    assert read_result(path, record)["extra"]["server_notification_context"] == capture
    assert rows(path, "execution_artifact")[0]["content"] == summary


def test_failed_expansion_rolls_back_earlier_rows_and_version(tmp_path):
    path = tmp_path / "rollback.db"
    initialize_native_database(path, revision="0040")
    _legacy_snapshot(path)
    record, _, _ = _legacy_snapshot(path, "snapshot-2")
    extra = rows(path, "execution_extension")[-1]["extra"]
    extra["server_notification_context"]["capture_sha256"] = "0" * 64
    sql(path, "UPDATE execution_extension SET extra=? WHERE record_id=?", (json.dumps(extra), record["id"]))
    tables = ("execution_extension", "execution_artifact", "account_analysis")
    before = {table: rows(path, table) for table in tables}
    schema = _schema(path)
    with pytest.raises(_core.NativeError, match="notification capture digest mismatch"):
        initialize_native_database(path)
    assert native_store_call(path, "migration_current", {})["public"] == "0040"
    assert {table: rows(path, table) for table in tables} == before
    assert _schema(path) == schema


@pytest.mark.parametrize(
    "field,value",
    [("record_id", value) for value in (True, 0, -1, "1", 1.0, None, 9999)]
    + [("projection_version", value) for value in (True, "2", 2.0, None, 1)],
)
def test_invalid_summary_reference_aborts_migration(tmp_path, field, value):
    path = tmp_path / "invalid-summary.db"
    initialize_native_database(path, revision="0040")
    _legacy_snapshot(path)
    content = rows(path, "execution_artifact")[0]["content"]
    content["business_facts_ref"][field] = value
    sql(path, "UPDATE execution_artifact SET content=?", (json.dumps(content),))
    before = rows(path, "execution_extension")
    with pytest.raises(_core.NativeError):
        initialize_native_database(path)
    assert native_store_call(path, "migration_current", {})["public"] == "0040"
    assert rows(path, "execution_extension") == before
    assert rows(path, "execution_artifact")[0]["content"] == content
