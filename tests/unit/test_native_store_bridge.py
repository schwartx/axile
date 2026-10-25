"""原生业务桥接不引入会话，且只跨越一次 JSON 请求边界。"""

import asyncio
import json
import math
import subprocess
import sys
import threading

import pytest

from axile.server import native_store


def test_native_operation_serializes_once_and_returns_receipt(monkeypatch):
    receipt = {"committed": True}
    calls = []

    def stage(path, request_json):
        calls.append((path, json.loads(request_json)))
        return receipt

    monkeypatch.setattr("axile.server.core.db.database_path", lambda: "/unused.sqlite")
    monkeypatch.setattr(native_store._core, "bridge_stage", stage, raising=False)
    result = native_store.native_operation_sync("bridge_stage", {"request": {"name": "账户", "amount": "1.25"}})

    assert result is receipt
    assert calls == [("/unused.sqlite", {"request": {"name": "账户", "amount": "1.25"}})]


def test_native_operation_runs_off_loop_and_preserves_bytes_and_errors(monkeypatch):
    loop_thread = threading.get_ident()
    response = b'{"ok":true}'

    def stage(path, request_json):
        assert threading.get_ident() != loop_thread
        assert path == "/unused.sqlite"
        if json.loads(request_json).get("fail"):
            raise native_store._core.NativeError("DATABASE_WRITE", "rejected")
        return response

    monkeypatch.setattr("axile.server.core.db.database_path", lambda: "/unused.sqlite")
    monkeypatch.setattr(native_store._core, "bridge_stage_json", stage, raising=False)

    async def run():
        assert await native_store.native_operation("bridge_stage_json", {}) is response
        with pytest.raises(native_store._core.NativeError) as caught:
            await native_store.native_operation("bridge_stage_json", {"fail": True})
        assert caught.value.args == ("DATABASE_WRITE", "rejected")

    asyncio.run(run())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_native_operation_rejects_nonfinite_before_native_call(monkeypatch, value):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: "/unused.sqlite")

    def stage(*_args):
        pytest.fail("invalid evidence reached native stage")

    monkeypatch.setattr(native_store._core, "bridge_stage", stage, raising=False)
    with pytest.raises(ValueError, match="Out of range float"):
        native_store.native_operation_sync("bridge_stage", {"nested": [value]})


@pytest.mark.parametrize(
    "name",
    [
        "persist_execution_state",
        "freeze_execution_input",
        "freeze_execution_after_assets",
        "asset_snapshot_write",
        "target_operation",
        "portfolio_operation",
    ],
)
def test_fact_stage_preserves_nonfinite_tokens_and_genuine_marker_objects(monkeypatch, name):
    calls = []

    def stage(path, request_json):
        calls.append((path, json.loads(request_json)))

    monkeypatch.setattr(native_store._core, "EVIDENCE_INPUT_STAGES", (name,), raising=False)
    monkeypatch.setattr(native_store._core, name, stage, raising=False)
    monkeypatch.setattr(native_store._core, "bridge_stage", stage, raising=False)
    native_store.native_operation_sync(
        name,
        {"values": [float("nan"), float("inf"), float("-inf"), {"invalid_number": "nan"}, "NaN"]},
        database="/unused.sqlite",
    )

    assert calls[0][0] == "/unused.sqlite"
    values = calls[0][1]["values"]
    assert math.isnan(values[0])
    assert values[1:3] == [float("inf"), float("-inf")]
    assert values[3:] == [{"invalid_number": "nan"}, "NaN"]

    with pytest.raises(ValueError, match="Out of range float"):
        native_store.native_operation_sync("bridge_stage", {"value": float("nan")}, database="/unused.sqlite")


def test_startup_migrates_once_per_path_and_retries_failures(monkeypatch):
    from axile.server.core import db

    paths = iter(("/one.sqlite", "/one.sqlite", "/two.sqlite", "/two.sqlite"))
    calls = []

    def migrate():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("migration failed")

    monkeypatch.setattr(db, "database_path", lambda: next(paths))
    monkeypatch.setattr(db, "_initialized_databases", set())

    db.initialize_database(migrate)
    db.initialize_database(migrate)
    with pytest.raises(RuntimeError, match="migration failed"):
        db.initialize_database(migrate)
    db.initialize_database(migrate)

    assert calls == [True, True, True]


def test_explicit_database_preserves_captured_path_without_reading_settings(monkeypatch):
    calls = []

    def stage(path, request_json):
        calls.append((path, json.loads(request_json)))

    def unexpected_path():
        pytest.fail("explicit database read the configured path")

    monkeypatch.setattr("axile.server.core.db.database_path", unexpected_path)
    monkeypatch.setattr(native_store._core, "bridge_stage", stage, raising=False)
    native_store.native_operation_sync("bridge_stage", {}, database="/captured.sqlite")
    asyncio.run(native_store.native_operation("bridge_stage", {}, database="/probe.sqlite"))

    assert calls == [("/captured.sqlite", {}), ("/probe.sqlite", {})]


def test_public_startup_uses_one_captured_path(monkeypatch):
    from axile.server.core import db

    calls = []
    paths = iter(("/startup.sqlite",))

    def stage(path, request_json):
        calls.append((path, json.loads(request_json)))

    monkeypatch.setattr(db, "database_path", lambda: next(paths))
    monkeypatch.setattr(db, "_initialized_databases", set())
    monkeypatch.setattr(native_store._core, "migration_upgrade", stage, raising=False)
    db.initialize_database()

    assert calls == [("/startup.sqlite", {})]


