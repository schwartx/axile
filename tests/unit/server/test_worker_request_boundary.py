"""Worker 请求解码的校验时点和通知快照边界。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from axile.server.execution.worker_backend import worker
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
from tests.unit.server._execution_test_support import FakeExecutor, build_account


@pytest.mark.parametrize("command", ["execute_trade", "empty_positions"])
@pytest.mark.parametrize("invalid_account", [False, True])
def test_worker_request_validation_precedes_runtime(monkeypatch, command, invalid_account):
    """坏账户或必需审计字段缺失时，不激活终止控制器或进入执行器准备。"""
    account = build_account().model_dump(mode="json")
    payload = (
        {"standard_input": {"account_config": account["account_config"], "curr_target": {}}, "trigger_source": "manual"}
        if command == "execute_trade"
        else {"empty_kwargs": {"algorithm": {}, "extra": {}}, "audit_input": {}}
    )
    request = WorkerBackendRequest(
        request_id="invalid-input",
        command=command,
        account_payload={} if invalid_account else account,
        execution_id="validation",
        payload=payload,
    )

    def unexpected_activation(*_args):
        pytest.fail("请求校验失败前不应激活运行态")

    monkeypatch.setattr(worker, "_activate_worker_termination", unexpected_activation)
    with pytest.raises(ValidationError if invalid_account else KeyError):
        worker._handle_worker_request(request, worker._WorkerBackendState())


@pytest.mark.parametrize("command", ["execute_trade", "empty_positions"])
@pytest.mark.parametrize("snapshot", [None, {}, {"currency": "CNY"}])
def test_worker_request_forwards_only_truthy_notification_snapshot(monkeypatch, command, snapshot):
    """缺省和空快照不传给执行器，非空快照保留原对象。"""
    account = build_account().model_dump(mode="json")
    payload = (
        {
            "standard_input": {"account_config": account["account_config"], "curr_target": {}},
            "trigger_source": "manual",
            "audit_input": {},
        }
        if command == "execute_trade"
        else {"empty_kwargs": {"algorithm": {}, "extra": {"audit": {}}}, "audit_input": {}}
    )
    if snapshot is not None:
        payload["notification_snapshot"] = snapshot
    request = WorkerBackendRequest(
        request_id="notification-input",
        command=command,
        account_payload=account,
        execution_id="notification",
        payload=payload,
    )
    captured = {}

    def execute(*_args, **kwargs):
        captured.update(kwargs)
        return FakeExecutor().execute(None)

    def resolve(**kwargs):
        assert kwargs["audit_context"] == {}
        return SimpleNamespace(execute=execute, empty_positions=execute)

    monkeypatch.setattr(worker, "_resolve_prepared_executor", resolve)
    monkeypatch.setattr(worker, "_append_trade_pre_execute_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "_append_empty_positions_pre_execute_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "_append_success_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "_finalize_with_orders", lambda *_args: None)

    response = worker._handle_worker_request(request, worker._WorkerBackendState())

    assert response.kind == "result", response.error
    if snapshot:
        assert captured["notification_snapshot"] is snapshot
    else:
        assert "notification_snapshot" not in captured
