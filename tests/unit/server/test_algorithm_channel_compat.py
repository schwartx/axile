"""账户级算法 × 渠道订单参数模型的配置期配对校验测试."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from axile.common.order_param_model import OrderParamModel
from axile.executor.algorithms.core.base import register_algorithm
from axile.server.db.models.account_validation import _check_algorithm_channel_compat


@register_algorithm(
    "TEST-POS-SIDE-ONLY",
    order_param_models=[OrderParamModel.POSITION_SIDE],
    label="仅双向持仓模型测试算法",
)
def _fake_pos_side_only_algorithm(executor, algorithm_input):  # pragma: no cover - 仅注册元数据
    raise NotImplementedError


def test_restricted_algorithm_rejected_on_offset_channel() -> None:
    """声明仅 POSITION_SIDE 的算法配对期货(OFFSET)渠道时拒绝."""
    config = {"method": "TEST-POS-SIDE-ONLY", "params": {}}

    with pytest.raises(ValueError, match="未适配渠道 ctp 的订单参数模型"):
        _check_algorithm_channel_compat(config, "ctp", "下单算法")


def test_create_account_rejects_incompatible_algorithm_channel_pairing(monkeypatch) -> None:
    """创建账户时算法与渠道参数模型不兼容返回 422."""
    from axile.server.api.routes import account_crud as account_crud_routes
    from tests.unit.server.test_account_control_routes import (
        _account_payload,
        _build_app,
        _noop_async,
        _RouteSession,
    )

    monkeypatch.setattr(account_crud_routes, "parse_cron_expr", lambda _expr: ["fake-trigger"])
    monkeypatch.setattr(account_crud_routes, "add_record_portfolio_account", _noop_async)
    session = _RouteSession()
    payload = _account_payload()
    payload["algorithm"] = {"method": "TEST-POS-SIDE-ONLY", "params": {}}

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 422
    assert "未适配渠道 ctp 的订单参数模型" in response.json()["detail"]
    assert session.account is None


def test_update_account_rejects_incompatible_algorithm_for_next_channel(monkeypatch) -> None:
    """PATCH 只改算法时按生效渠道校验,不兼容返回 422."""
    from axile.server.api.routes import account_crud as account_crud_routes
    from tests.unit.server.test_account_control_routes import (
        _build_account,
        _build_app,
        _noop_async,
        _RouteSession,
        _synchronized_runtime_sync,
    )

    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(_build_account())

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={"algorithm": {"method": "TEST-POS-SIDE-ONLY", "params": {}}},
    )

    assert response.status_code == 422
    assert "未适配渠道 ctp 的订单参数模型" in response.json()["detail"]


def test_update_account_rejects_current_algorithm_when_switching_channel(monkeypatch) -> None:
    """PATCH 只改渠道时按存量算法校验,不兼容返回 422."""
    from axile.server.api.routes import account_crud as account_crud_routes
    from tests.unit.server.test_account_control_routes import (
        _build_account,
        _build_app,
        _noop_async,
        _RouteSession,
        _synchronized_runtime_sync,
    )

    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    account = _build_account()
    account.algorithm = {"method": "TEST-POS-SIDE-ONLY", "params": {}}
    session = _RouteSession(account)

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={
            "trade_channel": "gm",
            "account_config": {
                "account_id": "gm-account",
                "token": "token",
                "connection_mode": "service",
                "serv_addr": "127.0.0.1:7001",
            },
        },
    )

    assert response.status_code == 422
    assert "未适配渠道 gm 的订单参数模型" in response.json()["detail"]
