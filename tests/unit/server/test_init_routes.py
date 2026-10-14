"""首启初始化向导路由测试。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from loguru import logger

import axile.common.config as cfg
from axile.server.api.routes import init as init_module
from axile.server.app import validation_exception_handler


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for name in ("exe_err_feishu_key", "system_execution_notification_mode", "system_execution_notification_code"):
        monkeypatch.setattr(cfg.settings, name, getattr(cfg.settings, name))
    app = FastAPI()
    app.include_router(init_module.router, prefix="/api/v1")
    return TestClient(app)


class _FakeResp:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def json(self) -> Any:
        return self._payload


class _FakeSession:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def post(self, _url: str, json: Any = None) -> _FakeResp:  # noqa: A002 - 匹配 aiohttp 签名
        return _FakeResp(self._payload)


def _patch_aiohttp_response(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    monkeypatch.setattr(
        init_module.aiohttp,
        "ClientSession",
        lambda *_args, **_kwargs: _FakeSession(payload),
    )


def test_init_status_never_returns_credential_values(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_uri = "postgresql+asyncpg://user:unique-db-password@db.example/axile"
    sentinel_key = "unique-feishu-webhook-key"
    monkeypatch.setattr(cfg.settings, "sqlalchemy_database_uri", sentinel_uri)
    monkeypatch.setattr(cfg.settings, "exe_err_feishu_key", sentinel_key)

    response = client.get("/api/v1/init/status")

    assert response.status_code == 200
    assert set(response.json()["values"]) == {
        "sqlalchemy_database_configured",
        "exe_err_feishu_configured",
        "system_execution_notification_mode",
        "system_execution_notification_code",
        "environment",
        "app_log_dir",
        "axile_log_rotation",
        "algorithm_modules",
        "algorithm_directories",
    }
    assert sentinel_uri not in response.text
    assert sentinel_key not in response.text


def test_system_notification_function_can_be_saved_without_feishu_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """系统只保存一份当前源码，并在保存后立即生效。"""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text('exe_err_feishu_key = ""\n', encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml_path)
    response = client.patch(
        "/api/v1/init/execution-alert",
        json={
            "exe_err_feishu_key": "",
            "system_execution_notification_mode": "function",
            "system_execution_notification_code": "def notify(context):\n    pass",
        },
    )
    assert response.status_code == 200
    assert cfg.settings.system_execution_notification_mode == "function"
    assert "def notify(context)" in toml_path.read_text(encoding="utf-8")


def test_system_notification_function_sample_run(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """试跑使用样例异常并返回函数错误。"""
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        init_module,
        "run_notification_function",
        lambda _code, context, **_kwargs: (
            captured.append(context) or type("Result", (), {"ok": True, "error_line": None})()
        ),
    )
    response = client.post("/api/v1/init/execution-alert/function/test", json={"code": "def notify(context): pass"})
    assert response.json()["ok"] is True
    assert captured[0]["is_test"] is True


def test_validation_error_preserves_complete_input_and_context() -> None:
    sentinel = "unique-validation-secret"
    request = Request({"type": "http", "method": "POST", "path": "/api/v1/init/save", "headers": []})
    exc = RequestValidationError(
        [
            {
                "type": "string_type",
                "loc": ("body", "exe_err_feishu_key"),
                "msg": "Input should be a valid string",
                "input": sentinel,
                "ctx": {"error": ValueError("token=original-error"), "value": float("nan")},
            }
        ]
    )
    captured: list[str] = []
    sink_id = logger.add(captured.append, format="{message}")
    try:
        response = asyncio.run(validation_exception_handler(request, exc))
    finally:
        logger.remove(sink_id)

    assert '"value":"nan"' in response.body.decode()
    assert "token=original-error" in response.body.decode()
    assert sentinel in response.body.decode()
    assert sentinel in "\n".join(captured)


def test_test_db_success(client: TestClient, tmp_path: Path) -> None:
    uri = f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}"

    response = client.post("/api/v1/init/test-db", json={"uri": uri})

    assert response.json()["ok"] is True


def test_test_db_failure(client: TestClient) -> None:
    response = client.post(
        "/api/v1/init/test-db",
        json={"uri": "sqlite+aiosqlite:////nonexistent_dir_xyz/probe.db"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"StatusMessage": "success"},
        {"code": 0, "msg": "success"},
    ],
)
def test_test_feishu_accepts_success_responses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    _patch_aiohttp_response(monkeypatch, payload)

    response = client.post("/api/v1/init/test-feishu", json={"key": "abc"})

    assert response.json()["ok"] is True


def test_test_feishu_reports_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_aiohttp_response(monkeypatch, {"code": 19001, "msg": "invalid access token"})

    response = client.post("/api/v1/init/test-feishu", json={"key": "bad"})

    assert response.json()["ok"] is False
    assert "invalid access token" in response.json()["message"]


def test_test_feishu_rejects_empty_key(client: TestClient) -> None:
    response = client.post("/api/v1/init/test-feishu", json={"key": "  "})

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_update_execution_alert_persists_and_updates_runtime_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        '# managed\nsqlalchemy_database_uri = "sqlite+aiosqlite:///./keep.db"\nexe_err_feishu_key = "old-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml_path)
    monkeypatch.setattr(cfg.settings, "exe_err_feishu_key", "old-key")
    monkeypatch.setattr(init_module, "_restart_process", lambda: pytest.fail("热更新不应触发重启"))

    response = client.patch(
        "/api/v1/init/execution-alert",
        json={"exe_err_feishu_key": "new-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "执行告警配置已保存并立即生效。"}
    written = toml_path.read_text(encoding="utf-8")
    assert "# managed" in written
    assert 'sqlalchemy_database_uri = "sqlite+aiosqlite:///./keep.db"' in written
    assert 'exe_err_feishu_key = "new-key"' in written
    assert cfg.settings.exe_err_feishu_key == "new-key"


def test_update_execution_alert_accepts_empty_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text('exe_err_feishu_key = "old-key"\n', encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml_path)
    monkeypatch.setattr(cfg.settings, "exe_err_feishu_key", "old-key")

    response = client.patch(
        "/api/v1/init/execution-alert",
        json={"exe_err_feishu_key": ""},
    )

    assert response.status_code == 200
    assert 'exe_err_feishu_key = ""' in toml_path.read_text(encoding="utf-8")
    assert cfg.settings.exe_err_feishu_key == ""


def test_update_execution_alert_write_failure_keeps_runtime_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text('exe_err_feishu_key = "old-key"\n', encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml_path)
    monkeypatch.setattr(cfg.settings, "exe_err_feishu_key", "old-key")

    def _raise_write_error(_values: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(init_module, "update_config_toml_values", _raise_write_error)

    response = client.patch(
        "/api/v1/init/execution-alert",
        json={"exe_err_feishu_key": "new-key"},
    )

    assert response.status_code == 500
    assert cfg.settings.exe_err_feishu_key == "old-key"


def test_update_execution_alert_requires_initialized_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", tmp_path / "missing.toml")

    response = client.patch(
        "/api/v1/init/execution-alert",
        json={"exe_err_feishu_key": "new-key"},
    )

    assert response.status_code == 409


def test_init_save_rejects_bad_dsn(client: TestClient) -> None:
    response = client.post(
        "/api/v1/init/save",
        json={"sqlalchemy_database_uri": "not-a-valid-dsn"},
    )

    assert response.status_code == 422


def test_init_save_writes_config_and_schedules_restart(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml_path)
    restarted: dict[str, bool] = {}
    monkeypatch.setattr(init_module, "_restart_process", lambda: restarted.setdefault("done", True))

    response = client.post(
        "/api/v1/init/save",
        json={
            "sqlalchemy_database_uri": "sqlite+aiosqlite:///./axile.db",
            "exe_err_feishu_key": "bot-key-123",
            "algorithm_modules": ["pkg.a"],
            "algorithm_directories": ["./my_algos"],
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    written = toml_path.read_text(encoding="utf-8")
    assert 'exe_err_feishu_key = "bot-key-123"' in written
    assert "tushare_token" not in written
    assert "./my_algos" in written
    assert restarted.get("done") is True


def test_calendar_initialization_field_is_removed(client: TestClient) -> None:
    response = client.post(
        "/api/v1/init/save",
        json={"sqlalchemy_database_uri": "sqlite+aiosqlite:///./axile.db", "trading_calendars": []},
    )

    assert response.status_code == 422


def test_advanced_save_preserves_omitted_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """只改日志目录时保留现有数据库与告警凭证。"""
    toml_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", toml_path)
    monkeypatch.setattr(cfg.settings, "sqlalchemy_database_uri", "sqlite+aiosqlite:///custom.db")
    monkeypatch.setattr(cfg.settings, "exe_err_feishu_key", "existing-key")
    monkeypatch.setattr(init_module, "_restart_process", lambda: None)

    response = client.post("/api/v1/init/save", json={"app_log_dir": "./new-logs"})

    assert response.status_code == 200
    written = toml_path.read_text()
    assert 'sqlalchemy_database_uri = "sqlite+aiosqlite:///custom.db"' in written
    assert 'exe_err_feishu_key = "existing-key"' in written
    assert 'app_log_dir = "./new-logs"' in written
