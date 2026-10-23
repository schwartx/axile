"""Protect storage benchmark evidence, integrity and physical-size accounting."""

import json
import socket
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from axile.domain.execution import ExecutionArtifactType
from scripts.benchmark_business_fact_storage import (
    DatasetSpec,
    analysis_fact_hashes,
    artifact_business_evidence,
    checkpoint_storage,
    event_business_evidence,
    freeze_fixture,
    inspect_storage,
    iter_fixture,
    latency_summary,
    no_network,
    semantic_hash,
    source_hash,
    source_identity,
    synthetic_record,
)
from scripts.business_facts_contract import business_evidence


def test_frozen_fixture_is_reproducible_and_matches_occurrence_counts(tmp_path):
    spec = DatasetSpec(records=2, symbols=3, orders_per_symbol=2, trades_per_order=3)
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    one, two = freeze_fixture(first, spec), freeze_fixture(second, spec)
    assert business_evidence(one) == business_evidence(two)
    assert business_evidence(one["counts"]) == business_evidence(
        {"executions": 2, "symbols": 6, "orders": 12, "trades": 36, "after_positions": 6}
    )
    items = list(iter_fixture(first))
    assert "inputs" not in items[0]["raw_result"]
    assert items[0]["raw_input"]["curr_target"] != items[0]["raw_input"]["last_target"]
    assert business_evidence(len(items[0]["before_account_assets"]["positions"])) == business_evidence(0)
    assert business_evidence(len(items[0]["raw_result"]["account_assets"]["positions"])) == business_evidence(3)
    with pytest.raises(FileExistsError):
        freeze_fixture(first, spec)
    first.write_bytes(first.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        list(iter_fixture(first))


def test_historical_repeated_input_is_separate_and_hash_preserves_missing_types():
    spec = DatasetSpec(records=1, symbols=1, result_inputs=True)
    item = synthetic_record(0, spec)
    assert business_evidence(item["raw_input"]) == business_evidence(item["raw_result"]["inputs"])
    assert business_evidence(
        len(
            {
                semantic_hash(value)
                for value in (
                    {},
                    {"curr_target": None},
                    {"curr_target": {}},
                    {"curr_target": {"A": 0}},
                    {"curr_target": {"A": None}},
                )
            }
        )
    ) == business_evidence(5)
    with pytest.raises(ValueError):
        DatasetSpec(symbols=0)


def test_storage_accounts_utf8_json_indexes_and_checkpointed_main_file(tmp_path):
    path = tmp_path / "measurement.sqlite"
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY,payload JSON,label TEXT)")
        database.execute("CREATE INDEX ix_facts_label ON facts(label)")
        database.commit()
        empty = checkpoint_storage(database, path)
        body = json.dumps({"name": "合成"}, ensure_ascii=False)
        database.executemany(
            "INSERT INTO facts(payload,label) VALUES(?,?)", [(body, str(index)) for index in range(200)]
        )
        database.commit()
        pending = inspect_storage(database, path)
        assert pending["file_bytes"]["-wal"] > 0
        assert business_evidence(pending["tables"]["facts"]["json_utf8_bytes"]) == business_evidence(
            len(body.encode()) * 200
        )
        final = checkpoint_storage(database, path)
        assert business_evidence(final["checkpoint"]["busy"]) == business_evidence(0)
        assert business_evidence(final["file_bytes"]["-wal"]) == business_evidence(0)
        assert business_evidence(final["file_bytes"]["database"]) == business_evidence(final["allocated_page_bytes"])
        assert final["allocated_page_bytes"] > empty["allocated_page_bytes"]
        if final["dbstat_status"] == "available":
            index = next(item for item in final["objects"] if item["name"] == "ix_facts_label")
            assert business_evidence(index["type"]) == business_evidence("index")
            assert index["allocated_bytes"] > 0


def test_latency_and_network_guard_do_not_silently_fabricate_measurements():
    assert latency_summary([])["p50_ms"] is None
    assert business_evidence(latency_summary([0.002, 0.004, 0.003])["p50_ms"]) == business_evidence(3.0)
    with no_network(), socket.socket() as connection:
        with pytest.raises(AssertionError, match="outbound"):
            connection.connect(("203.0.113.1", 80))


