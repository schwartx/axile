"""执行事实只保存一次；任务结算与迁移保持原子性。"""

import json

import pytest

from axile.server._native import _core
from axile.server.execution.execution_summaries import execution_result_view
from tests.conftest import initialize_native_database, native_store_call
from tests.unit.server._business_facts_test_support import read_result, rows, seed_account, sql

_PRIVATE_RULES = {
    "revision": "axon_0001",
    "version_table": "axon_alembic_version",
    "backup_table": "axon_0001_error_backup",
    "dependency": "0013",
    "codes": {"BINANCE": {"-2019": "保证金不足"}},
}


def persist(path, **changes):
    return native_store_call(
        path,
        "persist_execution_state",
        {
            "account_id": 1,
            "execution_id": "derived",
            "created_at": "2026-10-28",
            "input_id": "input",
            "result_input_id": "result-input",
            "is_success": 1,
            "raw_result": {"success": True, "status": "SUCCEEDED", "symbol_results": {}},
            **changes,
        },
    )


def test_worker_facts_survive_parent_failure_and_repeated_response(native_database):
    seed_account(native_database)
    persist(native_database, stage_only=True)
    staged = native_store_call(
        native_database, "execution_read", {"op": "detail", "execution_id": "derived", "account_id": 1}
    )
    assert staged["result"]["status"] == "SUCCEEDED"
    assert json.loads(_core.execution_records(_core.Store(str(native_database)), 1, 0, 10))["count"] == 0
    with pytest.raises(_core.NativeError, match="成功标识"):
        persist(native_database, is_success=2)
    assert rows(native_database, "executerecord")[0]["result_staged"] == 1
    receipt = persist(native_database, is_success=0, raw_result={"task_status": "FAILED", "error": "parent failed"})
    assert receipt["status"] == "FAILED"
    assert receipt["error"] == "parent failed"
    persist(native_database, stage_only=True)
    again = persist(native_database)
    assert again["status"] == "FAILED"
    result = native_store_call(
        native_database, "execution_read", {"op": "detail", "execution_id": "derived", "account_id": 1}
    )
    assert result["result"]["status"] == "SUCCEEDED"
    assert result["id"] == staged["id"]


def test_parent_attaches_input_and_termination_without_replacing_worker_facts(native_database):
    seed_account(native_database)
    worker = {"success": True, "status": "SUCCEEDED", "symbol_results": {"A": {"final_volume": 1}}}
    persist(native_database, stage_only=True, raw_result=worker)
    parent = {
        **worker,
        "inputs": {"curr_target": {"A": 1}},
        "execution_kind": "clear_positions",
        "task_status": "TERMINATED",
        "termination": {"mode": "cancel_pending", "cancel_failed_order_ids": ["o1"], "forced": True},
    }
    receipt = persist(native_database, raw_input={"curr_target": {}}, raw_result=parent)
    detail = json.loads(_core.execution_record_detail(_core.Store(str(native_database)), 1, receipt["record_id"]))
    assert detail["result"]["inputs"]["curr_target"] == {"A": 1}
    assert detail["input"]["curr_target"] == {}
    assert detail["result"]["execution_kind"] == "clear_positions"
    assert detail["result"]["task_status"] == "TERMINATED"
    assert detail["result"]["termination"]["cancel_failed_order_ids"] == ["o1"]
    assert detail["result"]["symbol_results"]["A"]["final_volume"] == 1
    seed_account(native_database, account_id=2)
    with pytest.raises(_core.NativeError, match="其他账户"):
        persist(native_database, account_id=2, raw_result=parent)


def test_migration_conflict_rolls_back_version_and_summary(tmp_path):
    path = tmp_path / "conflict.db"
    initialize_native_database(path, revision="0033")
    seed_account(path)
    sql(
        path,
        "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES('orphan','EXECUTION_SUMMARY',2,'{}','t')",
    )
    with pytest.raises(_core.NativeError, match="no complete result facts"):
        native_store_call(path, "migration_upgrade", {})
    assert native_store_call(path, "migration_current", {})["public"] == "0033"


