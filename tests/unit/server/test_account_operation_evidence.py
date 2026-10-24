"""账户操作独立导出，真实执行限额、审计事实和资源预算仍然有效。"""

import asyncio
import json
from dataclasses import replace

import pytest

from axile.server.account_control_audit import query_account_control_audit_executions
from axile.server.api.routes import account_evidence
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.db.models.execution import ExecutionArtifact
from axile.server.evidence.models import EvidenceLimits, ExportBudget
from tests.unit.server.test_evidence_export import _client, _control, _csv, _event, _intent, _manifest, _seed, _zip
from tests.unit.server.test_evidence_export import database as database

_DATE_URL = "/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15"


def _jsonl(archive, path):
    return [json.loads(line) for line in archive.read(path).splitlines()]


def _session(key, **overrides):
    return AccountControlSession(
        **{
            "instance_id": key,
            "account_id": 1,
            "execution_id": key,
            "timezone": "Asia/Shanghai",
            "started_at_ms": 1792026000000,
            "finished_at_ms": 1792026000100,
            "status": "closed",
            "context_kind": "account_operation",
            "source": "order_query",
            **overrides,
        }
    )


def _counter(key, **overrides):
    return (
        "account_control_counter_delta",
        {
            "account_id": 1,
            "execution_id": key,
            "control_date": "2026-10-15",
            "bucket_type": "day",
            "bucket_start": "2026-10-15T00:00:00+08:00",
            "scope_type": "account",
            "operation": "query_order",
            "delta_count": 1,
            "delta_uid": key,
            "context_kind": "account_operation",
            "source": "order_query",
            **overrides,
        },
    )


def test_thousands_of_operations_preserve_all_audits_and_only_one_execution(database):
    rows = [_intent(), _event()]
    keys = {f"orders-{index:032x}" for index in range(1001)}
    for index, key in enumerate(sorted(keys)):
        source, operation = ("manual_cancel", "cancel_order") if index == 0 else ("order_query", "query_order")
        rows.extend(
            [
                _control(
                    key,
                    context_kind="account_operation",
                    source=source,
                    operation=operation,
                    metadata_={"password": "do-not-export", "order_id": "native-1"},
                ),
                _session(key, source=source),
                _counter(key, source=source, operation=operation),
            ]
        )
    _seed(database, *rows)
    archive = _zip(_client(database).get(_DATE_URL))
    manifest = _manifest(archive)
    assert manifest["schema_version"] == 3
    assert [item["key"] for item in manifest["executions"]] == ["exec-1"]
    assert manifest["account_operations"]["count"] == 1001
    index = _jsonl(archive, manifest["account_operations"]["index"])
    assert {row["context_id"] for row in index} == keys
    for filename in ("control-events", "counter-deltas", "control-sessions"):
        values = _jsonl(archive, f"account-operations/{filename}.jsonl")
        assert len(values) == 1001
        assert {row["execution_id"] for row in values} == keys
        assert {row["context_kind"] for row in values} == {"account_operation"}
        assert {row["source"] for row in values} == {"order_query", "manual_cancel"}
    assert {row["operation"]: int(row["events"]) for row in _csv(archive, "control-summary.csv")} == {
        "cancel_order": 1,
        "query_order": 1000,
    }
    assert len(archive.namelist()) < 30
    assert "do-not-export" not in b"\n".join(archive.read(name) for name in archive.namelist()).decode()


@pytest.mark.parametrize("prefix", ["orders", "catalog"])
@pytest.mark.parametrize("profile", ["shareable", "internal"])
def test_historical_generated_ids_are_classified_for_date_and_explicit_export(database, prefix, profile):
    key = f"{prefix}-{'a' * 32}"
    _seed(database, _control(key))
    for url in (_DATE_URL + f"&profile={profile}", f"/account/executions/{key}/evidence?profile={profile}"):
        archive = _zip(_client(database).get(url))
        manifest = _manifest(archive)
        assert manifest["executions"] == []
        assert manifest["account_operations"]["count"] == 1
        assert _jsonl(archive, "account-operations/control-events.jsonl")[0]["execution_id"] == key


def test_real_business_material_overrides_a_reserved_prefix_outside_the_selected_day(database):
    key = f"orders-{'b' * 32}"
    _seed(database, _control(key), _event(key, ts_local_created="2026-10-14T09:00:00+08:00"))
    archive = _zip(_client(database).get(_DATE_URL))
    assert [item["key"] for item in _manifest(archive)["executions"]] == [key]
    assert _manifest(archive)["account_operations"]["count"] == 0
    assert len(_jsonl(archive, f"executions/{key}/events.jsonl")) == 1


