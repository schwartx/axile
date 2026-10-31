"""Keep storage measurements on the frozen instant across native time layouts."""

import asyncio
import hashlib
import json
import sqlite3
import time
from copy import deepcopy
from datetime import UTC

from axile.executor.algorithms.utils import clock
from scripts import benchmark_business_fact_storage as storage
from tests.unit.server._business_facts_test_support import seed_account


def test_frozen_clock_keeps_microseconds_and_restores_real_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(clock, "_default_clock", clock.RealClock())
    expected = 1793323800123456
    item = {"created_at": "2026-10-30T09:30:00.123456+08:00"}
    original_ns = time.time_ns
    original_time = clock.get_default_clock().time
    observed = []

    def write(*_args):
        before = time.perf_counter()
        observed.append((clock.clock_now_us(), clock.clock_now(tz=UTC).isoformat()))
        assert time.perf_counter() > before

    monkeypatch.setattr(storage, "_write_fixture_row", write)
    storage._run_writes([item], None, tmp_path / "unused.sqlite", "terminal")
    assert observed == [(expected, "2026-10-30T01:30:00.123456+00:00")]
    assert time.time_ns is original_ns
    assert clock.get_default_clock().time == original_time


def test_native_terminal_write_and_verification_keep_the_frozen_instant(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    item = storage.synthetic_record(
        0, storage.DatasetSpec(records=1, symbols=1, orders_per_symbol=1, trades_per_order=1)
    )
    item["created_at"] = "2026-10-30T09:30:00.123456+08:00"
    frozen = deepcopy(item)
    storage._run_writes([item], None, native_database, "terminal")
    digest = asyncio.run(storage._verify_records(None, [item], "typed"))
    expected = storage.semantic_hash({"input": item["raw_input"], "result": item["raw_result"]})
    assert digest == hashlib.sha256(expected.encode()).hexdigest()
    assert item == frozen


def test_native_text_time_layout_remains_readable(tmp_path, monkeypatch):
    from axile.server._native import _core

    database = tmp_path / "legacy.sqlite"
    item = {"execution_id": "legacy", "created_at": "2025-01-01T09:00:00", "raw_input": {}, "raw_result": {}}
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE executerecord(id INTEGER,execution_id TEXT,created_at TEXT)")
        connection.execute("INSERT INTO executerecord VALUES(1,?,?)", (item["execution_id"], item["created_at"]))
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(database))
    monkeypatch.setattr(_core, "Store", lambda path: path)
    monkeypatch.setattr(_core, "execution_record_detail", lambda *_args: json.dumps({"input": {}, "result": {}}))
    digest = asyncio.run(storage._verify_records(None, [item], "typed"))
    expected = storage.semantic_hash({"input": {}, "result": {}})
    assert digest == hashlib.sha256(expected.encode()).hexdigest()