def test_new_database_rejects_persisted_summary(native_database):
    with pytest.raises(Exception, match="derived from facts"):
        sql(
            native_database,
            "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES('x','EXECUTION_SUMMARY',2,'{}','t')",
        )


def test_result_writer_discards_reconciliation_instead_of_stashing_json(native_database):
    seed_account(native_database)
    receipt = persist(native_database, raw_result={"status": "SUCCEEDED", "reconciliation": {"symbols": []}})
    detail = json.loads(_core.execution_record_detail(_core.Store(str(native_database)), 1, receipt["record_id"]))
    assert "reconciliation" not in detail["result"]
    assert "reconciliation" not in json.dumps(rows(native_database, "execution_extension"))


@pytest.mark.parametrize("public", ["head", "0034"])
def test_private_error_patch_and_restore_without_reconciliation_column(native_database, public):
    seed_account(native_database)
    receipt = persist(
        native_database,
        is_success=0,
        raw_result={
            "channel_type": "binance",
            "status": "FAILED",
            "error": "-2019",
            "symbol_results": {"A": {"error": "-2019"}},
            "extra": {"kept": False},
        },
    )
    record = {"id": receipt["record_id"]}
    native_store_call(native_database, "migration_upgrade", {"private_rules": _PRIVATE_RULES})
    patched = read_result(native_database, record)
    assert patched["error"] == "保证金不足"
    assert patched["symbol_results"]["A"]["error"] == "保证金不足"
    assert "reconciliation" not in json.dumps(rows(native_database, "execution_extension"))

    restored = native_store_call(
        native_database,
        "migration_downgrade",
        {"public": public, "private": "base", "private_rules": _PRIVATE_RULES},
    )
    assert restored["private"] is None
    result = read_result(native_database, record)
    assert result["error"] == "-2019"
    assert result["symbol_results"]["A"]["error"] == "-2019"
    assert result["extra"] == {"kept": False}
    assert "reconciliation" not in result


@pytest.mark.parametrize("edit_fact", [False, True])
def test_summary_backup_cleanup_preserves_private_restore_and_conflict_checks(tmp_path, edit_fact):
    path = tmp_path / "summary-backup.db"
    initialize_native_database(path, revision="0033")
    seed_account(path)
    result = {"channel_type": "binance", "status": "FAILED", "error": "-2019"}
    sql(
        path,
        "INSERT INTO executerecord(account_id,execution_id,is_success,created_at,raw_input,raw_result) "
        "VALUES(1,'derived',0,'2026-10-28','{}',?)",
        (json.dumps(result),),
    )
    for kind, content in [
        ("EXECUTION_SUMMARY", {"status": "FAILED", "error": "-2019"}),
        ("DIAGNOSTIC", {"keep": "original evidence"}),
    ]:
        sql(
            path,
            "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES('derived',?,2,?,'2026-10-28')",
            (kind, json.dumps(content)),
        )
    request = {"private_rules": _PRIVATE_RULES}
    native_store_call(path, "migration_upgrade", request)
    backups = rows(path, "axon_0001_error_backup")
    assert backups
    assert {backup["table_name"] for backup in backups} == {"executerecord"}
    assert [artifact["artifact_type"] for artifact in rows(path, "execution_artifact")] == ["DIAGNOSTIC"]
    downgrade = {**request, "public": "0033", "private": "base"}
    if edit_fact:
        sql(path, "UPDATE executerecord SET error='edited after migration' WHERE execution_id='derived'")
        with pytest.raises(_core.NativeError, match="restore conflict"):
            native_store_call(path, "migration_downgrade", downgrade)
        assert native_store_call(path, "migration_current", {})["public"] == "0034"
        assert rows(path, "axon_0001_error_backup") == backups
        return
    native_store_call(path, "migration_downgrade", downgrade)
    assert rows(path, "executerecord")[0]["raw_result"]["error"] == "-2019"
    assert [artifact["artifact_type"] for artifact in rows(path, "execution_artifact")] == ["DIAGNOSTIC"]


