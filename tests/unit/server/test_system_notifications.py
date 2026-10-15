"""系统告警配置、试跑与真实结果的边界契约。"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient

import axile.common.config as cfg
from axile.common.notification_function import NotificationFunctionResult
from axile.server import error_notifications
from axile.server.api.routes import init as init_module
from axile.server.db.models.system_notification import SystemNotificationState, SystemNotificationStatePublic
from axile.server.execution import notification_state


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


def test_latest_result_is_persistent_and_older_completions_cannot_overwrite(monkeypatch, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'results.db'}")
    SystemNotificationState.__table__.create(engine)
    monkeypatch.setattr(notification_state, "_notification_engine", lambda: engine)
    assert notification_state.get_system_notification_result().ok is None
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
    engine.dispose()
    assert notification_state.get_system_notification_result() == latest


def test_state_write_failure_does_not_escape_notification(client, monkeypatch):
    monkeypatch.setattr(
        error_notifications, "run_notification_function", lambda *args, **kwargs: NotificationFunctionResult(True)
    )

    def fail(_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(error_notifications, "record_system_notification_result", fail)
    asyncio.run(error_notifications.send_feishu_error(RuntimeError("error"), None, "saved-key"))


def test_system_notification_migration_roundtrip(tmp_path):
    path = Path(__file__).parents[3] / "axile/server/alembic/versions/0022_system_notification_state.py"
    spec = importlib.util.spec_from_file_location("system_state_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert "system_notification_state" in sa.inspect(connection).get_table_names()
        connection.execute(sa.text("INSERT INTO system_notification_state (id, ok) VALUES (1, 0)"))
        module.downgrade()
        assert "system_notification_state" not in sa.inspect(connection).get_table_names()
