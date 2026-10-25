"""Keep service benchmarking honest about semantics and memory boundaries."""

import ast
import asyncio
import json
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import benchmark_business_fact_service as service
from scripts import benchmark_business_fact_storage as storage
from scripts.benchmark_business_fact_service import (
    aggregate,
    common_semantics,
    endpoint_paths,
    memory_evidence,
    network_guard,
    prioritize_benchmark_routes,
    semantic_hash,
    source_identity,
    source_tree_hash,
    verify_writes,
)
from scripts.benchmark_business_fact_storage import pinned_wal_reader, sqlite_settings, wal_commit_count


def test_native_fixture_preserves_complete_account_defaults_without_create_dto(monkeypatch, tmp_path):
    from axile.server._native import _core
    from axile.server.db.models.account import AccountCreate

    operations = []
    database = tmp_path / "synthetic.sqlite"
    monkeypatch.setattr(_core, "migration_upgrade", lambda *_args: None, raising=False)
    monkeypatch.setattr(storage, "initialize_benchmark_database", lambda _path: None)
    monkeypatch.setattr(
        "axile.server.native_store.native_operation_sync",
        lambda name, request, **options: operations.append((name, request, options)),
    )
    storage.prepare_database(database, tmp_path)
    _, request, options = next(row for row in operations if row[0] == "account_create")
    payload = request["account"]
    required = {name for name, field in AccountCreate.model_fields.items() if field.is_required()}
    assert required <= payload.keys()
    assert payload["backtest_weight_type"] == "cs"
    assert payload["backtest_fee_rate"] == 0.0002
    assert payload["portfolio_id"] == 1
    assert payload["created_at"] == "2025-01-01T09:00:00"
    assert options["database"] == str(database)
    assert not database.exists()


def test_legacy_fixture_records_the_same_portfolio_binding_as_native_create(monkeypatch, tmp_path):
    from axile.server._native import _core

    added = []

    class Context:
        attributes = {}

        def __enter__(self):
            return self

        def __exit__(self, *_error):
            pass

        def add(self, value):
            added.append(value)

        def flush(self):
            pass

        def commit(self):
            pass

    monkeypatch.setattr("axile.server.db.models.PortfolioAccount", SimpleNamespace, raising=False)
    monkeypatch.setattr(_core, "migration_upgrade", None, raising=False)
    monkeypatch.setattr("sqlalchemy.create_engine", lambda *_args: SimpleNamespace(begin=Context, dispose=lambda: None))
    monkeypatch.setattr("sqlalchemy.orm.Session", lambda *_args: Context())
    monkeypatch.setattr("alembic.command.upgrade", lambda *_args: None)
    storage.prepare_database(tmp_path / "synthetic.sqlite", tmp_path)
    binding = next(value for value in added if isinstance(value, SimpleNamespace))
    assert (binding.account_id, binding.portfolio_id, binding.created_at) == (1, 1, "2025-01-01T09:00:00")


def test_matching_detail_preserves_missing_null_and_nested_occurrences():
    # Baseline ExecuteRecordPublic has no account_id; ownership is route-scoped.
    head = {"id": 2, "execution_id": None, "created_at": "frozen", "is_success": 1}
    source = {"curr_target": {"A": 0}, "last_target": None}
    result = {"orders": [{"order_id": ""}, {"order_id": ""}], "extra": {"known": False}}
    baseline = {"data": [{**head, "raw_input": source, "raw_result": result}], "count": 1}
    candidate = {**head, "input": source, "result": result, "trade_count": 0}
    assert common_semantics("detail", baseline, "baseline") == common_semantics("detail", candidate, "candidate")
    fingerprint = semantic_hash(common_semantics("detail", candidate, "candidate"))
    candidate["input"] = {"curr_target": {}, "last_target": None}
    assert semantic_hash(common_semantics("detail", candidate, "candidate")) != fingerprint
    assert len({semantic_hash(value) for value in (None, 0, False, {}, [], {"extra": {"value": None}})}) == 6


