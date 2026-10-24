"""四个原生 JSON 读取出口与原公开模型的行为对照。"""

import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server._native import _core
from axile.server.api.routes import account_crud, account_execution
from axile.server.api.routes import init as init_routes
from axile.server.core import db
from axile.server.db.models import ExecutionArtifact, ExecutionArtifactPublic, ExecutionEvent, ExecutionEventPublic
from axile.server.db.models.account_runtime_sync import AccountRuntimeSyncPublic
from axile.server.db.models.system_notification import SystemNotificationStatePublic
from tests.conftest import native_store_call
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._legacy_result_oracle import normalize_legacy_result
from tests.unit.server._runtime_db_support import runtime_rows

_READS = [
    ("/account/executions/e1/events", "list_execution_events_json", {"execution_id": "e1", "skip": 0, "limit": 100}),
    (
        "/account/executions/e1/artifacts",
        "list_execution_artifacts_json",
        {"execution_id": "e1", "skip": 0, "limit": 100},
    ),
    ("/account/1/runtime-sync", "account_runtime_sync_json", {"account_id": 1}),
    ("/init/execution-alert/result", "notifications_get_system_json", {}),
]


@pytest.fixture
def client(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    monkeypatch.setattr(init_routes, "is_configured", lambda: True)
    app = FastAPI()
    app.include_router(account_execution.router, prefix="/account")
    app.include_router(account_crud.router, prefix="/account")
    app.include_router(init_routes.router)
    with TestClient(app, raise_server_exceptions=False) as result:
        yield result


@pytest.mark.parametrize("path,operation,payload", _READS)
def test_native_bytes_reach_http_unchanged(client, native_database, monkeypatch, path, operation, payload):
    content = '{ "保留原字节": [false, 0, null] }\n'.encode()
    query = Mock(return_value=content)
    monkeypatch.setattr(_core, operation, query)
    response = client.get(path)
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/json"
    assert query.call_count == 1
    assert query.call_args.args[0] == str(native_database)
    assert json.loads(query.call_args.args[1]) == payload
    schema = client.app.openapi()["paths"][path.replace("e1", "{execution_id}").replace("/1/", "/{account_id}/")]
    assert "$ref" in schema["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def _event(path, details, **overrides):
    values = ExecutionEvent(
        execution_id="e1",
        account_id=1,
        channel="ctp",
        algorithm="SINGLE-MAKER",
        event_uid="event-1",
        event_type="execution_started",
        status="INFO",
        reason_family="SYSTEM",
        reason_code="TEST.READ",
        details=details,
    ).model_dump(mode="json")
    native_store_call(path, "append_execution_event", values | overrides)


def _audit_rows(path, table):
    data = [dict(row) for row in runtime_rows(path, f"SELECT * FROM {table} ORDER BY id")]
    content, kind = ("details", "event_type") if table == "execution_event" else ("content", "artifact_type")
    for row in data:
        row[content] = json.loads(row[content])
        row[kind] = row[kind].lower()
    return data


def test_event_fields_enums_and_error_promotion_match_public_model(client, native_database):
    cases = [
        ({"debug": {"error": "连接暂不可用"}}, "连接暂不可用"),
        ({"error": "已有说明", "debug": {"error": "旧说明"}}, "已有说明"),
        ({"error": "\u001c ", "debug": {"error": "  保留空格  "}}, "  保留空格  "),
        ({"debug": []}, None),
        *[
            ({"debug": {"error": value}}, None)
            for value in (
                "RuntimeError: offline",
                "错误原因: RuntimeError: offline",
                "Traceback ...",
                "ErrorID=1",
                "return_code=-1",
                "CTP.ERROR",
                "-１２",
                "gm_authentication_error",
                " \u001c ",
            )
        ],
    ]
    for index, (details, _) in enumerate(cases):
        _event(native_database, details, event_uid=f"e-{index}", channel="\u001c MY-PLUGIN-ΟΣ \u001f")
    for enum, field in (
        (ExecutionEventType, "event_type"),
        (ExecutionEventStatus, "status"),
        (ExecutionReasonFamily, "reason_family"),
    ):
        for value in enum:
            _event(native_database, {"business": [False, 0, None]}, event_uid=f"{field}-{value}", **{field: value})
    runtime_rows(native_database, "ALTER TABLE execution_event ADD COLUMN internal_only TEXT DEFAULT 'private'")
    before = _audit_rows(native_database, "execution_event")
    expected = [ExecutionEventPublic.model_validate(row).model_dump(mode="json") for row in before]
    for item, (_, error) in zip(expected, cases, strict=False):
        if error is not None:
            item["details"]["error"] = error
    response = client.get("/account/executions/e1/events")
    assert response.status_code == 200, response.text
    assert response.json() == {"data": expected, "count": len(before)}
    assert all("internal_only" not in row for row in response.json()["data"])
    assert _audit_rows(native_database, "execution_event") == before
    assert client.get("/account/executions/missing/events").json() == {"data": [], "count": 0}


def test_artifact_projection_and_summary_normalization_match_existing_reader(client, native_database):
    summary = {
        "outcome": "error",
        "outcome_reason": "RuntimeError: offline",
        "business": [False, 0, None],
        "symbol_results": {
            "a": {"outcome": "blocked", "reason_code": "CTP.SESSION.CLOSED"},
            "b": {"outcome": "not_reached", "final_volume": 0},
            "c": {"task_status": "TERMINATED"},
        },
        "reconciliation": {"symbols": {"raw": {"outcome": "error", "reason_code": None}}},
    }
    for kind in ExecutionArtifactType:
        native_store_call(
            native_database,
            "append_execution_artifact",
            ExecutionArtifact(
                execution_id="e1",
                artifact_type=kind,
                content=summary,
            ).model_dump(mode="json"),
        )
    runtime_rows(native_database, "ALTER TABLE execution_artifact ADD COLUMN internal_only TEXT DEFAULT 'private'")
    before = _audit_rows(native_database, "execution_artifact")
    expected = [ExecutionArtifactPublic.model_validate(row).model_dump(mode="json") for row in before]
    for item in expected:
        if item["artifact_type"] == "execution_summary":
            item["content"] = normalize_legacy_result(item["content"])
    response = client.get("/account/executions/e1/artifacts")
    assert response.status_code == 200, response.text
    assert response.json() == {"data": expected, "count": len(before)}
    assert _audit_rows(native_database, "execution_artifact") == before
    assert client.get("/account/executions/missing/artifacts").json() == {"data": [], "count": 0}


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("execution_event", "event_type", "future_unknown"),
        ("execution_event", "status", "future_unknown"),
        ("execution_event", "reason_family", "future_unknown"),
        ("execution_event", "channel", " \u001c "),
        ("execution_event", "details", "[]"),
        ("execution_event", "seq", "invalid"),
        ("execution_event", "account_id", 1.5),
        ("execution_artifact", "artifact_type", "future_unknown"),
        ("execution_artifact", "content", "[]"),
        ("execution_artifact", "schema_version", 1.5),
    ],
)
def test_invalid_audit_rows_still_fail_validation(client, native_database, table, column, value):
    _event(native_database, {})
    native_store_call(
        native_database,
        "append_execution_artifact",
        ExecutionArtifact(
            execution_id="e1",
            artifact_type="target_snapshot",
        ).model_dump(mode="json"),
    )
    runtime_rows(native_database, f"UPDATE {table} SET {column}=?", (value,))
    suffix = "events" if table == "execution_event" else "artifacts"
    assert client.get(f"/account/executions/e1/{suffix}").status_code == 500


