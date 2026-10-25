"""系统告警配置、试跑与真实结果的边界契约。"""

import asyncio
import io
import json
import sqlite3
import urllib.request
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import axile.common.config as cfg
from axile.common.default_system_notification import DEFAULT_SYSTEM_NOTIFICATION_CODE
from axile.common.notification_function import NotificationFunctionResult
from axile.server import error_notifications
from axile.server._native import _core
from axile.server.api.routes import init as init_module
from axile.server.core import db
from axile.server.db.models.system_notification import SystemNotificationStatePublic
from axile.server.execution import notification_state
from tests.conftest import initialize_native_database, native_store_call


@pytest.fixture
def client(monkeypatch, tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('exe_err_feishu_key = "saved-key"\n')
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml)
    monkeypatch.setattr(cfg.settings, "exe_err_feishu_key", "saved-key")
    monkeypatch.setattr(cfg.settings, "system_execution_notification_mode", "function")
    monkeypatch.setattr(cfg.settings, "system_execution_notification_code", "def notify(context):\n    pass\n")
    app = FastAPI()
    app.include_router(init_module.router, prefix="/api/v1")
    return TestClient(app)


def test_function_mode_requires_valid_code_but_default_keeps_inactive_draft(client):
    response = client.patch(
        "/api/v1/init/execution-alert",
        json={
            "system_execution_notification_mode": "function",
            "system_execution_notification_code": "",
        },
    )
    assert response.status_code == 422
    response = client.patch(
        "/api/v1/init/execution-alert",
        json={
            "system_execution_notification_mode": "default",
            "system_execution_notification_code": "unfinished",
        },
    )
    assert response.status_code == 200
    assert cfg.settings.system_execution_notification_code == "unfinished"


def test_trial_reports_real_source_error(client):
    response = client.post(
        "/api/v1/init/execution-alert/function/test",
        json={
            "code": "async def notify(context):\n    raise RuntimeError('source error')",
        },
    )
    assert response.json()["ok"] is False
    assert response.json()["error_line"] == 2
    assert "source error" in response.json()["message"]


def test_quota_trial_exposes_warning_data_without_error_or_persisting_result(client, monkeypatch):
    results = []
    monkeypatch.setattr(error_notifications, "record_system_notification_result", results.append)
    response = client.post(
        "/api/v1/init/execution-alert/function/test",
        json={
            "event_type": "account_control.warning",
            "code": "def notify(context):\n    assert context['event_type'] == 'account_control.warning'\n    assert context['error'] is None\n    assert context['quota']['used'] == 400\n    assert context['quota']['remaining'] == 100\n    assert context['is_test'] is True\n",
        },
    )
    assert response.json()["ok"] is True
    assert results == []


def test_default_function_template_formats_quota_card_without_exception(monkeypatch):
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(b'{"code": 0}')

    monkeypatch.setenv("AXILE_SYSTEM_FEISHU_KEY", "test-only-key")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    namespace = {}
    exec(DEFAULT_SYSTEM_NOTIFICATION_CODE, namespace)
    namespace["notify"](
        {
            "event_type": "account_control.warning",
            "account": {"id": 1, "name": "额度测试"},
            "occurred_at": "2026-10-15T09:30:00+08:00",
            "execution_id": "exec",
            "error": None,
            "is_test": False,
            "quota": {
                "operation": "cancel_order",
                "used": 320,
                "limit": 400,
                "remaining": 80,
                "warning_percent": 80,
                "threshold_count": 320,
                "control_date": "2026-10-15",
                "timezone": "Asia/Shanghai",
            },
        }
    )
    card = requests[0]["card"]
    assert card["header"]["title"]["content"] == "账户额度预警"
    assert "撤单" in card["elements"][0]["content"]
    assert "320 / 400" in card["elements"][0]["content"]


def test_default_trial_can_use_saved_key(client, monkeypatch):
    captured = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"code": 0}

    class Session(Response):
        def post(self, url, json):
            captured.append(url)
            return Response()

    monkeypatch.setattr(init_module.aiohttp, "ClientSession", lambda **kwargs: Session())
    assert client.post("/api/v1/init/test-feishu", json={"key": None}).json()["ok"]
    assert captured[0].endswith("/saved-key")
    assert not client.post("/api/v1/init/test-feishu", json={"key": ""}).json()["ok"]


def test_latest_result_is_persistent_and_older_completions_cannot_overwrite(client, monkeypatch, native_database):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    assert client.get("/api/v1/init/execution-alert/result").json()["ok"] is None
    latest = SystemNotificationStatePublic(
        finished_at="2026-09-30T10:02:00+08:00",
        mode="function",
        event_type="execution_timeout",
        execution_id="exec-new",
        ok=False,
        error="timeout",
    )
    notification_state.record_system_notification_result(latest)
    notification_state.record_system_notification_result(
        SystemNotificationStatePublic(finished_at="2026-09-30T10:01:00+08:00", ok=True)
    )
    assert client.get("/api/v1/init/execution-alert/result").json() == latest.model_dump(mode="json")


def test_state_write_failure_does_not_escape_notification(client, monkeypatch, native_database):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    calls = []

    def notify(*args, **kwargs):
        calls.append(args)
        return NotificationFunctionResult(True)

    monkeypatch.setattr(error_notifications, "run_notification_function", notify)
    with closing(sqlite3.connect(native_database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_system_result BEFORE INSERT ON system_notification_state "
            "BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END"
        )
    with pytest.raises(_core.NativeError, match="disk unavailable"):
        notification_state.record_system_notification_result(
            SystemNotificationStatePublic(finished_at="2026-10-23T10:00:00", ok=True)
        )
    asyncio.run(error_notifications.send_feishu_error(RuntimeError("error"), None, "saved-key"))
    assert len(calls) == 1
    assert client.get("/api/v1/init/execution-alert/result").json()["ok"] is None


def test_system_notification_migration_roundtrip(tmp_path):
    path = tmp_path / "migration.db"
    initialize_native_database(path, revision="0021")
    initialize_native_database(path, revision="0022")
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='system_notification_state'"
        ).fetchone()
        connection.execute("INSERT INTO system_notification_state (id, ok) VALUES (1, 0)")
    native_store_call(path, "migration_downgrade", {"public": "0021"})
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='system_notification_state'"
            ).fetchone()
            is None
        )