def test_history_compares_head_without_hiding_evidence_in_detail():
    head = dict(id=1, execution_id="e", created_at="frozen", is_success=0)
    old = {"data": [{**head, "raw_result": {"large": "old"}}], "count": 8}
    new = {"data": [{**head, "symbol_count": 2}], "count": 8}
    assert common_semantics("history", old, "baseline") == common_semantics("history", new, "candidate")
    config = {"account_id": 1, "record_id": 7, "arm": "baseline"}
    assert endpoint_paths(config)["detail"].endswith("/execute_records/1?record_id=7&limit=1")
    assert endpoint_paths({**config, "arm": "candidate"})["detail"].endswith("/execute_records/1/7")
    assert endpoint_paths({**config, "response_layout": "typed"})["detail"].endswith("/execute_records/1/7")


def test_fingerprint_includes_rust_sql_cargo_and_actual_native_artifact(tmp_path):
    for name in (
        "axile/writer.py",
        "rust/storage/src/store.rs",
        "rust/storage/src/migration.sql",
        "rust/storage/Cargo.lock",
        "rust/storage/build.rs",
        "axile/server/_native/_core.abi3.so",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"before")
    fingerprint = source_tree_hash(tmp_path)
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            path.write_bytes(b"after")
            assert source_tree_hash(tmp_path) != fingerprint
            path.write_bytes(b"before")
            assert source_tree_hash(tmp_path) == fingerprint


def test_sqlite_settings_are_observed_per_connection(tmp_path):
    with sqlite3.connect(tmp_path / "settings.sqlite") as connection:
        connection.execute("PRAGMA busy_timeout=17")
        connection.execute("PRAGMA synchronous=OFF")
        settings = sqlite_settings(connection)
    assert settings["busy_timeout"] == 17
    assert settings["synchronous"] == 0
    assert settings["journal_mode"] == "delete"


def test_wal_commit_count_includes_native_and_python_commits_but_not_rollback(tmp_path):
    from axile.server._native import _core

    path = tmp_path / "commits.sqlite"
    with sqlite3.connect(path) as monitor, sqlite3.connect(path) as writer:
        monitor.execute("PRAGMA journal_mode=WAL")
        monitor.executescript(
            "CREATE TABLE marker(id INTEGER); CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY, requested INTEGER DEFAULT 0);"
        )
        monitor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with pinned_wal_reader(path):
            writer.execute("INSERT INTO marker VALUES(1)")
            writer.commit()
            _core.queue_enqueue(str(path), 1, False)
            writer.execute("INSERT INTO marker VALUES(2)")
            writer.rollback()
            assert wal_commit_count(path) == 2
    Path(str(path) + "-wal").write_bytes(b"partial")
    with pytest.raises(AssertionError, match="WAL header"):
        wal_commit_count(path)


def test_memory_uses_simultaneous_sample_and_reports_unsampled_requests():
    requests = [{"start": 1.1, "end": 1.9}, {"start": 2.1, "end": 2.2}]
    result = memory_evidence([(1.0, 100, 0), (1.5, 150, 1), (2.0, 120, 0)], requests)
    assert result == {"sampled_combined_process_peak_bytes": 150, "sampled_max_children": 1, "sample_count": 3}
    assert requests[0]["sampled_combined_peak_bytes"] == 150
    assert requests[1]["sampled_combined_peak_bytes"] is None


def test_timing_summary_excludes_instrumented_latency():
    runs = [
        {
            "arm": "baseline",
            "mode": "timing",
            "samples": [
                {"endpoint": "detail", "phase": "warm", "seconds": 0.1, "bytes": 12},
                {"endpoint": "detail", "phase": "warm", "seconds": 0.2, "bytes": 12},
            ],
        },
        {
            "arm": "baseline",
            "mode": "rss",
            "sampled_combined_process_peak_bytes": 123,
            "sample_count": 7,
            "sampled_after_workload_median_bytes": 120,
            "samples": [{"endpoint": "detail", "phase": "warm", "seconds": 999, "bytes": 12}],
        },
    ]
    summary = aggregate(runs)["baseline"]
    assert summary["http"]["warm/detail"]["sample_count"] == 2
    assert summary["http"]["warm/detail"]["p50_seconds"] == pytest.approx(0.15)
    assert summary["http"]["warm/detail"]["p95_seconds"] == 0.2
    assert summary["sampled_combined_process_peak_bytes"] == 123


def test_network_guard_rejects_network_before_socket_connect():
    with network_guard(12345), socket.socket() as connection:
        for address in (("203.0.113.1", 80), ("127.0.0.1", 12346)):
            with pytest.raises(AssertionError, match="outbound"):
                connection.connect(address)
            with pytest.raises(AssertionError, match="outbound"):
                connection.connect_ex(address)