def test_analysis_fingerprints_include_values_and_preserve_primary_key_order():
    with closing(sqlite3.connect(":memory:")) as database, database:
        database.executescript(
            "CREATE TABLE analysis_source_trade(record_id INTEGER,ordinal INTEGER,price REAL,PRIMARY KEY(record_id,ordinal));"
            "CREATE TABLE replay_observation(record_id INTEGER PRIMARY KEY,equity REAL);"
            "INSERT INTO analysis_source_trade VALUES(1,1,101),(1,0,100);"
            "INSERT INTO replay_observation VALUES(1,100000);"
        )
        before = analysis_fact_hashes(database)
        database.executescript(
            "DELETE FROM analysis_source_trade;INSERT INTO analysis_source_trade VALUES(1,0,100),(1,1,101);"
        )
        assert business_evidence(analysis_fact_hashes(database)) == business_evidence(before)
        database.execute("UPDATE analysis_source_trade SET price=102 WHERE ordinal=1")
        changed = analysis_fact_hashes(database)
        assert business_evidence(changed["analysis_source_trade"]["rows"]) == business_evidence(2)
        assert changed["analysis_source_trade"]["sha256"] != before["analysis_source_trade"]["sha256"]
        assert business_evidence(changed["replay_observation"]) == business_evidence(before["replay_observation"])


def test_manifested_archive_freeze_needs_no_git_and_checks_runtime_resources(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    (archive / "axile").mkdir(parents=True)
    manifest = {"commit": "a" * 40, "archive_sha256": "b" * 64, "kind": "git archive source snapshot"}
    (archive / "BASELINE_SOURCE.json").write_text(json.dumps(manifest))
    for name in ("pyproject.toml", "uv.lock", "alembic.ini", "hatch_build.py"):
        (archive / name).write_text("")
    (archive / "axile" / "writer.py").write_text("value = 1\n")
    resource = archive / "axile" / "resource.json"
    resource.write_text('{"mode": "first"}')

    def reject_git(*_args, **_kwargs):
        raise AssertionError("manifested archive must not invoke Git")

    monkeypatch.setattr("scripts.benchmark_business_fact_storage.subprocess.check_output", reject_git)
    identity = source_identity(archive, "aaaaaaa")
    assert business_evidence(identity["kind"]) == business_evidence("manifested_archive")
    assert business_evidence(identity["archive_sha256"]) == business_evidence(manifest["archive_sha256"])
    before = source_hash(archive)
    resource.write_text('{"mode": "second"}')
    assert source_hash(archive) != before
    with pytest.raises(ValueError, match="requested frozen checkpoint"):
        source_identity(archive, "ccccccc")
    (archive / "BASELINE_SOURCE.json").write_text(json.dumps({**manifest, "archive_sha256": "invalid"}))
    with pytest.raises(ValueError, match="manifest is invalid"):
        source_identity(archive, "aaaaaaa")


@pytest.mark.parametrize("kind", ["TARGET_SNAPSHOT", ExecutionArtifactType.TARGET_SNAPSHOT])
def test_artifact_semantics_separate_reference_protocol_without_filling_missing_keys(kind):
    old = SimpleNamespace(execution_id="e1", artifact_type=kind, created_at="2025-01-01", schema_version=2)
    new = SimpleNamespace(**{**vars(old), "schema_version": 3})
    body = {"curr_target": {}, "sizing_context": {}}
    evidence = artifact_business_evidence(old, body)
    assert evidence == artifact_business_evidence(new, {**body, "business_facts_ref": {"version": 1}})
    assert evidence == artifact_business_evidence(new, {"curr_target": {}})
    assert evidence != artifact_business_evidence(new, {"curr_target": {}, "sizing_context": {"long_leverage": 2}})
    assert evidence != artifact_business_evidence(new, {"curr_target": None})
    assert evidence != artifact_business_evidence(new, {})


def test_event_comparison_keeps_hydrated_fields_and_detects_unrequested_defaults():
    event = SimpleNamespace(model_dump=lambda **_kwargs: {"id": 1, "schema_version": 1, "details": {}})
    before = event_business_evidence(event, {"decision": {"curr_target": {"S": 0.0}}})
    hydrated = {"decision": {"curr_target": {"S": 0.0}, "business_facts_ref": {"version": 1}}}
    assert event_business_evidence(event, hydrated) == before
    hydrated["decision"]["last_target"] = {}
    assert event_business_evidence(event, hydrated) != before
    assert event_business_evidence(event, {"decision": {"curr_target": {"S": None}}}) != before
