"""Exercise real native publication and SQLite failure boundaries in isolated files.

These tests require a prebuilt worker. Database triggers inject SQLite failures
only in disposable test databases; no production fault hook or fake publisher is
used. A committed database is authoritative even when receipt delivery fails.
"""

import asyncio
import json
import sqlite3
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.db.models import ExecuteRecord
from axile.server.performance_analysis import LOGIC_VERSION
from axile.server.performance_replay import ENGINE_VERSION
from tests.unit.server._business_analysis_support import persist_records
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration
from tests.unit.server.test_native_analysis_contract import _cost_records
from tests.unit.server.test_native_analysis_contract import native_binary as native_binary
from tests.unit.server.test_performance_analysis import _create_schema

pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class Store:
    binary: Path
    database: Path
    journal: str

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=1)
        connection.row_factory = sqlite3.Row
        return connection

    def state(self):
        with closing(self.connect()) as connection:
            return dict(connection.execute("SELECT * FROM account_analysis WHERE account_id=7").fetchone())

    def results(self):
        """Capture actual durable rows, including tables not known to the test."""
        with closing(self.connect()) as connection:
            names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'performance_%' ORDER BY name"
            ).fetchall()
            return {
                name[0]: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name[0]}" ORDER BY rowid')]
                for name in names
            }

    def claim(self, token):
        with closing(self.connect()) as connection, connection:
            version = connection.execute("SELECT source_version FROM account_analysis WHERE account_id=7").fetchone()[0]
            connection.execute(
                "UPDATE account_analysis SET requested=1,running_version=?,running_token=? WHERE account_id=7",
                (version, token),
            )
        return {
            "command": "publish_store",
            "protocol": 1,
            "account_id": 7,
            "expected_source_version": version,
            "snapshot_id": token,
            "claim_token": token,
            "logic_version": LOGIC_VERSION,
            "engine_version": ENGINE_VERSION,
            "computed_at": "2026-10-20T10:00:00+08:00",
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.001},
            "trade_channel": "ctp",
        }

    @contextmanager
    def start(self):
        with subprocess.Popen(
            [str(self.binary), "query", "--database", str(self.database)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)

    def run(self, request, *, success=True):
        with self.start() as process:
            stdout, stderr = process.communicate(json.dumps(request).encode(), timeout=5)
        if success:
            assert process.returncode == 0, stderr.decode()
            assert not stderr, stderr.decode()
            assert len(stdout) < 16_384, "Native IPC must contain only a bounded receipt"
            return json.loads(stdout)
        assert process.returncode != 0
        assert not stdout
        return json.loads(stderr)


def _write_database(path):
    async def write():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(_create_schema)
                migration = _load_migration(_MIGRATIONS_DIR / "0036_analysis_read_views.py")
                await connection.run_sync(migration.install_triggers)
            async with sessions() as session:
                session.add(
                    build_account(
                        id=7,
                        portfolio_id=None,
                        is_started=False,
                        backtest_weight_type="cs",
                        backtest_fee_rate=0.001,
                    )
                )
                await session.commit()
                records = _cost_records()
                records[0].result["account_assets"]["positions"] = [{"symbol": "A", "volume": 2, "direction": "LONG"}]
                await persist_records(session, records, project=True)
                await session.commit()
                heads = (await session.scalars(sa.select(ExecuteRecord))).all()
                assert len(heads) == len(records)
                assert all(head.input_id is not None for head in heads)
                assert all(not hasattr(head, "raw_input") and not hasattr(head, "raw_result") for head in heads)
        finally:
            await engine.dispose()

    asyncio.run(write())


@pytest.fixture(params=["wal", "delete"])
def store(native_binary, tmp_path, request):
    database = tmp_path / "publish.db"
    _write_database(database)
    value = Store(native_binary, database, request.param)
    with closing(value.connect()) as connection:
        actual = connection.execute(f"PRAGMA journal_mode={request.param}").fetchone()[0]
        assert actual == request.param
    receipt = value.run(value.claim("baseline"))
    assert receipt["published"] is True
    assert value.state()["current_snapshot"] == "baseline"
    with closing(value.connect()) as connection:
        schema = connection.execute(
            "SELECT cost_schema_version,result_schema_version,read_view_schema_version "
            "FROM performance_snapshot WHERE id='baseline'"
        ).fetchone()
        assert tuple(schema) == (1, 2, 1)
        assert connection.execute("SELECT COUNT(*) FROM performance_cost_position").fetchone()[0] > 0
    yield value


def test_same_version_foreign_token_cannot_publish_or_release_new_owner(store):
    request = store.claim("obsolete-owner")
    newer_request = store.claim("current-owner")
    before, rows = store.state(), store.results()

    receipt = store.run(request)

    assert receipt["published"] is False and receipt["reason"] == "claim_lost"
    assert store.state() == before
    assert store.results() == rows
    assert store.run(newer_request)["published"] is True


def test_stale_source_requeues_only_matching_claim_without_touching_published_rows(store):
    request = store.claim("stale-source")
    before, rows = store.state(), store.results()
    with closing(store.connect()) as connection, connection:
        connection.execute("UPDATE account_analysis SET source_version=source_version+1 WHERE account_id=7")

    receipt = store.run(request)

    assert receipt["published"] is False and receipt["reason"] == "source_changed"
    state = store.state()
    assert state["current_snapshot"] == before["current_snapshot"]
    assert state["published_version"] == before["published_version"]
    assert state["running_version"] is None and state["running_token"] is None
    assert state["source_version"] == before["source_version"] + 1 and state["requested"]
    assert store.results() == rows


@pytest.mark.parametrize("fault", ["sqlite_abort", "claim_cas"])
def test_failure_after_result_inserts_rolls_back_every_table_and_pointer(store, fault):
    request = store.claim("writer-failure")
    before, rows = store.state(), store.results()
    with closing(store.connect()) as connection, connection:
        if fault == "sqlite_abort":
            # The pointer is written after the result, range and source facts.
            # Fail this real SQL statement after all those inserts have run.
            connection.executescript("""
                CREATE TRIGGER test_reject_publication BEFORE UPDATE OF current_snapshot ON account_analysis
                WHEN NEW.current_snapshot='writer-failure'
                BEGIN SELECT RAISE(ABORT, 'test publication write failure'); END;
            """)
        else:
            # Deliberately perturb the claim inside the writer transaction;
            # the final token CAS must fail and roll back this change too.
            connection.executescript("""
                CREATE TRIGGER test_lose_claim AFTER INSERT ON performance_snapshot
                WHEN NEW.id='writer-failure'
                BEGIN UPDATE account_analysis SET running_token='replacement' WHERE account_id=7; END;
            """)

    error = store.run(request, success=False)

    assert error["code"] == ("PUBLICATION_WRITE" if fault == "sqlite_abort" else "CLAIM_LOST")
    assert store.state() == before
    assert store.results() == rows


def test_cleanup_failure_restores_updated_pointer_and_deleted_previous_rows(store):
    assert store.run(store.claim("second"))["published"] is True
    request = store.claim("third")
    before, rows = store.state(), store.results()
    with closing(store.connect()) as connection, connection:
        # Cleanup follows the successful pointer UPDATE. At this point its
        # child-table deletes have also run; failing the parent delete must
        # undo the pointer, the new version and those previous-version deletes.
        connection.executescript("""
            CREATE TRIGGER test_reject_cleanup BEFORE DELETE ON performance_snapshot
            WHEN OLD.id='baseline'
            BEGIN SELECT RAISE(ABORT, 'test cleanup write failure'); END;
        """)

    error = store.run(request, success=False)

    assert error["code"] == "PUBLICATION_WRITE"
    assert store.state() == before
    assert store.results() == rows


def test_lost_stdout_after_commit_keeps_success_and_retry_preserves_new_claim(store):
    request = store.claim("receipt-lost")
    with store.start() as process:
        # Closing before sending stdin ensures no reader exists when the worker
        # delivers its receipt. The real writer commits before stdout is used.
        process.stdout.close()
        process.stdout = None
        _, stderr = process.communicate(json.dumps(request).encode(), timeout=5)
    assert process.returncode != 0
    assert json.loads(stderr)["code"] == "RESULT_WRITE"
    assert store.state()["current_snapshot"] == "receipt-lost"
    committed_rows = store.results()

    store.claim("new-owner")
    before = store.state()
    receipt = store.run(request)

    assert receipt["published"] is True and receipt["snapshot_id"] == "receipt-lost"
    assert store.state() == before
    assert store.results() == committed_rows


def test_retry_cannot_reuse_snapshot_identity_with_different_settings(store):
    request = store.claim("settings-bound")
    assert store.run(request)["published"] is True
    store.claim("new-owner")
    before, rows = store.state(), store.results()
    request["settings"]["backtest_fee_rate"] = 0.002

    error = store.run(request, success=False)

    assert error["code"] == "SNAPSHOT_CONFLICT"
    assert store.state() == before
    assert store.results() == rows


def _finish(process, *, timeout=5):
    stdout, stderr = process.communicate(timeout=timeout)
    assert process.returncode == 0, stderr.decode()
    assert not stderr
    return json.loads(stdout)


def _send(process, request):
    process.stdin.write(json.dumps(request).encode())
    process.stdin.close()
    process.stdin = None


def test_read_transaction_and_two_version_retention_remain_consistent(store):
    second = store.claim("second")
    with closing(store.connect()) as reader:
        reader.execute("BEGIN")
        assert (
            reader.execute("SELECT current_snapshot FROM account_analysis WHERE account_id=7").fetchone()[0]
            == "baseline"
        )
        baseline_count = reader.execute(
            "SELECT COUNT(*) FROM performance_result_point WHERE snapshot_id='baseline'"
        ).fetchone()[0]
        assert baseline_count > 0
        with store.start() as process:
            _send(process, second)
            if store.journal == "delete":
                # Rollback-journal readers prevent a writer's COMMIT. No
                # partial publication becomes visible while that reader lives.
                with pytest.raises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.1)
                reader.rollback()
            assert _finish(process)["published"] is True
        if store.journal == "wal":
            assert store.run(store.claim("third"))["published"] is True
            assert (
                reader.execute("SELECT current_snapshot FROM account_analysis WHERE account_id=7").fetchone()[0]
                == "baseline"
            )
            assert (
                reader.execute("SELECT COUNT(*) FROM performance_result_point WHERE snapshot_id='baseline'").fetchone()[
                    0
                ]
                == baseline_count
            )
            reader.rollback()
        else:
            assert store.run(store.claim("third"))["published"] is True

    state = store.state()
    assert (state["current_snapshot"], state["previous_snapshot"]) == ("third", "second")
    with closing(store.connect()) as connection:
        assert {row[0] for row in connection.execute("SELECT id FROM performance_snapshot")} == {"second", "third"}
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    for table, rows in store.results().items():
        if table != "performance_snapshot":
            assert all(row[0] in {"second", "third"} for row in rows), table


def _wait_for_main_journal(store, process):
    journal = store.database.with_name(store.database.name + "-journal")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        assert process.poll() is None, "Publisher exited before reaching its final write transaction"
        if journal.exists() and journal.stat().st_size > 0:
            return
        threading.Event().wait(0.005)
    pytest.fail("Publisher did not enter a journaled main-database write transaction")


def test_kill_after_main_writes_before_commit_preserves_old_snapshot(store):
    if store.journal != "delete":
        pytest.skip("A held reader gates the precommit boundary only in rollback-journal mode")
    request = store.claim("killed-writer")
    before, rows = store.state(), store.results()
    with closing(store.connect()) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT current_snapshot FROM account_analysis WHERE account_id=7").fetchone()
        with store.start() as process:
            _send(process, request)
            _wait_for_main_journal(store, process)
            process.kill()
            process.communicate(timeout=2)
        reader.rollback()

    # A fresh connection performs SQLite hot-journal recovery where necessary.
    assert store.state() == before
    assert store.results() == rows