def test_adapters_remain_reachable_with_embedded_spa_catchall():
    api = SimpleNamespace(path="/api/v1/account/")
    spa = SimpleNamespace(path="/{path:path}")
    adapter = SimpleNamespace(path="/__business_fact_benchmark/last-target")
    routes = [api, spa, adapter]
    prioritize_benchmark_routes(routes)
    assert routes == [adapter, api, spa]


@pytest.mark.parametrize(
    ("record", "sessions"),
    [
        pytest.param({"record_id": 3, "execution_id": "e3", "is_success": 0}, None, id="native-receipt"),
        pytest.param(SimpleNamespace(id=3, execution_id="e3", is_success=0), None, id="legacy-native"),
        pytest.param(SimpleNamespace(id=3, execution_id="e3", is_success=0), object(), id="legacy-session"),
    ],
)
def test_write_adapter_preserves_receipt_and_legacy_record_heads(record, sessions):
    tree = ast.parse(Path(service.__file__).read_text())
    server = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve")
    handler = next(node for node in server.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "write")
    handler.decorator_list = []
    writer = AsyncMock(return_value=record)
    scope = {
        "Body": lambda: None,
        "records": SimpleNamespace(_persist_execute_record=writer),
        "sessions": sessions,
        "config": {"account_id": 7},
    }
    exec(compile(ast.Module(body=[handler], type_ignores=[]), service.__file__, "exec"), scope)
    payload = {"execution_id": "e3", "raw_input": {"curr_target": {"A": 0}}, "raw_result": {}, "is_success": 0}

    assert asyncio.run(scope["write"](payload)) == {"id": 3, "execution_id": "e3", "is_success": 0}
    writer.assert_awaited_once_with(
        account_id=7, **payload, **({"session_factory": sessions} if sessions is not None else {})
    )


def test_write_verification_rejects_success_response_without_committed_rows(monkeypatch):
    monkeypatch.setattr(service, "request", lambda *_args: ({}, {"count": 2, "data": []}))
    with pytest.raises(AssertionError, match="committed count"):
        verify_writes({}, {}, [{"id": 3, "execution_id": "e3"}], 2, {})


def test_write_verification_rereads_exact_input_result_and_last_target(monkeypatch):
    template = {"raw_input": {"curr_target": {"A": 0.0}}, "raw_result": {"orders": []}, "is_success": 1}
    head = {"id": 3, "execution_id": "e3", "created_at": "new-time", "is_success": 1}
    responses = iter(
        [
            {"count": 3, "data": []},
            {**head, "input": template["raw_input"], "result": template["raw_result"]},
            {"A": 0.0},
        ]
    )
    monkeypatch.setattr(service, "request", lambda *_args: ({}, next(responses)))
    result = verify_writes({"arm": "candidate"}, template, [{"id": 3, "execution_id": "e3"}], 2, {})
    assert result["committed_records"] == 1
    assert result["last_target_sha256"] == semantic_hash({"A": 0.0})


def test_archive_identity_requires_no_git_checkout(tmp_path):
    identity = {"commit": "59dac8b", "archive_sha256": "frozen", "kind": "git archive source snapshot"}
    (tmp_path / "BASELINE_SOURCE.json").write_text(json.dumps(identity))
    assert source_identity(tmp_path) == identity
    assert not (tmp_path / ".git").exists()


def test_adapter_decorators_satisfy_actual_application_unique_id_generator():
    from fastapi import FastAPI

    from axile.server.app import custom_generate_unique_id

    application = FastAPI(generate_unique_id_function=custom_generate_unique_id)
    tree = ast.parse(Path(service.__file__).read_text())
    server = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve")
    adapters = []
    for function in server.body:
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        for decorator in function.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            path = ast.literal_eval(decorator.args[0])
            if path.startswith("/__business_fact_benchmark/"):
                options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in decorator.keywords}
                # Use the actual app generator on options extracted from the real
                # adapters, so removing their tags reproduces its IndexError.
                application.add_api_route(
                    path, lambda: {}, name=function.name, methods=[decorator.func.attr.upper()], **options
                )
                adapters.append(path)
    assert len(adapters) == 2
    operations = application.openapi()["paths"]
    assert all(
        operation["operationId"].startswith("benchmark-")
        for path in adapters
        for operation in operations[path].values()
    )