def test_legacy_independent_reconciliation_is_rejected_before_dropping_json(tmp_path):
    path = tmp_path / "independent-reconciliation.db"
    initialize_native_database(path, revision="0033")
    seed_account(path)
    result = {"status": "FAILED", "reconciliation": {"independent": "keep"}}
    sql(
        path,
        "INSERT INTO executerecord(account_id,is_success,created_at,raw_input,raw_result) VALUES(1,0,'2026-10-28','{}',?)",
        (json.dumps(result),),
    )
    with pytest.raises(_core.NativeError, match="independent reconciliation"):
        native_store_call(path, "migration_upgrade", {})
    assert native_store_call(path, "migration_current", {})["public"] == "0033"
    assert rows(path, "executerecord")[0]["raw_result"] == result


def test_downgrade_rejects_pending_worker_result(native_database):
    seed_account(native_database)
    persist(native_database, stage_only=True)
    with pytest.raises(_core.NativeError, match="cannot be downgraded"):
        native_store_call(native_database, "migration_downgrade", {"public": "0033"})
    assert native_store_call(native_database, "migration_current", {})["public"] == "0034"


def test_read_view_uses_frozen_before_observation_and_keeps_unknowns():
    result = {"success": True, "symbol_results": {"A": {"status": "NOOP", "target_volume": 1, "final_volume": 1}}}
    view = execution_result_view(result, {"source": "real", "total_asset": 0, "positions": []})
    assert view["reconciliation"]["account"]["equity_before"] == 0
    assert view["reconciliation"]["account"]["equity_after"] is None
    assert "reconciliation" not in result


@pytest.mark.parametrize("conflict", [None, "extra", "nested", "extension", "contradiction"])
def test_migration_removes_only_derivable_summary(tmp_path, conflict):
    path = tmp_path / "summary.db"
    initialize_native_database(path, revision="0033")
    seed_account(path)
    sql(
        path,
        "INSERT INTO executerecord(account_id,execution_id,is_success,created_at,raw_input,raw_result) "
        "VALUES(1,'derived',1,'2026-10-28','{}',?)",
        (json.dumps({"success": True, "status": "SUCCEEDED", "symbol_results": {}}),),
    )
    summary = {"success": True, "summary": {}, "reconciliation": {"symbols": []}}
    if conflict == "extra":
        summary["journal_note"] = "independent observation"
    if conflict == "nested":
        summary["reconciliation"]["symbols"] = [{"symbol": "A", "tca": {"journal_metric": 1}}]
    if conflict == "extension":
        sql(
            path,
            "UPDATE executerecord SET raw_result=?",
            (json.dumps({"status": "SUCCEEDED", "reconciliation": {"independent": "keep"}}),),
        )
    if conflict == "contradiction":
        summary["success"] = False
    sql(
        path,
        "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES('derived','EXECUTION_SUMMARY',2,?,'t')",
        (json.dumps(summary),),
    )
    if conflict:
        with pytest.raises(_core.NativeError, match="independent"):
            native_store_call(path, "migration_upgrade", {})
        assert native_store_call(path, "migration_current", {})["public"] == "0033"
        assert len(rows(path, "execution_artifact")) == 1
    else:
        receipt = native_store_call(path, "migration_upgrade", {})
        assert receipt["backup"]
        assert native_store_call(path, "migration_current", {})["public"] == "0034"
        assert rows(path, "execution_artifact") == []
        assert "reconciliation" not in rows(path, "execution_extension")[0]


def test_history_deletion_is_not_exposed():
    from axile.server.api.routes.account_crud import router

    assert not hasattr(_core, "delete_execution_history")
    assert not any(
        route.path == "/execute_records/{account_id}" and "DELETE" in route.methods for route in router.routes
    )
