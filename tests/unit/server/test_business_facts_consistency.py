"""业务事实在重放、事务失败和 SQLite 外键开关下保持同一执行语义。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest

from axile.domain.execution import ExecutionArtifactType
from axile.server._native import _core
from axile.server.business_facts import repository
from axile.server.business_facts.artifacts import REFERENCE_KEY, make_artifact_reference
from axile.server.db.models import ExecuteRecord, ExecutionArtifact
from scripts.business_facts_contract import business_evidence
from tests.conftest import initialize_native_database, native_store_call
from tests.fixtures.rust_store.oracle_tables import table
from tests.unit.server._execution_test_support import build_account, insert_account

_CREATED = "2026-10-20T09:30:00.000001"


def _fact_tables():
    """冻结原事实域清单；原生迁移负责真实表、索引和触发器。"""
    names = (
        "execution_input",
        "execution_input_target",
        "execution_input_symbol",
        "execution_input_plan",
        "executerecord",
        "execution_termination_cancel_failure",
        "execution_symbol_collection",
        "execution_symbol_result",
        "execution_symbol_sizing",
        "execution_symbol_tick",
        "execution_symbol_conversion",
        "execution_symbol_target",
        "business_asset_snapshot",
        "business_asset_position",
        "business_asset_binding",
        "execution_order_collection",
        "execution_order",
        "business_trade_collection",
        "business_trade",
        "business_trade_conversion",
        "execution_extension",
    )
    return tuple(table(name) for name in names)


@pytest.fixture(autouse=True)
def _database_path(monkeypatch, tmp_path):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(tmp_path / "fact-consistency.db"))


@contextmanager
def _connection(path, foreign_keys=False):
    """每次原生调用前关闭测试连接，外键模式只作用于显式 SQL。"""
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == int(foreign_keys)
        with connection:
            yield connection


def _database(tmp_path):
    path = tmp_path / "fact-consistency.db"
    initialize_native_database(path)
    with _connection(path) as connection:
        for account_id in (1, 2):
            insert_account(connection, build_account(id=account_id, name=f"facts-{account_id}", portfolio_id=None))
            connection.execute("INSERT OR IGNORE INTO account_analysis(account_id) VALUES (?)", (account_id,))
    return path


def _rows(path, sql, parameters=()):
    with _connection(path) as connection:
        return [dict(row) for row in connection.execute(sql, parameters)]


def _scalar(path, sql, parameters=()):
    with _connection(path) as connection:
        row = connection.execute(sql, parameters).fetchone()
        assert row is not None
        return row[0]


def _counts(path, tables=None):
    return {item.name: _scalar(path, f'SELECT COUNT(*) FROM "{item.name}"') for item in tables or _fact_tables()}


def _version(path, account_id=1):
    return _scalar(path, "SELECT source_version FROM account_analysis WHERE account_id=?", (account_id,))


def _projection_rows(path, record_id):
    return {
        item.name: _rows(path, f'SELECT * FROM "{item.name}" WHERE record_id=?', (record_id,))
        for item in _fact_tables()
        if "record_id" in item.c
    }


def _freeze(path, *, account_id=1, execution_id="facts-one", payload=None, created_at=_CREATED, before_assets=None):
    request = {
        "account_id": account_id,
        "execution_id": execution_id,
        "input_id": str(uuid4()),
        "payload": _input() if payload is None else payload,
        "created_at": created_at,
    }
    if before_assets is not None:
        request["before_assets"] = before_assets
    return native_store_call(path, "freeze_execution_input", request)["input_id"]


def _store(path, *, execution_id="facts-one", account_id=1, payload=None, result=None, is_success=1):
    return ExecuteRecord.model_validate(
        native_store_call(
            path,
            "persist_execute_record",
            {
                "account_id": account_id,
                "execution_id": execution_id,
                "created_at": _CREATED,
                "is_success": is_success,
                "input_id": str(uuid4()),
                "result_input_id": str(uuid4()),
                "raw_input": _input() if payload is None else payload,
                "raw_result": _result() if result is None else result,
            },
        )
    )


def _input_view(input_id):
    return asyncio.run(repository.read_execution_input(input_id))


def _result_view(record):
    return asyncio.run(repository.read_execution_result(record))


def _append_artifact(path, execution_id, kind, content, schema_version=3):
    artifact = ExecutionArtifact(
        execution_id=execution_id,
        artifact_type=kind,
        schema_version=schema_version,
        content=content,
        created_at=_CREATED,
    )
    assert native_store_call(path, "append_execution_artifact", artifact.model_dump(mode="json"))
    return _artifact(path, execution_id)


def _artifact(path, execution_id):
    (row,) = _rows(path, "SELECT * FROM execution_artifact WHERE execution_id=?", (execution_id,))
    row["content"] = json.loads(row["content"])
    row["artifact_type"] = row["artifact_type"].lower()
    return ExecutionArtifact.model_validate(row)


def _resolved(path, execution_id):
    result = native_store_call(path, "list_execution_artifacts", {"execution_id": execution_id})
    assert result["count"] == 1
    return result["data"][0]["content"]


def _input(value=0.25):
    return {"channel_type": "CTP", "curr_target": {"A": value}, "last_target": {}}


def _result(*, inputs=None):
    payload = {
        "status": "SUCCEEDED",
        "success": True,
        "execution_time": 0.125,
        "account_assets": {
            "total_asset": 1000.0,
            "available_cash": 800.0,
            "market_value": 200.0,
            "currency": "CNY",
            "positions": [{"symbol": "A", "volume": 2.0, "direction": "LONG"}],
        },
        "symbol_results": {
            "A": {
                "status": "SUCCEEDED",
                "sizing": {"unit_multiplier": 1, "target_quantity": 2},
                "first_tick": {"last_price": 100.0},
                "orders": [{"order_id": "order-A", "symbol": "A", "direction": "BUY", "volume": 2, "price": 100}],
                "trades": [
                    {
                        "trade_id": "trade-A",
                        "order_id": "order-A",
                        "symbol": "A",
                        "trade_price": 100.0,
                        "trade_volume": 2.0,
                        "trade_time": "2026-10-20T09:30:01.123456",
                        "trade_value": 200.0,
                        "extra": {"commission": 0, "commission_asset": "CNY"},
                    }
                ],
            }
        },
    }
    if inputs is not None:
        payload["inputs"] = inputs
    return payload


def _summary_content(result):
    """使用真实 worker 的正文结构，另保留独立于业务结果的审计附注。"""
    from axile.server.execution.execution_summaries import (
        build_execution_outcome_details,
        build_execution_summary_from_symbol_results,
        build_symbol_reconciliation,
    )

    return {
        **build_execution_outcome_details(result),
        "summary": build_execution_summary_from_symbol_results(result),
        "success": result.get("success", True),
        "execution_time": result.get("execution_time"),
        "reconciliation": build_symbol_reconciliation(result, None),
        "independent_evidence": {"review_note": "retain journal after history deletion", "observed_zero": 0},
    }


def _wire_json(value):
    """比较原始线上的字段顺序与数值表示，不对键排序或补默认值。"""
    import json

    return json.dumps(
        business_evidence(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _store_summary(path, *, execution_id="facts-one", account_id=1):
    original = _summary_content(_result())
    _append_artifact(path, execution_id, ExecutionArtifactType.EXECUTION_SUMMARY, deepcopy(original), 2)
    record = _store(path, execution_id=execution_id, account_id=account_id)
    return record, _artifact(path, execution_id), original


def test_duplicate_terminal_write_keeps_first_facts_and_versions(tmp_path):
    """成功与终止路径重放时，首份终态及其事实图保持不变。"""
    path = _database(tmp_path)
    result = {"task_status": "TERMINATED", "error": "operator stopped", "status": "FAILED"}
    first = _store(path, execution_id="first-terminal-wins", result=result, is_success=0)
    counts, version = _counts(path), _version(path)
    second = _store(path, execution_id="first-terminal-wins", payload=_input(0.75))
    assert first.id == second.id and second.is_success == 0
    assert business_evidence(_result_view(second)) == business_evidence(result)
    assert _counts(path) == counts and _version(path) == version


def test_legacy_null_execution_ids_keep_independent_record_and_input_identity(tmp_path):
    """没有执行标识的历史记录不能因相同输入被合并。"""
    path = _database(tmp_path)
    first, second = _store(path, execution_id=None), _store(path, execution_id=None)
    rows = _rows(path, "SELECT * FROM executerecord")
    assert len(rows) == 2 and all(row["execution_id"] is None for row in rows)
    assert first.id != second.id and first.input_id != second.input_id
    assert business_evidence(_input_view(first.input_id)) == business_evidence(_input())
    assert business_evidence(_input_view(second.input_id)) == business_evidence(_input())


@pytest.mark.parametrize("different", [False, True], ids=["shared-input", "distinct-inputs"])
def test_input_and_result_input_share_only_equal_payloads(tmp_path, different):
    """头输入与结果输入只有内容相等时才共享事实。"""
    path = _database(tmp_path)
    result_input = _input(0.5) if different else deepcopy(_input())
    record = _store(path, result=_result(inputs=result_input))
    assert (record.input_id != record.result_input_id) is different
    assert business_evidence(_input_view(record.input_id)) == business_evidence(_input())
    assert business_evidence(_input_view(record.result_input_id)) == business_evidence(result_input)
    assert business_evidence(_result_view(record)["inputs"]) == business_evidence(result_input)


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
@pytest.mark.parametrize("reference", ["input_id", "result_input_id"])
def test_cross_account_input_references_are_rejected_by_database(tmp_path, foreign_keys, reference):
    """直接写表不能把其他账户的输入挂到当前执行。"""
    path = _database(tmp_path)
    borrowed = _freeze(path, execution_id="before-only")
    with _connection(path, foreign_keys) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO executerecord(account_id,execution_id,created_at,is_success,{reference}) "
                "VALUES(2,'cross-account',?,1,?)",
                (_CREATED, borrowed),
            )
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM executerecord").fetchone()[0] == 0


def test_result_write_failure_preserves_committed_preorder_input(tmp_path):
    """成交子行失败撤回整份终态，已提交的输入及前置资产仍可恢复。"""
    path = _database(tmp_path)
    input_id = _freeze(path, before_assets=_result()["account_assets"])
    counts, version = _counts(path), _version(path)
    with _connection(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_trade BEFORE INSERT ON business_trade "
            "BEGIN SELECT RAISE(ABORT,'synthetic final-result storage failure'); END"
        )
    with pytest.raises(_core.NativeError, match="synthetic final-result"):
        _store(path)
    assert _counts(path) == counts and _version(path) == version
    assert business_evidence(_input_view(input_id)) == business_evidence(_input())
    assert _scalar(path, "SELECT COUNT(*) FROM executerecord") == 0


def test_preorder_replay_preserves_identity_time_and_before_assets(tmp_path):
    """相同重放复用输入及前置资产，冲突重放不得覆盖原证据。"""
    path = _database(tmp_path)
    first = _freeze(path, execution_id="before-replay", before_assets=_result()["account_assets"])
    counts, version = _counts(path), _version(path)
    replay = _freeze(
        path, execution_id="before-replay", created_at="2026-10-20T10:00:00", before_assets={"total_asset": 9999}
    )
    assert replay == first
    assert _counts(path) == counts and _version(path) == version
    assert _scalar(path, "SELECT created_at FROM execution_input") == _CREATED
    assert _scalar(path, "SELECT total_asset FROM business_asset_snapshot") == 1000.0
    with pytest.raises(_core.NativeError):
        _freeze(path, execution_id="before-replay", payload=_input(0.75))
    assert business_evidence(_input_view(first)) == business_evidence(_input())
    assert _counts(path) == counts


@pytest.mark.parametrize("commit", [False, True], ids=["rollback", "commit"])
def test_child_change_invalidation_and_source_version_are_atomic(tmp_path, commit):
    """同数量成交改值也失效，回滚同时恢复值和版本。"""
    path = _database(tmp_path)
    record = _store(path)
    version = _version(path)
    with _connection(path) as connection:
        connection.execute("UPDATE business_trade SET trade_price=101 WHERE record_id=?", (record.id,))
        assert (
            connection.execute("SELECT source_version FROM account_analysis WHERE account_id=1").fetchone()[0] > version
        )
        connection.commit() if commit else connection.rollback()
    assert (_version(path) > version) is commit
    assert _scalar(path, "SELECT trade_price FROM business_trade") == (101.0 if commit else 100.0)


def test_result_input_target_mutation_invalidates_its_execution(tmp_path):
    """只通过 result_input_id 关联的目标也属于执行事实。"""
    path = _database(tmp_path)
    record = _store(path, result=_result(inputs=_input(0.5)))
    assert record.input_id != record.result_input_id
    version = _version(path)
    with _connection(path) as connection:
        connection.execute(
            "UPDATE execution_input_target SET value=0.75 WHERE input_id=? AND role='curr_target'",
            (record.result_input_id,),
        )
    assert _version(path) > version


def test_target_reparenting_invalidates_both_accounts(tmp_path):
    """目标子行换属主时，旧账户与新账户的源投影都失效。"""
    path = _database(tmp_path)
    first = _store(path)
    second = _store(path, execution_id="facts-two", account_id=2, payload=_input(0.5))
    with _connection(path) as connection:
        connection.execute("DELETE FROM execution_input_target WHERE input_id=?", (second.input_id,))
        connection.execute("UPDATE execution_input SET curr_target_count=0 WHERE id=?", (second.input_id,))
    versions = {account_id: _version(path, account_id) for account_id in (1, 2)}
    with _connection(path) as connection:
        connection.execute(
            "UPDATE execution_input_target SET input_id=? WHERE input_id=? AND role='curr_target'",
            (second.input_id, first.input_id),
        )
    for account_id in (1, 2):
        assert _version(path, account_id) > versions[account_id]


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_referenced_input_cannot_be_moved_to_another_account(tmp_path, foreign_keys):
    """已被终态引用的输入不能先改账户来绕过校验。"""
    path = _database(tmp_path)
    record = _store(path)
    with _connection(path, foreign_keys) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE execution_input SET account_id=2 WHERE id=?", (record.input_id,))
        connection.rollback()
        assert (
            connection.execute("SELECT account_id FROM execution_input WHERE id=?", (record.input_id,)).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_record_delete_removes_owned_facts_and_unreferenced_inputs(tmp_path, foreign_keys):
    """删除执行清理输入及各域子行，独立待执行输入保留。"""
    path = _database(tmp_path)
    pending = _freeze(path, execution_id="pending", payload=_input(0))
    record = _store(path, result=_result(inputs=_input(0.5)))
    with _connection(path, foreign_keys) as connection:
        # 本场景没有独立历史引用，保留原测试对末次事实引用的边界。
        connection.execute("DELETE FROM account_asset_snapshot WHERE execution_id=?", (record.execution_id,))
        connection.execute("DELETE FROM executerecord WHERE id=?", (record.id,))
    assert _rows(path, "SELECT id FROM execution_input") == [{"id": pending}]
    assert business_evidence(_input_view(pending)) == business_evidence(_input(0))
    for item in _fact_tables():
        if "record_id" in item.c or "asset_id" in item.c:
            assert _scalar(path, f'SELECT COUNT(*) FROM "{item.name}"') == 0, item.name
        if "input_id" in item.c:
            assert not {row["input_id"] for row in _rows(path, f'SELECT input_id FROM "{item.name}"')} - {pending}


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_delete_keeps_input_until_its_last_reference_is_removed(tmp_path, foreign_keys):
    """另一执行仍引用同一输入时，删除第一执行不能提前清理。"""
    path = _database(tmp_path)
    first = _store(path)
    second = _store(path, execution_id="facts-two", payload=_input(0.5))
    with _connection(path, foreign_keys) as connection:
        connection.execute("DELETE FROM account_asset_snapshot")
        connection.execute("UPDATE executerecord SET result_input_id=? WHERE id=?", (first.input_id, second.id))
        connection.execute("DELETE FROM executerecord WHERE id=?", (first.id,))
    assert business_evidence(_input_view(first.input_id)) == business_evidence(_input())
    assert business_evidence(_input_view(second.input_id)) == business_evidence(_input(0.5))
    with _connection(path, foreign_keys) as connection:
        connection.execute("DELETE FROM executerecord WHERE id=?", (second.id,))
    assert _scalar(path, "SELECT COUNT(*) FROM execution_input") == 0


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_terminal_delete_retains_immutable_input_artifact_and_before_assets(tmp_path, foreign_keys):
    """终态删除后，前置审计附件仍能解析输入及其资产图。"""
    path = _database(tmp_path)
    input_id = _freeze(path, before_assets=_result()["account_assets"])
    artifact = _append_artifact(
        path,
        "facts-one",
        ExecutionArtifactType.STANDARD_INPUT,
        make_artifact_reference(view="input", input_id=input_id),
    )
    record = _store(path)
    with _connection(path, foreign_keys) as connection:
        connection.execute("DELETE FROM executerecord WHERE id=?", (record.id,))
    assert _artifact(path, "facts-one").id == artifact.id
    assert business_evidence(_resolved(path, "facts-one")["input"]) == business_evidence(_input())
    (before,) = _rows(path, "SELECT * FROM business_asset_snapshot WHERE input_id=? AND scope='before'", (input_id,))
    assert before["scope"] == "before" and before["total_asset"] == 1000.0
    positions = _rows(path, "SELECT * FROM business_asset_position WHERE asset_id=?", (before["asset_id"],))
    assert len(positions) == 1 and positions[0]["symbol"] == "A" and positions[0]["volume"] == 2.0


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_terminal_delete_retains_asset_only_artifact_reference(tmp_path, foreign_keys):
    """仅含资产标识的附件在删除终态后仍保留观测与输入锚点。"""
    path = _database(tmp_path)
    record = _store(path)
    asset_id = _scalar(
        path, "SELECT asset_id FROM business_asset_binding WHERE record_id=? AND path='/account_assets'", (record.id,)
    )
    artifact = _append_artifact(
        path,
        "facts-one",
        ExecutionArtifactType.ACCOUNT_SNAPSHOT,
        make_artifact_reference(view="assets_after", asset_id=asset_id),
    )
    with _connection(path, foreign_keys) as connection:
        connection.execute("DELETE FROM executerecord WHERE id=?", (record.id,))
    assert _artifact(path, "facts-one").id == artifact.id
    assert business_evidence(_resolved(path, "facts-one")["account_assets"]) == business_evidence(
        _result()["account_assets"]
    )
    (row,) = _rows(path, "SELECT * FROM business_asset_snapshot WHERE asset_id=?", (asset_id,))
    assert row["record_id"] is None and row["input_id"] == record.input_id


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_account_delete_cleans_its_pending_and_final_graph_only(tmp_path, foreign_keys):
    """删除账户清理其待执行与终态图，其他账户详情不受影响。"""
    path = _database(tmp_path)
    _freeze(path, execution_id="never-finalized", payload=_input(0), before_assets=_result()["account_assets"])
    first = _store(path)
    second = _store(path, execution_id="facts-two", account_id=2)
    with _connection(path, foreign_keys) as connection:
        connection.execute("DELETE FROM account WHERE id=1")
    assert _rows(path, "SELECT * FROM executerecord WHERE id=?", (first.id,)) == []
    assert business_evidence(_result_view(second)) == business_evidence(_result())
    assert {row["account_id"] for row in _rows(path, "SELECT account_id FROM execution_input")} == {2}
    for item in _fact_tables():
        if "record_id" in item.c:
            assert not {row["record_id"] for row in _rows(path, f'SELECT record_id FROM "{item.name}"')} - {
                second.id,
                None,
            }
    fact_names = {item.name for item in _fact_tables()}
    with _connection(path, foreign_keys) as connection:
        assert [tuple(row) for row in connection.execute("PRAGMA foreign_key_check") if row[0] in fact_names] == []


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
@pytest.mark.parametrize("asset_account", [1, 2], ids=["other-execution", "other-account"])
def test_result_asset_binding_cannot_read_another_execution_observation(
    tmp_path, monkeypatch, foreign_keys, asset_account
):
    """指向其他执行的资产时，数据库或完整业务读取必须拒绝。"""
    path = _database(tmp_path)
    _freeze(path, account_id=asset_account, execution_id="unrelated-observation", payload=_input(0.75))
    borrowed = native_store_call(
        path,
        "freeze_execution_after_assets",
        {
            "account_id": asset_account,
            "execution_id": "unrelated-observation",
            "payload": {"account_assets": {"total_asset": 123456.0, "positions": []}},
        },
    )
    record = _store(path)
    try:
        with _connection(path, foreign_keys) as connection:
            connection.execute(
                "UPDATE business_asset_binding SET asset_id=? WHERE record_id=? AND path='/account_assets'",
                (borrowed["asset_id"], record.id),
            )
    except sqlite3.IntegrityError:
        return
    with pytest.raises(ValueError):
        asyncio.run(repository.read_execution_detail(record))


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
@pytest.mark.parametrize("asset_account", [1, 2], ids=["other-execution", "other-account"])
@pytest.mark.parametrize("include_positions", [False, True], ids=["scalar", "positions"])
def test_account_snapshot_cannot_read_another_execution_asset(
    tmp_path, monkeypatch, foreign_keys, asset_account, include_positions
):
    """执行历史资产引用被改归属时，标量与持仓读取都拒绝。"""
    path = _database(tmp_path)
    local = _store(path, execution_id="snapshot-local")
    (snapshot,) = _rows(path, "SELECT * FROM account_asset_snapshot WHERE execution_id=?", (local.execution_id,))
    _freeze(path, account_id=asset_account, execution_id="unrelated-observation")
    borrowed = native_store_call(
        path,
        "freeze_execution_after_assets",
        {
            "account_id": asset_account,
            "execution_id": "unrelated-observation",
            "payload": {"account_assets": {"total_asset": 123456.0, "positions": []}},
        },
    )
    try:
        with _connection(path, foreign_keys) as connection:
            connection.execute(
                "UPDATE account_asset_snapshot SET asset_fact_id=? WHERE id=?", (borrowed["asset_id"], snapshot["id"])
            )
    except sqlite3.IntegrityError:
        return
    snapshot["asset_fact_id"] = borrowed["asset_id"]
    with pytest.raises(_core.NativeError):
        native_store_call(
            path, "asset_snapshots_read", {"snapshots": [snapshot], "include_positions": include_positions}
        )


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_direct_terminal_delete_cannot_dangle_summary_reference(tmp_path, foreign_keys):
    """原始 SQL 删除不能使独立审计摘要的结果引用悬空。"""
    path = _database(tmp_path)
    record, artifact, original = _store_summary(path)
    assert artifact.content[REFERENCE_KEY]["view"] == "result"
    assert artifact.content[REFERENCE_KEY]["record_id"] == record.id
    assert artifact.content[REFERENCE_KEY]["projection_version"] == 2
    with _connection(path, foreign_keys) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM executerecord WHERE id=?", (record.id,))
        connection.rollback()
    assert _scalar(path, "SELECT COUNT(*) FROM executerecord WHERE id=?", (record.id,)) == 1
    resolved = _resolved(path, record.execution_id)
    resolved.pop(REFERENCE_KEY)
    assert _wire_json(resolved) == _wire_json(original)


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_application_history_delete_preserves_independent_summary_wire(tmp_path, foreign_keys):
    """应用删除物化摘要，独立审计正文与其他账户的引用保持原义。"""
    from axile.server.api.routes.account_crud import delete_execute_records

    path = _database(tmp_path)
    record, artifact, original = _store_summary(path)
    other, other_artifact, other_original = _store_summary(path, execution_id="other-account-summary", account_id=2)
    asyncio.run(delete_execute_records(account_id=1))
    assert _scalar(path, "SELECT COUNT(*) FROM executerecord WHERE id=?", (record.id,)) == 0
    assert _scalar(path, "SELECT COUNT(*) FROM executerecord WHERE id=?", (other.id,)) == 1
    journal = _artifact(path, record.execution_id)
    assert journal.id == artifact.id and journal.schema_version == 2
    assert REFERENCE_KEY not in journal.content
    assert _wire_json(journal.content) == _wire_json(original)
    assert _wire_json(_resolved(path, record.execution_id)) == _wire_json(original)
    untouched = _artifact(path, other.execution_id)
    assert untouched.id == other_artifact.id and untouched.content[REFERENCE_KEY]["view"] == "result"
    resolved = _resolved(path, other.execution_id)
    resolved.pop(REFERENCE_KEY)
    assert _wire_json(resolved) == _wire_json(other_original)


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_detach_and_delete_rollback_restore_original_summary_reference(tmp_path, foreign_keys):
    """删除后的故障回滚摘要物化、历史和整份事实图。"""
    path = _database(tmp_path)
    record, artifact, original = _store_summary(path)
    counts, version = _counts(path), _version(path)
    history = _rows(path, "SELECT * FROM account_asset_snapshot")
    with _connection(path, foreign_keys) as connection:
        connection.execute(
            "CREATE TRIGGER reject_history_delete AFTER DELETE ON executerecord BEGIN "
            "SELECT CASE WHEN EXISTS(SELECT 1 FROM execution_artifact WHERE "
            "json_extract(content,'$.business_facts_ref.view')='result') "
            "THEN RAISE(ABORT,'summary was not detached') END; "
            "SELECT RAISE(ABORT,'synthetic failure after detach and delete'); END"
        )
    with pytest.raises(_core.NativeError, match="synthetic failure after detach and delete"):
        native_store_call(path, "delete_execution_history", {"account_id": 1})
    assert _counts(path) == counts and _version(path) == version
    assert _rows(path, "SELECT * FROM account_asset_snapshot") == history
    journal = _artifact(path, record.execution_id)
    assert journal.id == artifact.id and journal.content[REFERENCE_KEY]["view"] == "result"
    resolved = _resolved(path, record.execution_id)
    resolved.pop(REFERENCE_KEY)
    assert _wire_json(resolved) == _wire_json(original)


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_terminal_compaction_rollback_keeps_previously_committed_worker_summary(tmp_path, foreign_keys):
    """摘要压缩之后失败，worker 已提交的完整正文与前置输入仍保留。"""
    path = _database(tmp_path)
    input_id = _freeze(path)
    original = _summary_content(_result())
    artifact = _append_artifact(path, "facts-one", ExecutionArtifactType.EXECUTION_SUMMARY, deepcopy(original), 2)
    counts = _counts(path)
    with _connection(path, foreign_keys) as connection:
        connection.execute(
            "CREATE TRIGGER reject_compaction AFTER UPDATE OF content ON execution_artifact "
            "WHEN json_extract(NEW.content,'$.business_facts_ref.view')='result' "
            "BEGIN SELECT RAISE(ABORT,'synthetic failure after summary compaction'); END"
        )
    with pytest.raises(_core.NativeError, match="synthetic failure after summary compaction"):
        _store(path)
    assert _scalar(path, "SELECT COUNT(*) FROM executerecord") == 0
    assert _counts(path) == counts
    assert business_evidence(_input_view(input_id)) == business_evidence(_input())
    journal = _artifact(path, "facts-one")
    assert journal.id == artifact.id and journal.schema_version == 2 and REFERENCE_KEY not in journal.content
    assert _wire_json(_resolved(path, "facts-one")) == _wire_json(original)


@pytest.mark.parametrize(
    ("account_id", "execution_id"),
    [(2, "facts-one"), (1, "unrelated-execution")],
    ids=["wrong-account", "wrong-execution"],
)
def test_summary_reference_rejects_foreign_account_or_execution(tmp_path, account_id, execution_id):
    """正确的结果编号也不能在其他账户或执行上下文解析。"""
    from axile.server.business_facts.artifacts import resolve_artifact_content

    path = _database(tmp_path)
    _, artifact, _ = _store_summary(path)
    assert artifact.content[REFERENCE_KEY]["view"] == "result"
    with pytest.raises(ValueError, match="账户"):
        asyncio.run(
            resolve_artifact_content(
                {"execution_id": execution_id, "artifact_type": artifact.artifact_type, "content": artifact.content},
                account_id=account_id,
            )
        )


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_summary_reference_freezes_child_until_journal_is_materialized(tmp_path, foreign_keys):
    """摘要引用期间禁止改成交；物化后业务可改，原审计保留原值。"""
    path = _database(tmp_path)
    record, artifact, original = _store_summary(path)
    with _connection(path, foreign_keys) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE business_trade SET trade_price=101 WHERE record_id=?", (record.id,))
        connection.rollback()
    materialized = _resolved(path, record.execution_id)
    materialized.pop(REFERENCE_KEY)
    with _connection(path, foreign_keys) as connection:
        # 测试直接物化已由原生读取校验的正文，验证数据库冻结边界。
        connection.execute(
            "UPDATE execution_artifact SET content=? WHERE id=?",
            (json.dumps(materialized, ensure_ascii=False, allow_nan=False), artifact.id),
        )
        connection.execute("UPDATE business_trade SET trade_price=101 WHERE record_id=?", (record.id,))
    journal = _artifact(path, record.execution_id)
    assert REFERENCE_KEY not in journal.content
    assert _wire_json(journal.content) == _wire_json(original)
    assert _result_view(record)["symbol_results"]["A"]["trades"][0]["trade_price"] == 101.0


@pytest.mark.parametrize("second_account", [1, 2], ids=["same-account", "other-account"])
def test_composite_order_paths_cannot_invalidate_another_record(tmp_path, second_account):
    """相同订单路径的不同执行只影响真正拥有子行的执行。"""
    path = _database(tmp_path)
    first = _store(path)
    projection, version = _projection_rows(path, first.id), _version(path)
    second = _store(path, execution_id="same-order-paths", account_id=second_account)
    assert _projection_rows(path, first.id) == projection
    if second_account != 1:
        assert _version(path) == version
    with _connection(path) as connection:
        connection.execute("UPDATE execution_order SET price=101 WHERE record_id=?", (second.id,))
    assert _projection_rows(path, first.id) == projection
    if second_account != 1:
        assert _version(path) == version


@pytest.mark.parametrize("new_snapshot", ["manual", "execution-index"])
@pytest.mark.parametrize("mutation", ["asset", "binding"])
def test_new_account_asset_observation_preserves_unrelated_record_projection(
    tmp_path, monkeypatch, new_snapshot, mutation
):
    """新观测只推动账户版本，事实变化只影响其拥有者的投影。"""
    path = _database(tmp_path)
    first = _store(path)
    alternate = _result()
    alternate["account_assets"]["total_asset"] = 1500.0
    after = native_store_call(
        path,
        "freeze_execution_after_assets",
        {
            "account_id": 1,
            "execution_id": first.execution_id,
            "payload": alternate,
        },
    )
    assert after is not None
    projection, version = _projection_rows(path, first.id), _version(path)
    second = None
    if new_snapshot == "manual":
        observation = native_store_call(
            path,
            "asset_snapshot_write",
            {
                "account_id": 1,
                "created_at": _CREATED,
                "account_assets": {"total_asset": 2000.0, "positions": []},
            },
        )
        assert observation is not None
    else:
        second = _store(path, execution_id="second-execution-index")
        assert (
            _scalar(path, "SELECT COUNT(*) FROM account_asset_snapshot WHERE execution_id=?", (second.execution_id,))
            == 1
        )
    assert _version(path) > version
    assert _projection_rows(path, first.id) == projection
    second_projection = _projection_rows(path, second.id) if second is not None else None
    with _connection(path) as connection:
        if mutation == "asset":
            connection.execute(
                "UPDATE business_asset_snapshot SET total_asset=1001 WHERE asset_id IN "
                "(SELECT asset_id FROM business_asset_binding WHERE record_id=? AND path='/account_assets')",
                (first.id,),
            )
        else:
            connection.execute(
                "UPDATE business_asset_binding SET asset_id=? WHERE record_id=? AND path='/account_assets'",
                (after["asset_id"], first.id),
            )
    if second is not None:
        assert _projection_rows(path, second.id) == second_projection


def test_atomic_new_graph_does_not_version_account_for_every_fill(tmp_path):
    """一次新执行事务的版本增量不随订单成交行数增长。"""
    path = _database(tmp_path)
    start = _version(path)
    _store(path, execution_id="small")
    small = _version(path)
    large_result = _result()
    large_result["symbol_results"]["A"]["orders"] *= 40
    large_result["symbol_results"]["A"]["trades"] *= 40
    _store(path, execution_id="large", result=large_result)
    large = _version(path)
    assert large - small == small - start
    assert 0 < large - small <= 4


def test_late_insert_into_complete_graph_still_invalidates_projection(tmp_path):
    """完成后补入事实仍推动版本，不能绕过冻结后的失效边界。"""
    path = _database(tmp_path)
    record = _store(path)
    previous = _version(path)
    assert record.facts_complete
    with _connection(path) as connection:
        connection.execute(
            "INSERT INTO business_trade_collection(record_id,path,parent_path,ordinal,state,member_count,audit_evidence) "
            "VALUES(?,'/trades','',0,'array',1,'{}')",
            (record.id,),
        )
        connection.execute(
            "INSERT INTO business_trade(record_id,path,collection_path,ordinal,state,trade_id,trade_volume,"
            "trade_id_state,order_id_state,conversion_recorded,fee_state,audit_evidence) "
            "VALUES(?,'/trades/0','/trades',0,'object','\"late\"',0,'value','absent',0,'absent','{}')",
            (record.id,),
        )
    assert _version(path) > previous
