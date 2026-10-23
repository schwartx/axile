"""首启初始化向导路由测试。"""

import asyncio
import sqlite3
from pathlib import Path

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
    with sqlite3.connect(tmp_path / "probe.db") as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.parametrize("uri", ["sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"])
def test_test_db_accepts_memory_database(client: TestClient, uri: str) -> None:
    assert client.post("/api/v1/init/test-db", json={"uri": uri}).json()["ok"] is True


def test_test_db_uses_supplied_read_only_uri(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "probe.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE retained(value TEXT)")
    uri = f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true"
    assert client.post("/api/v1/init/test-db", json={"uri": uri}).json()["ok"] is True
    path.unlink()
    assert client.post("/api/v1/init/test-db", json={"uri": uri}).json()["ok"] is False
    assert not path.exists()


def test_test_db_failure_does_not_create_missing_directory(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "missing" / "probe.db"
    response = client.post("/api/v1/init/test-db", json={"uri": f"sqlite+aiosqlite:///{path}"})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "uri", ["not-a-valid-dsn", "sqlite+aiosqlite://host/probe.db", "sqlite+aiosqlite:///a?uri=bad"]
)
def test_test_db_rejects_invalid_uri(client: TestClient, uri: str) -> None:
    response = client.post("/api/v1/init/test-db", json={"uri": uri})
    assert response.status_code == 200
    assert response.json()["ok"] is False


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


@pytest.mark.parametrize("existing", [False, True])
def test_time_check_first_run_marker_only_for_new_install(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: bool
) -> None:
    """升级保存配置不补建首次待办；初次配置明确标记自动检查。"""
    path = tmp_path / "config.toml"
    if existing:
        path.write_text('environment = "local"\n', encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_TOML_PATH", path)
    monkeypatch.setattr(cfg.settings, "time_check_first_run", False)
    monkeypatch.setattr(init_module, "_restart_process", lambda: None)
    response = client.post("/api/v1/init/save", json={"sqlalchemy_database_uri": "sqlite+aiosqlite:///./axile.db"})
    assert response.status_code == 200
    expected = "false" if existing else "true"
    assert f"time_check_first_run = {expected}" in path.read_text(encoding="utf-8")
