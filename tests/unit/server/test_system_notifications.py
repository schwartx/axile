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
from axile.common.default_system_notification import DEFAULT_SYSTEM_NOTIFICATION_CODE
from axile.common.notification_function import NotificationFunctionResult, run_notification_function
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


def test_switching_modes_preserves_inactive_configuration(client):
    code = cfg.settings.system_execution_notification_code
    response = client.patch("/api/v1/init/execution-alert", json={"system_execution_notification_mode": "default"})
    assert response.status_code == 200
    assert cfg.settings.system_execution_notification_code == code
    assert cfg.settings.exe_err_feishu_key == "saved-key"
    assert "def notify(context)" in cfg.CONFIG_TOML_PATH.read_text()
    response = client.patch("/api/v1/init/execution-alert", json={"system_execution_notification_mode": "function"})
    assert response.status_code == 200
    assert cfg.settings.system_execution_notification_code == code


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


@pytest.mark.parametrize("key, expected", [(None, "saved-key"), ("draft-key", "draft-key"), ("", "")])
@pytest.mark.parametrize("event_type", ["execution_error", "execution_timeout"])
def test_trial_uses_selected_credentials_and_sample_without_persisting(client, monkeypatch, key, expected, event_type):
    before = cfg.CONFIG_TOML_PATH.read_text()
    captured = []

    def run(code, context, **kwargs):
        captured.append((context, kwargs))
        return NotificationFunctionResult(False, "boom", 2, "Traceback\n  child.py:2\nboom")

    monkeypatch.setattr(init_module, "run_notification_function", run)
    monkeypatch.setattr(
        notification_state, "record_system_notification_result", lambda _: pytest.fail("试跑不能写真实结果")
    )
    response = client.post(
        "/api/v1/init/execution-alert/function/test",
        json={
            "code": "def notify(context):\n    raise RuntimeError('boom')",
            "key": key,
            "event_type": event_type,
        },
    )
    assert response.json() == {
        "ok": False,
        "message": "boom",
        "error_line": 2,
        "traceback": "Traceback\n  child.py:2\nboom",
    }
    context, kwargs = captured[0]
    assert context["event_type"] == event_type
    assert context["is_test"] is True
    assert context["execution_id"] is None
    assert context["occurred_at"] != "sample"
    assert kwargs["system_feishu_key"] == expected
    assert cfg.CONFIG_TOML_PATH.read_text() == before
    assert cfg.settings.exe_err_feishu_key == "saved-key"


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


def test_advanced_save_preserves_notification_mode_and_code(client, monkeypatch):
    monkeypatch.setattr(init_module, "_restart_process", lambda: None)
    response = client.post("/api/v1/init/save", json={"app_log_dir": "./changed"})
    assert response.status_code == 200
    assert 'system_execution_notification_mode = "function"' in cfg.CONFIG_TOML_PATH.read_text()
    assert "def notify(context)" in cfg.CONFIG_TOML_PATH.read_text()


def test_template_is_valid_and_contains_no_saved_credential(client):
    response = client.get("/api/v1/init/execution-alert/function/default")
    assert response.json()["code"] == DEFAULT_SYSTEM_NOTIFICATION_CODE
    assert "saved-key" not in response.text
    # 替换网络发送以执行模板本身，确保示例的真实结构与试跑上下文匹配。
    code = (
        DEFAULT_SYSTEM_NOTIFICATION_CODE
        + """
class Response:
    def __enter__(self):
        import io
        return io.BytesIO(b'{"code": 0}')
    def __exit__(self, *args):
        return False
def urlopen(request, timeout):
    assert request.full_url.endswith('/template-key')
    assert b'execution_timeout' in request.data
    return Response()
"""
    )
    context = error_notifications.build_system_notification_context(
        RuntimeError("timeout"), None, event_type="execution_timeout", is_test=True
    )
    assert run_notification_function(code, context, system_feishu_key="template-key").ok


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


@pytest.mark.parametrize("mode, ok", [("default", True), ("default", False), ("function", True), ("function", False)])
def test_real_sends_record_success_and_failure(client, monkeypatch, mode, ok):
    monkeypatch.setattr(cfg.settings, "system_execution_notification_mode", mode)
    captured = []
    monkeypatch.setattr(error_notifications, "record_system_notification_result", captured.append)
    monkeypatch.setattr(
        error_notifications,
        "run_notification_function",
        lambda *args, **kwargs: NotificationFunctionResult(ok, None if ok else "function failed"),
    )

    async def ip():
        return ""

    def push(*args):
        if not ok:
            raise RuntimeError("sender failed")

    monkeypatch.setattr(error_notifications, "get_external_ip", ip)
    monkeypatch.setattr(error_notifications, "push_feishu_card", push)
    asyncio.run(
        error_notifications.send_feishu_error(
            RuntimeError("execution failed"), None, "saved-key", execution_id="exec", event_type="execution_timeout"
        )
    )
    assert len(captured) == 1
    state = captured[0]
    assert state.mode == mode
    assert state.ok is ok
    assert state.execution_id == "exec"
    assert state.event_type == "execution_timeout"
    assert bool(state.error) is not ok


def test_unconfigured_default_does_not_overwrite_real_result(client, monkeypatch):
    monkeypatch.setattr(cfg.settings, "system_execution_notification_mode", "default")
    monkeypatch.setattr(error_notifications, "record_system_notification_result", lambda _: pytest.fail("关闭时不写入"))
    asyncio.run(error_notifications.send_feishu_error(RuntimeError("error"), None, ""))


def test_state_write_failure_does_not_escape_notification(client, monkeypatch):
    monkeypatch.setattr(
        error_notifications, "run_notification_function", lambda *args, **kwargs: NotificationFunctionResult(True)
    )

    def fail(_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(error_notifications, "record_system_notification_result", fail)
    asyncio.run(error_notifications.send_feishu_error(RuntimeError("error"), None, "saved-key"))


def test_result_route_requires_initialization(client, monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", tmp_path / "missing.toml")
    monkeypatch.setattr(init_module, "get_system_notification_result", lambda: pytest.fail("首启不读业务库"))
    assert client.get("/api/v1/init/execution-alert/result").status_code == 409


def test_result_route_exposes_real_summary(client, monkeypatch):
    monkeypatch.setattr(
        init_module,
        "get_system_notification_result",
        lambda: SystemNotificationStatePublic(ok=False, error="timeout", execution_id="exec"),
    )
    assert client.get("/api/v1/init/execution-alert/result").json()["execution_id"] == "exec"


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
