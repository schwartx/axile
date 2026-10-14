"""连接自检仅显式启动；测试用假 worker，禁止访问真实柜台。"""

import asyncio
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import connection_validation as routes
from axile.server.connection_validation import ValidationService

CONFIG = dict(
    broker_id="9999",
    investor_id="test-account",
    password="private-password",
    td_front="tcp://test.invalid:1234",
    md_front="tcp://test.invalid:5678",
    app_id="test-app",
    auth_code="private-auth",
)


@pytest.fixture
def service():
    return ValidationService()


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(routes, "service", service)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(routes.account_router, prefix="/api/v1")
    with TestClient(app) as client:
        yield client
        client.portal.call(service.close)


def test_read_does_not_launch_worker(client, service, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(service, "_start", spawn)
    assert client.get("/api/v1/channel-validations/unknown").status_code == 404
    spawn.assert_not_called()
    assert not service.jobs


def test_unsupported_channel_has_no_network_side_effect(client, service, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(service, "_start", spawn)
    response = client.post("/api/v1/channel-validations", json={"channel": "gm", "account_config": CONFIG})
    assert response.status_code == 422
    spawn.assert_not_called()


def test_single_active_validation_and_cancel(client, service, monkeypatch):
    monkeypatch.setattr(service, "_start", lambda *_args: None)
    body = {"channel": "ctp", "account_config": CONFIG}
    first = client.post("/api/v1/channel-validations", json=body).json()
    assert client.post("/api/v1/channel-validations", json=body).status_code == 409
    cancelled = client.delete(f"/api/v1/channel-validations/{first['validation_id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post("/api/v1/channel-validations", json=body).status_code == 202


def test_shutdown_cancels_active_jobs(service, monkeypatch):
    monkeypatch.setattr(service, "_start", lambda *_args: None)
    job = service.create("ctp", CONFIG)
    asyncio.run(service.close())
    assert job.status == "cancelled"


def test_details_reach_stage_snapshot(service, monkeypatch):
    monkeypatch.setattr(service, "_start", lambda *_args: None)
    job = service.create("ctp", CONFIG)
    service.apply_event(job, {"step": "trader_login", "status": "running"})
    original = "CTP: 不合法的登录\n" + "柜台完整错误信息" * 1000 + "\nprivate-password private-auth test-account"
    service.apply_event(
        job,
        {
            "result": "failed",
            "reason_code": "CONNECTION.BROKER_REJECTED",
            "message": "柜台拒绝登录",
            "error_id": 3,
            "details": original,
        },
    )
    assert job.steps[2].error.details == original
    assert job.error.error_id == 3
    assert job.steps[2].error.message == "柜台拒绝登录"


def test_no_new_validation_until_native_cleanup_finishes(service, monkeypatch):
    from axile.server.connection_validation import ValidationRejected

    async def scenario():
        cleanup = asyncio.Event()
        started = asyncio.Event()

        async def fake_run(job, _config):
            service.fail(job, "OFFLINE", "模拟失败")
            started.set()
            await cleanup.wait()

        monkeypatch.setattr(service, "_run", fake_run)
        job = service.create("ctp", CONFIG)
        await started.wait()
        with pytest.raises(ValidationRejected) as rejected:
            service.create("ctp", CONFIG)
        assert rejected.value.status == 409
        cleanup.set()
        await service.close()
        assert not service.tasks
        assert job.status == "failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("broker_error", [True, False])
def test_worker_returns_exact_error_original(monkeypatch, broker_error):
    from types import SimpleNamespace

    from axile import channels
    from axile.server import connection_validation_worker as worker

    original = "错误原文\naccount=original-account; value=unchanged"
    failure = RuntimeError(original)
    if broker_error:
        failure.broker_message = original
        failure.error_id = 3
    validate = Mock(side_effect=failure)
    plugin = SimpleNamespace(
        account_config_model=SimpleNamespace(model_validate=lambda config: config), validate_connection=validate
    )
    monkeypatch.setattr(channels, "get_channel", lambda _channel: plugin)
    monkeypatch.setattr(worker.logger, "remove", lambda: None)
    monkeypatch.setattr(worker.os, "dup2", lambda *_args: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(worker.tempfile, "tempdir", None)
    pipe = Mock()
    pipe.recv.return_value = {"temp_dir": "/tmp", "channel": "ctp", "account_config": CONFIG}
    worker.validation_worker(pipe)
    result = pipe.send.call_args.args[0]
    assert result["details"] == original
    assert result["result"] == "failed"
    assert "details" not in result["diagnostic"]
    pipe.close.assert_called_once()


@pytest.mark.parametrize("rows", [[], [["000123", 0, False, None], ["000124", 1.25, True, ""]]])
def test_table_snapshot_is_lazy_and_survives_later_failure(client, service, monkeypatch, rows):
    spawn = Mock()
    monkeypatch.setattr(service, "_start", spawn)
    job = service.create("ctp", CONFIG)
    columns = [
        {"key": key, "label": key, "type": kind, "filterable": False}
        for key, kind in (("id", "text"), ("quantity", "number"), ("flag", "text"), ("missing", "text"))
    ]
    service.apply_event(
        job,
        {
            "step": "query_orders",
            "status": "passed",
            "details": {
                "kind": "table",
                "title": "记录",
                "summary": f"{len(rows)} 条",
                "columns": columns,
                "rows": rows,
            },
        },
    )
    service.apply_event(job, {"step": "query_trades", "status": "running"})
    service.fail(job, "CONNECTION.STAGE_TIMEOUT", "成交查询超时")
    snapshot = client.get(f"/api/v1/channel-validations/{job.validation_id}").json()
    details = next(step for step in snapshot["steps"] if step["id"] == "query_orders")["details"]
    assert details["row_count"] == len(rows)
    assert "rows" not in details
    response = client.get(f"/api/v1/channel-validations/{job.validation_id}/steps/query_orders/table")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["rows"] == rows
    assert response.json()["columns"] == columns
    assert client.get(f"/api/v1/channel-validations/{job.validation_id}/steps/query_trades/table").status_code == 404
    spawn.assert_called_once()
    service.expires[job.validation_id] = 0
    assert client.get(f"/api/v1/channel-validations/{job.validation_id}/steps/query_orders/table").status_code == 404
    assert job.validation_id not in service.tables