def test_explicit_types_override_prefixes_and_unknown_history_is_preserved(database):
    key = f"orders-{'c' * 32}"
    _seed(
        database,
        _control(key, context_kind="execution"),
        _control("orders-unknown"),
        _control("plugin-operation", context_kind="account_operation", source="order_query"),
        _control("orders-" + "f" * 32, context_kind="future-plugin-kind"),
    )
    archive = _zip(_client(database).get(_DATE_URL))
    assert {item["key"] for item in _manifest(archive)["executions"]} == {key, "orders-unknown", "orders-" + "f" * 32}
    assert [item["context_id"] for item in _jsonl(archive, "account-operations/contexts.jsonl")] == ["plugin-operation"]


def test_session_and_counter_only_operations_keep_missing_evidence_truthful(database):
    _seed(database, _session("session-only", status="incomplete", finished_at_ms=None), _counter("counter-only"))
    archive = _zip(_client(database).get(_DATE_URL))
    assert _manifest(archive)["executions"] == []
    reports = {item["context_id"]: item for item in _jsonl(archive, "account-operations/contexts.jsonl")}
    assert "control_session_not_cleanly_finished" in reports["session-only"]["coverage"]["issues"]
    assert "control_counter_activity_time_unknown" in reports["counter-only"]["coverage"]["issues"]
    assert _csv(archive, "control-summary.csv") == []


def test_real_execution_limit_still_applies_including_promoted_artifact_only_contexts(database, monkeypatch):
    keys = [f"orders-{index:032x}" for index in range(2)]
    _seed(
        database,
        *[_control(key) for key in keys],
        *[ExecutionArtifact(execution_id=key, artifact_type="execution_summary", content={}) for key in keys],
    )
    monkeypatch.setattr(
        account_evidence, "ExportBudget", lambda: ExportBudget(limits=replace(EvidenceLimits(), executions=1))
    )
    response = _client(database).get(_DATE_URL)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "EVIDENCE_TOO_MANY_EXECUTIONS"


@pytest.mark.parametrize("limit", [{"rows": 1}, {"bytes": 100}])
def test_account_operations_still_obey_shared_resource_limits(database, monkeypatch, limit):
    _seed(
        database, _control("op-1", context_kind="account_operation"), _control("op-2", context_kind="account_operation")
    )
    monkeypatch.setattr(
        account_evidence, "ExportBudget", lambda: ExportBudget(limits=replace(EvidenceLimits(), **limit))
    )
    response = _client(database).get(_DATE_URL)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "EVIDENCE_TOO_LARGE"


def test_account_operation_ownership_is_checked_before_export(database):
    _seed(database, _control("op", context_kind="account_operation"), _control("op", seq=2, account_id=2))
    assert _client(database).get(_DATE_URL).status_code == 409


def test_control_audit_list_exposes_context_type_and_all_sources(database):
    _seed(
        database,
        _control(context_kind="execution", source="algorithm"),
        _control(seq=2, context_kind="execution", source="manual_cancel"),
        _control("op", context_kind="account_operation", source="order_query"),
        _control("old-unknown"),
    )

    async def verify():
        page = json.loads(await query_account_control_audit_executions(account_id=1, control_date="2026-10-15"))
        assert page["count"] == 3
        contexts = {row["execution_id"]: row for row in page["data"]}
        assert contexts["exec-1"]["context_kind"] == "execution"
        assert contexts["exec-1"]["sources"] == ["algorithm", "manual_cancel"]
        assert contexts["op"]["context_kind"] == "account_operation"
        assert contexts["old-unknown"]["context_kind"] == "unknown"

    asyncio.run(verify())


def test_account_operation_logs_are_included_with_the_same_privacy_boundary(database, monkeypatch, tmp_path):
    _seed(database, _control("op", context_kind="account_operation", source="order_query"))
    (tmp_path / "axile-test.jsonl").write_text(
        json.dumps(
            {
                "message": "password=hidden",
                "context": {"account_id": 1, "execution_id": "op", "operation": "query_order"},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(account_evidence.settings, "app_log_dir", tmp_path)
    archive = _zip(_client(database).get(_DATE_URL + "&include_logs=true"))
    assert _manifest(archive)["logs"]["matched_rows"] == 1
    assert _jsonl(archive, "logs.jsonl")[0]["context"]["execution_id"] == "op"
    assert "hidden" not in archive.read("logs.jsonl").decode()


def test_unknown_or_conflicting_context_types_stay_unknown_for_both_export_paths(database):
    _seed(
        database,
        _control("old-unknown"),
        _control("conflicting", context_kind="account_operation"),
        _control("conflicting", seq=2),
    )
    for key in ("old-unknown", "conflicting"):
        for url in (_DATE_URL, f"/account/executions/{key}/evidence"):
            archive = _zip(_client(database).get(url))
            report = next(item for item in _manifest(archive)["executions"] if item["key"] == key)
            assert report["context_kind"] == "unknown"
            assert report["task_status"] == "UNKNOWN"
