"""同进程重复原生调用保持快照、数据库身份和错误恢复边界。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from shutil import copyfile
from threading import Barrier

import pytest

from scripts.benchmark_structured_publication import record
from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.unit.server._execution_test_support import build_account

_NOW = "2026-10-23T09:30:00+08:00"
_SQL = """
import json, sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1], timeout=2)) as connection, connection:
    rows = connection.execute(sys.argv[2], json.loads(sys.argv[3])).fetchall()
print(json.dumps(rows))
"""


def _sql(database: Path, statement: str, parameters: tuple = ()) -> list:
    """独立进程查询，避免两个 SQLite 内核共用本进程的文件锁。"""
    result = subprocess.run(
        [sys.executable, "-c", _SQL, str(database), statement, json.dumps(parameters)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture
def database(native_database: Path) -> Path:
    account = build_account(name="initial", remark="initial", portfolio_id=None)
    saved = native_store_call(
        native_database,
        "account_create",
        {"account": account.model_dump(mode="json"), "now": _NOW},
    )
    assert saved["account"]["id"] == 1
    return native_database


def _persist(database: Path, execution_id: str) -> None:
    native_store_call(
        database,
        "persist_execute_record",
        {
            "account_id": 1,
            "execution_id": execution_id,
            "created_at": _NOW,
            "is_success": 1,
            "input_id": f"input-{execution_id}",
            "result_input_id": f"result-{execution_id}",
            "raw_input": {},
            "raw_result": {},
        },
    )


def test_repeated_reads_see_external_commits_and_release_snapshots(database: Path) -> None:
    core = require_native_store("Store", "execution_records", "account_get")
    store = core.Store(str(database))
    assert json.loads(core.execution_records(store, 1, 0, 10))["count"] == 0

    for index in range(3):
        name = f"external-{index}"
        _sql(database, "UPDATE account SET name=? WHERE id=1", (name,))
        _persist(database, f"fresh-{index}")
        assert native_store_call(database, "account_get", {"account_id": 1})["name"] == name
        page = json.loads(core.execution_records(store, 1, 0, 10))
        assert page["count"] == index + 1
        assert page["data"][0]["execution_id"] == f"fresh-{index}"
        assert _sql(database, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0


def test_analysis_store_tracks_configured_path_and_retains_explicit_handles(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axile.server.core import db

    core = require_native_store("Store", "asset_snapshots")
    other = tmp_path / "other.sqlite"
    initialize_native_database(other)
    configured = [database]
    monkeypatch.setattr(db, "database_path", lambda: str(configured[0]))
    first = db.analysis_store()
    assert json.loads(core.asset_snapshots(first, 1, 0, 10)) == {"data": [], "count": 0}

    configured[0] = other
    with pytest.raises(core.NativeError) as caught:
        core.asset_snapshots(db.analysis_store(), 1, 0, 10)
    assert caught.value.args[0] == "ACCOUNT_NOT_FOUND"
    assert json.loads(core.asset_snapshots(first, 1, 0, 10))["count"] == 0

    configured[0] = database
    assert json.loads(core.asset_snapshots(db.analysis_store(), 1, 0, 10))["count"] == 0


def test_parallel_native_readers_and_writer_keep_commits_and_finish_snapshots(database: Path) -> None:
    core = require_native_store("Store", "asset_snapshots", "account_update")
    barrier = Barrier(3, timeout=5)

    def read() -> None:
        store = core.Store(str(database))
        barrier.wait()
        for _ in range(12):
            account = native_store_call(database, "account_get", {"account_id": 1})
            assert account["name"] == account["remark"]
            assert json.loads(core.asset_snapshots(store, 1, 0, 10))["count"] == 0

    def write() -> None:
        barrier.wait()
        for index in range(8):
            value = f"revision-{index}"
            receipt = native_store_call(
                database,
                "account_update",
                {"account_id": 1, "patch": {"name": value, "remark": value}, "now": _NOW},
            )
            assert receipt["account"]["name"] == value

    with ThreadPoolExecutor(max_workers=3) as workers:
        futures = [workers.submit(read), workers.submit(read), workers.submit(write)]
        for future in futures:
            future.result(timeout=10)
    assert native_store_call(database, "account_get", {"account_id": 1})["name"] == "revision-7"
    assert _sql(database, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0


@pytest.mark.parametrize("path_kind", ["missing", "empty", "memory"])
def test_business_reads_and_writes_reject_bad_paths_without_creating_files(tmp_path: Path, path_kind: str) -> None:
    core = require_native_store("Store", "account_get", "queue_enqueue")
    path = {"missing": str(tmp_path / "missing.sqlite"), "empty": "", "memory": ":memory:"}[path_kind]
    operations = (
        lambda: native_store_call(path, "account_get", {"account_id": 1}),
        lambda: core.asset_snapshots(core.Store(path), 1, 0, 10),
        lambda: core.queue_enqueue(path, 1, False),
    )
    for operation in operations:
        with pytest.raises(core.NativeError) as caught:
            operation()
        assert caught.value.args[0] == "DATABASE_OPEN"
    assert list(tmp_path.iterdir()) == []


def test_missing_warmed_file_is_not_recreated(database: Path) -> None:
    """Unix 直接删除热缓存文件；Windows 先经迁移生命周期释放文件句柄。"""
    core = require_native_store("Store", "asset_snapshots", "migration_current", "migration_check")
    store = core.Store(str(database))
    assert json.loads(core.asset_snapshots(store, 1, 0, 10))["count"] == 0
    assert _sql(database, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0
    if sys.platform == "win32":
        native_store_call(database, "migration_upgrade", {})
    database.unlink()

    for operation in (
        lambda: core.asset_snapshots(store, 1, 0, 10),
        lambda: core.queue_enqueue(str(database), 1, False),
    ):
        with pytest.raises(core.NativeError) as caught:
            operation()
        assert caught.value.args[0] == "DATABASE_OPEN"
    for operation in ("migration_current", "migration_check"):
        assert native_store_call(database, operation, {})["public"] is None
    assert not database.exists()


def test_replaced_file_is_used_by_warmed_store_and_business_writer(database: Path) -> None:
    """Unix 直接替换热缓存文件；Windows 经迁移释放池后复用 Store 路径句柄。"""
    core = require_native_store("Store", "execution_records", "account_update")
    _persist(database, "old-file")
    store = core.Store(str(database))
    assert json.loads(core.execution_records(store, 1, 0, 10))["count"] == 1
    assert _sql(database, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0

    replacement = database.with_name("replacement.sqlite")
    copyfile(database, replacement)
    _sql(replacement, "DELETE FROM executerecord")
    _sql(replacement, "UPDATE account SET name='replacement' WHERE id=1")
    if sys.platform == "win32":
        native_store_call(database, "migration_upgrade", {})
    os.replace(replacement, database)

    assert native_store_call(database, "account_get", {"account_id": 1})["name"] == "replacement"
    assert json.loads(core.execution_records(store, 1, 0, 10))["count"] == 0
    receipt = native_store_call(
        database, "account_update", {"account_id": 1, "patch": {"name": "new-commit"}, "now": _NOW}
    )
    assert receipt["account"]["name"] == "new-commit"
    assert _sql(database, "SELECT name FROM account WHERE id=1") == [["new-commit"]]
    assert _sql(database, "PRAGMA integrity_check") == [["ok"]]


def test_migration_roundtrip_reopens_warmed_readers_and_writers(database: Path) -> None:
    core = require_native_store("Store", "asset_snapshots", "migration_downgrade")
    store = core.Store(str(database))
    assert json.loads(core.asset_snapshots(store, 1, 0, 10))["count"] == 0
    for operation in ("migration_current", "migration_check"):
        assert native_store_call(database, operation, {})["public"] == "0040"

    assert native_store_call(database, "migration_downgrade", {"public": "0038"})["public"] == "0038"
    assert native_store_call(database, "migration_current", {})["public"] == "0038"
    assert native_store_call(database, "migration_upgrade", {})["public"] == "0040"
    assert native_store_call(database, "migration_check", {})["public"] == "0040"
    assert json.loads(core.asset_snapshots(store, 1, 0, 10))["count"] == 0
    receipt = native_store_call(
        database, "account_update", {"account_id": 1, "patch": {"name": "after-migration"}, "now": _NOW}
    )
    assert receipt["account"]["name"] == "after-migration"
    assert _sql(database, "PRAGMA foreign_key_check") == []


def test_publication_can_repeat_after_stale_source_and_failed_commit(database: Path) -> None:
    core = require_native_store("publish_store", "queue_claim", "queue_enqueue")
    item = record(0, 1)
    native_store_call(
        database,
        "persist_execute_record",
        {**vars(item), "input_id": "publication-input", "result_input_id": "publication-result"},
    )

    def request(token: str) -> dict:
        core.queue_enqueue(str(database), 1, True)
        claim = core.queue_claim(str(database), 1_800_000_000.0, token)
        assert claim is not None
        return {
            "command": "publish_store",
            "protocol": 1,
            "account_id": 1,
            "expected_source_version": claim["source_version"],
            "snapshot_id": token,
            "claim_token": token,
            "logic_version": "reuse-test",
            "engine_version": "reuse-test",
            "computed_at": _NOW,
            "settings": {"backtest_weight_type": "ts", "backtest_fee_rate": 0.0},
            "trade_channel": "ctp",
        }

    first = native_store_call(database, "publish_store", request("first"))
    assert first["published"]
    assert first["row_counts"]["performance_cost_execution"] == 1

    stale = request("stale")
    _sql(database, "UPDATE account_analysis SET source_version=source_version+1 WHERE account_id=1")
    skipped = native_store_call(database, "publish_store", stale)
    assert not skipped["published"] and skipped["reason"] == "source_changed"

    retry = request("retry")
    _sql(
        database,
        "CREATE TRIGGER reject_publication BEFORE INSERT ON performance_snapshot "
        "BEGIN SELECT RAISE(ABORT,'reuse-publication'); END",
    )
    with pytest.raises(core.NativeError) as caught:
        native_store_call(database, "publish_store", retry)
    assert caught.value.args[0] == "PUBLICATION_WRITE"
    assert "reuse-publication" in caught.value.args[1]
    _sql(database, "DROP TRIGGER reject_publication")
    published = native_store_call(database, "publish_store", retry)
    assert published["published"]
    assert published["row_counts"] == first["row_counts"]
    assert _sql(database, "SELECT id FROM performance_snapshot ORDER BY id") == [["first"], ["retry"]]
    assert _sql(database, "SELECT current_snapshot FROM account_analysis WHERE account_id=1") == [["retry"]]
    assert _sql(database, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0
