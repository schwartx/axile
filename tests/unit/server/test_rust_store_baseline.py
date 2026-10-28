"""真实历史迁移与当前原生迁移的结构契约；仅连接临时 SQLite 文件。"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.fixtures.rust_store.data_contract import database_rows, seed_database
from tests.fixtures.rust_store.schema_contract import database_schema, load_schema, schema_differences
from tests.sqlite_oracle import connect as oracle_connect

_FIXTURES = Path(__file__).parents[2] / "fixtures/rust_store"
_TAG_COMMIT = "bc70e4a3785eacedeaf4eadcda5a3109d3a3ffa6"
_SOURCE_COMMIT = "509b3627ca5fc6ee5f5a72e91b5ebebed3cf9879"


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema():
    """本模块显式控制每个迁移阶段，不创建共享当前版本库。"""
    yield


@pytest.fixture(autouse=True)
def _isolate_connection_runtime():
    """本模块不使用服务端连接运行时。"""
    yield


def _manifest(name: str) -> dict:
    path = _FIXTURES / name
    manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sql_sha256"]
    return manifest


def test_v0220_baseline_preserves_the_tag_and_0033_head() -> None:
    """标签的提交、连续迁移链和 DDL 哈希由静态 provenance 固定。"""
    manifest = _manifest("v0.22.0.sql")
    assert manifest["tag"] == "v0.22.0"
    assert manifest["source_commit"] == _TAG_COMMIT
    assert manifest["public_head"] == "0033"
    assert manifest["revisions"] == [f"{index:04}" for index in range(1, 34)]


def test_missing_capability_fails_instead_of_skipping() -> None:
    with pytest.raises(pytest.fail.Exception, match="缺少能力.*__missing_storage_operation_for_test__"):
        require_native_store("__missing_storage_operation_for_test__")


def test_native_helper_gates_nonfinite_evidence(monkeypatch) -> None:
    import math
    from types import SimpleNamespace

    from tests import conftest as support

    calls = []

    def capture(path, wire):
        assert path == "synthetic.db"
        calls.append(wire)
        return wire

    core = SimpleNamespace(
        EVIDENCE_INPUT_STAGES=("persist_execution_state",),
        persist_execution_state=capture,
        migration_check=capture,
    )
    monkeypatch.setattr(support, "require_native_store", lambda *_operations: core)
    request = {
        "nan": float("nan"),
        "positive": float("inf"),
        "negative": float("-inf"),
        "literal": {"invalid_number": "nan"},
    }
    decoded = json.loads(support.native_store_call("synthetic.db", "persist_execution_state", request))
    assert math.isnan(decoded["nan"])
    assert decoded["positive"] == float("inf")
    assert decoded["negative"] == float("-inf")
    assert decoded["literal"] == {"invalid_number": "nan"}
    with pytest.raises(ValueError, match="Out of range float values"):
        support.native_store_call("synthetic.db", "migration_check", request)
    del core.EVIDENCE_INPUT_STAGES
    with pytest.raises(ValueError, match="Out of range float values"):
        support.native_store_call("synthetic.db", "persist_execution_state", request)
    literal = {"invalid_number": "nan"}
    assert json.loads(support.native_store_call("synthetic.db", "migration_check", literal)) == literal
    assert len(calls) == 2


@pytest.mark.parametrize("name", ["v0.22.0.sql"])
def test_frozen_oracle_ddl_loads_with_all_objects(tmp_path: Path, name: str) -> None:
    with closing(oracle_connect(tmp_path / "oracle.db")) as connection, connection:
        load_schema(connection, (_FIXTURES / name).read_text(encoding="utf-8"))
        schema = database_schema(connection)
        assert "executerecord" in schema["tables"]
        assert "axile_0013_error_backup" in schema["tables"]
        assert schema["triggers"]


def test_schema_contract_detects_integrity_changes(tmp_path: Path) -> None:
    with closing(oracle_connect(tmp_path / "contract.db")) as connection, connection:
        connection.executescript(
            "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
            "CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id),"
            "value INTEGER NOT NULL DEFAULT 7 CHECK(value>0));"
            "CREATE UNIQUE INDEX child_value ON child(value);"
            "CREATE TRIGGER child_insert AFTER INSERT ON child BEGIN SELECT RAISE(ABORT,'blocked'); END;"
        )
        before = database_schema(connection)
        connection.executescript("DROP INDEX child_value; DROP TRIGGER child_insert;")
        after = database_schema(connection)
        assert before["tables"]["child"]["columns"] == after["tables"]["child"]["columns"]
        assert before["tables"]["child"]["foreign_keys"] == after["tables"]["child"]["foreign_keys"]
        assert before["tables"]["child"]["checks"] == (("(", "value", ">", "0", ")"),)
        assert before["tables"]["child"]["indexes"] != after["tables"]["child"]["indexes"]
        assert before["triggers"] != after["triggers"]
        differences = schema_differences(after, before)
        assert len(differences) == 2
        assert any("tables.child.indexes" in difference for difference in differences)
        assert "triggers.child_insert: missing" in differences


def test_fixture_id_move_keeps_parent_facts_and_foreign_keys(tmp_path: Path) -> None:
    from tests.unit.server._business_analysis_support import _move_record_id, _record_tables, _reset_records
    from tests.unit.server._execution_test_support import build_account, insert_account

    path = tmp_path / "fixture.db"
    initialize_native_database(path)
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        insert_account(connection, build_account(id=2, portfolio_id=None))
        connection.execute(
            "INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at) VALUES (1,2,'move',0,'2026-10-01')"
        )
        connection.execute(
            "INSERT INTO execution_order_collection(record_id,path,ordinal,kind,item_count,audit_evidence) "
            "VALUES (1,'/orders',0,'array',1,'{}')"
        )
        connection.execute(
            "INSERT INTO execution_order(record_id,path,ordinal,kind,order_id_state,audit_evidence) "
            "VALUES (1,'/orders',0,'object','absent','{}')"
        )
        _move_record_id(connection, 1, 9, "move", _record_tables(connection))
        assert connection.execute("SELECT id,execution_id FROM executerecord").fetchall() == [(9, "move")]
        assert connection.execute("SELECT record_id,path FROM execution_order").fetchall() == [(9, "/orders")]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        _reset_records(connection, [9])
        assert connection.execute("SELECT COUNT(*) FROM executerecord").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM execution_order").fetchone() == (0,)


@pytest.mark.parametrize("revision,name", [("0033", "v0.22.0.sql")])
def test_native_migration_matches_real_schema(tmp_path: Path, revision: str, name: str) -> None:
    """列、默认值、索引、外键、唯一性和完整触发器与历史实现一致。"""
    path = tmp_path / "native.db"
    result = initialize_native_database(path, revision=revision)
    assert result["public"] == revision
    with closing(oracle_connect(tmp_path / "oracle.db")) as oracle, closing(oracle_connect(path)) as native:
        load_schema(oracle, (_FIXTURES / name).read_text(encoding="utf-8"))
        differences = schema_differences(database_schema(native), database_schema(oracle))
        if differences:
            pytest.fail("\n".join(differences), pytrace=False)
        assert native.execute("SELECT version_num FROM alembic_version").fetchall() == [(revision,)]
        assert native.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert native.execute("PRAGMA foreign_key_check").fetchall() == []
        assert native.execute("SELECT COUNT(*) FROM axile_0013_error_backup").fetchone() == (0,)


def test_current_and_check_do_not_create_a_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    for operation in ("migration_current", "migration_check"):
        result = native_store_call(path, operation, {})
        assert result["public"] is None
        assert result["public_target"] == "0038"
        assert not path.exists()
        assert list(tmp_path.iterdir()) == []


def test_0033_upgrade_keeps_backup_rows_and_reaches_real_head(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    initialize_native_database(path, revision="0033")
    backup = ("executerecord", "701", "raw_result", '"error"', 1, '"before"', 1, '"after"', "fixture", "{}")
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO axile_0013_error_backup "
            "(table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,identity) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            backup,
        )
    result = initialize_native_database(path)
    assert result["public"] == "0038"
    assert native_store_call(path, "migration_current", {})["public"] == "0038"
    with closing(oracle_connect(path)) as connection, connection:
        assert connection.execute(
            "SELECT table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,identity "
            "FROM axile_0013_error_backup"
        ).fetchall() == [backup]
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("0038",)]


def test_nonempty_0033_fixture_loads_without_current_models(tmp_path: Path) -> None:
    data = json.loads((_FIXTURES / "public-0033-data.json").read_text(encoding="utf-8"))
    assert data["source_commit"] == _SOURCE_COMMIT
    assert len(data["seed"]["executerecord"]) == 22
    assert len(data["seed"]["notification_event"]) == 8
    assert len(data["expected"]["execution_input"]) > 22
    with closing(oracle_connect(tmp_path / "historical.db")) as connection, connection:
        load_schema(connection, (_FIXTURES / "v0.22.0.sql").read_text(encoding="utf-8"))
        seed_database(connection, data["seed"])
        assert database_rows(connection, canonical=False) == data["seed"]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("table", "fields", "count"),
    [
        ("executerecord", ("status", "error", "execution_time", "success"), 12),
        ("execution_input", ("execution_timeout",), 1),
        ("execution_input_symbol", ("value",), 1),
        ("execution_order", ("avg_price",), 46),
        ("business_trade", ("trade_value",), 11),
        ("business_asset_snapshot", ("total_asset", "market_value"), 12),
        ("business_asset_position", ("available_volume",), 11),
    ],
)
def test_frozen_null_evidence_is_derived_from_original_json(table, fields, count) -> None:
    """399389c9 的侧录仅补原始字段的显式 null，不将缺失或非法值当成 null。"""
    source = (_FIXTURES / "public-0033-data.json").read_bytes()
    data = json.loads(source)
    corrections = json.loads((_FIXTURES / "public-0034-explicit-nulls.json").read_text(encoding="utf-8"))
    assert corrections["source_sha256"] == hashlib.sha256(source).hexdigest()
    entries = corrections["tables"][table]
    assert len(entries) == count
    assert len({json.dumps(item["identity"], sort_keys=True) for item in entries}) == count
    for item in entries:
        source_table, source_id, column, pointer = item["source"]
        original = next(row for row in data["seed"][source_table] if row["id"] == source_id)
        payload = json.loads(original[column])
        for key in pointer.split("/")[1:]:
            key = key.replace("~1", "/").replace("~0", "~")
            payload = payload[int(key)] if isinstance(payload, list) else payload[key]
        if table == "execution_input_symbol":
            payload = {"value": payload}
        assert item["fields"] == [name for name in fields if name in payload and payload[name] is None]
        matches = [
            row for row in data["expected"][table] if all(row[key] == value for key, value in item["identity"].items())
        ]
        assert len(matches) == 1
        assert all(matches[0][name] is None for name in item["fields"])


def test_nonempty_native_upgrade_matches_frozen_0034_rows(tmp_path: Path) -> None:
    """真实输入、成交、资产、目标、摘要、通知 outbox 和分析结果同时迁移。"""
    data = json.loads((_FIXTURES / "public-0033-data.json").read_text(encoding="utf-8"))
    path = tmp_path / "historical.db"
    initialize_native_database(path, revision=data["from_revision"])
    with closing(oracle_connect(path)) as connection, connection:
        seed_database(connection, data["seed"])
    assert initialize_native_database(path, revision="0034")["public"] == "0034"
    expected = data["expected"]
    # 只补 399389c9 保留显式 null 的静态侧录；原始 seed 和冻结行均保留。
    corrections = json.loads((_FIXTURES / "public-0034-explicit-nulls.json").read_text(encoding="utf-8"))
    for table, entries in corrections["tables"].items():
        for item in entries:
            (row,) = [
                row for row in expected[table] if all(row[key] == value for key, value in item["identity"].items())
            ]
            evidence = json.loads(row["audit_evidence"][1])
            assert "explicit_null_fields" not in evidence
            evidence["explicit_null_fields"] = item["fields"]
            row["audit_evidence"] = [
                "json",
                json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            ]
        expected[table].sort(key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    raw_results = {row["id"]: json.loads(row["raw_result"]) for row in data["seed"]["executerecord"]}
    for row in expected["execution_extension"]:
        extra = json.loads(row["extra"][1])
        captured = extra.get("server_notification_context") if isinstance(extra, dict) else None
        if isinstance(captured, dict) and "$axile_notification_context" in captured:
            extra["server_notification_context"] = raw_results[row["record_id"]]["extra"]["server_notification_context"]
            row["extra"] = ["json", json.dumps(extra, sort_keys=True, ensure_ascii=False, separators=(",", ":"))]
    originals = {row["id"]: row for row in data["seed"]["execution_artifact"]}
    for row in expected["execution_artifact"]:
        if row["artifact_type"].lower() == "execution_summary":
            original = originals[row["id"]]
            row["content"] = [
                "json",
                json.dumps(json.loads(original["content"]), sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            ]
            row["schema_version"] = original["schema_version"]
    for name in ("execution_extension", "execution_artifact"):
        expected[name].sort(key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    with closing(oracle_connect(path)) as connection, connection:
        actual = database_rows(connection)

        def business(name):
            return name not in {"alembic_version", "account_analysis", "analysis_calendar_day"} and not name.startswith(
                "performance_"
            )

        assert {k: v for k, v in actual.items() if business(k)} == {k: v for k, v in expected.items() if business(k)}
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_native_record_fixture_preserves_explicit_ids_and_rewrites(tmp_path: Path) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.unit.server._business_analysis_support import load_record, persist_records, record
    from tests.unit.server._execution_test_support import build_account, insert_account

    path = tmp_path / "records.db"
    initialize_native_database(path)
    with closing(oracle_connect(path)) as connection, connection:
        insert_account(connection, build_account(id=2, portfolio_id=None))

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                original = record(9)
                await persist_records(session, [original])
                assert (await load_record(session, 9)).input == original.input
                original.input = {"curr_target": {"A": 0.25}}
                original.result["account_assets"]["total_asset"] = 123.0
                await persist_records(session, [original])
                updated = await load_record(session, 9)
                assert updated.input == original.input
                assert updated.result["account_assets"]["total_asset"] == 123.0
        finally:
            await engine.dispose()

    asyncio.run(check())
    with closing(oracle_connect(path)) as connection, connection:
        assert connection.execute("SELECT id FROM executerecord").fetchall() == [(9,)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_native_target_fixture_preserves_explicit_id_and_empty_weights(tmp_path: Path) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.unit.server._business_analysis_support import persist_target
    from tests.unit.server._execution_test_support import build_account, insert_account

    path = tmp_path / "targets.db"
    initialize_native_database(path)
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at,updated_at) "
            "VALUES (11,'fixture','FUTURES','','now','now')"
        )
        insert_account(connection, build_account(id=2, portfolio_id=11))

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                first = await persist_target(
                    session,
                    id=9,
                    portfolio_id=11,
                    account_id=2,
                    source="manual",
                    execution_id="target-9",
                    normalized_weights={"A": 1},
                    raw_weights={},
                )
                updated = await persist_target(session, snapshot=first, normalized_weights={}, raw_weights=None)
                assert updated.id == 9
                assert updated.normalized_weights == {}
                assert updated.raw_weights is None
        finally:
            await engine.dispose()

    asyncio.run(check())
    with closing(oracle_connect(path)) as connection, connection:
        assert connection.execute("SELECT id FROM target_weight_snapshot").fetchall() == [(9,)]
        assert connection.execute("SELECT COUNT(*) FROM target_weight_snapshot_weight").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