@pytest.mark.parametrize("worker_error", [False, True])
def test_cancellation_drains_worker_before_releasing_account_lock(monkeypatch, worker_error):
    entered, release, finished = (threading.Event() for _ in range(3))

    def stage(*_args):
        entered.set()
        assert release.wait(2)
        finished.set()
        if worker_error:
            raise RuntimeError("late native failure")

    monkeypatch.setattr(native_store._core, "bridge_stage", stage, raising=False)

    async def run():
        lock = asyncio.Lock()

        async def operation():
            async with lock:
                await native_store.native_operation("bridge_stage", {}, database="/unused.sqlite")

        caller = asyncio.create_task(operation())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert lock.locked()
            assert not caller.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished.is_set()
        assert not lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("task_cancel", [False, True])
def test_evidence_cancellation_signals_native_token_and_drains_worker(monkeypatch, task_cancel):
    entered, cancelled, release, finished = (threading.Event() for _ in range(4))
    external = threading.Event()

    class Control:
        def cancel(self):
            cancelled.set()

    def stage(path, request_json, control):
        assert path == "/unused.sqlite"
        assert json.loads(request_json) == {"account_id": 1}
        assert isinstance(control, Control)
        entered.set()
        assert cancelled.wait(2)
        assert release.wait(2)
        finished.set()
        raise native_store._core.NativeError("EVIDENCE_CANCELLED", "cancelled")

    monkeypatch.setattr("axile.server.core.db.database_path", lambda: "/unused.sqlite")
    monkeypatch.setattr(native_store._core, "EvidenceCancellation", Control, raising=False)
    monkeypatch.setattr(native_store._core, "collect_evidence", stage, raising=False)

    async def run():
        caller = asyncio.create_task(native_store.native_evidence_operation({"account_id": 1}, external))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            if task_cancel:
                caller.cancel()
            else:
                external.set()
            assert await asyncio.to_thread(cancelled.wait, 1)
            assert not caller.done()
        finally:
            release.set()
        expected = asyncio.CancelledError if task_cancel else native_store._core.NativeError
        with pytest.raises(expected):
            await caller
        assert finished.is_set()

    asyncio.run(run())


@pytest.mark.skipif(not hasattr(native_store._core, "migration_upgrade"), reason="requires rebuilt native store")
def test_native_bigint_roundtrip(tmp_path):
    database = str(tmp_path / "bigint.sqlite")
    numbers = {
        "above_u64": 2**100 + 123,
        "below_i64": -(2**100 + 321),
        "beyond_f64": 10**400 + 456,
        "float": 1.25,
        "whole_float": 1.0,
    }
    native_store.native_operation_sync("migration_upgrade", {}, database=database)
    saved = native_store.native_operation_sync(
        "account_create",
        {
            "now": "2026-10-23T09:00:00",
            "account": {
                "name": "bigint",
                "market": "期货",
                "trade_channel": "ctp",
                "brokerage": "test",
                "account_config": numbers,
                "portfolio_id": None,
            },
        },
        database=database,
    )["account"]
    restored = native_store.native_operation_sync("account_get", {"account_id": saved["id"]}, database=database)
    for account in (saved, restored):
        assert account["account_config"] == numbers
        assert type(account["account_config"]["above_u64"]) is int
        assert type(account["account_config"]["below_i64"]) is int
        assert type(account["account_config"]["beyond_f64"]) is int
        assert type(account["account_config"]["whole_float"]) is float


@pytest.mark.skipif(not hasattr(native_store._core, "migration_upgrade"), reason="requires rebuilt native store")
@pytest.mark.parametrize("corruption", ["digit_limit", "malformed_json"])
def test_native_corrupt_database_returns_error_without_aborting_process(tmp_path, corruption):
    if corruption == "digit_limit" and sys.get_int_max_str_digits() == 0:
        pytest.skip("interpreter integer digit limit disabled")
    script = r"""
import json
import sqlite3
import sys
from contextlib import closing

from axile.server._native import _core

database, corruption = sys.argv[1:]
_core.migration_upgrade(database, "{}")
saved = _core.account_create(database, json.dumps({
    "now": "2026-10-23T09:00:00",
    "account": {"name": "corrupt", "market": "期货", "trade_channel": "ctp",
                "brokerage": "test", "account_config": {}, "portfolio_id": None},
}))["account"]
if corruption == "digit_limit":
    encoded = '{"huge":' + "9" * (sys.get_int_max_str_digits() + 1) + "}"
else:
    encoded = '{"broken":'
with closing(sqlite3.connect(database)) as connection, connection:
    connection.execute("UPDATE account_settings SET connection=? WHERE account_id=?", (encoded, saved["id"]))
try:
    _core.account_get(database, json.dumps({"account_id": saved["id"]}))
except ValueError as error:
    assert corruption == "digit_limit", str(error)
    assert "limit" in str(error), str(error)
except _core.NativeError:
    assert corruption == "malformed_json"
else:
    raise AssertionError("invalid account JSON did not raise")
assert _core.migration_current(database, "{}")["public"] == "0034"
print("native-process-alive")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "corrupt.sqlite"), corruption],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "native-process-alive" in result.stdout