@pytest.mark.parametrize("status", ["pending", "synchronized", "failed"])
def test_runtime_sync_keeps_only_public_fields_and_does_not_load_account(client, native_database, monkeypatch, status):
    native_store_call(
        native_database,
        "account_create",
        {
            "account": build_account(portfolio_id=None).model_dump(mode="json"),
            "now": "2026-10-24",
        },
    )
    runtime_rows(
        native_database,
        "UPDATE accountruntimesync SET revision=0,status=?,attempts=0,reset_worker=1,create_request_key='private',last_error='失败原因',last_attempt_at='attempt',synchronized_at='synced' WHERE account_id=1",
        (status,),
    )
    raw = dict(runtime_rows(native_database, "SELECT * FROM accountruntimesync")[0])
    monkeypatch.setattr(_core, "account_get", Mock(side_effect=AssertionError("must not load full account")))
    response = client.get("/account/1/runtime-sync")
    assert response.status_code == 200, response.text
    assert response.json() == AccountRuntimeSyncPublic.model_validate(raw).model_dump(mode="json")
    assert dict(runtime_rows(native_database, "SELECT * FROM accountruntimesync")[0]) == raw


def test_runtime_sync_missing_account_and_initial_sync_keep_status_and_detail(client, native_database):
    response = client.get("/account/1/runtime-sync")
    assert response.status_code == 404 and response.json() == {"detail": "账户不存在"}
    native_store_call(
        native_database,
        "account_create",
        {
            "account": build_account(portfolio_id=None).model_dump(mode="json"),
            "now": "2026-10-24",
        },
    )
    runtime_rows(native_database, "DELETE FROM accountruntimesync")
    response = client.get("/account/1/runtime-sync")
    assert response.status_code == 202 and response.json() == {"detail": "账户运行态尚待首次同步"}


@pytest.mark.parametrize("ok", [True, False, None])
def test_system_notification_empty_and_saved_results_match_public_model(client, native_database, ok):
    response = client.get("/init/execution-alert/result")
    assert response.status_code == 200
    assert response.json() == SystemNotificationStatePublic().model_dump(mode="json")
    result = SystemNotificationStatePublic(
        finished_at="2026-10-24T01:02:03+08:00",
        mode="function",
        event_type="execution_timeout",
        execution_id="e1",
        account_id=1,
        ok=ok,
        error="发送失败" if ok is False else None,
    ).model_dump(mode="json")
    native_store_call(native_database, "notifications_record_system", result)
    assert client.get("/init/execution-alert/result").json() == result
    assert native_store_call(native_database, "notifications_get_system", {}) == result


def test_unconfigured_system_notification_does_not_query_database(client, monkeypatch):
    monkeypatch.setattr(init_routes, "is_configured", lambda: False)
    query = Mock(side_effect=AssertionError("first setup must not query database"))
    monkeypatch.setattr(_core, "notifications_get_system_json", query)
    response = client.get("/init/execution-alert/result")
    assert response.status_code == 409 and response.json() == {"detail": "系统尚未完成初始化"}
    query.assert_not_called()
