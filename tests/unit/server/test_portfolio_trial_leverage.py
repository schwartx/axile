"""临时杠杆只影响本次试跑，保持账户和组合持久化边界。"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from axile.channels import get_channel
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.server.api.routes import portfolio as routes
from axile.server.app import validation_exception_handler
from tests.unit.server.test_portfolio_routes import _account_payload


def _client(monkeypatch):
    account = _account_payload(7) | {"long_leverage": 3.0, "short_leverage": 2.0}
    native = AsyncMock(return_value=account)
    runner = AsyncMock(return_value=PortfolioTarget.from_result({"long": 4, "short": 0}))
    monkeypatch.setattr(routes, "_portfolio_native", native)
    monkeypatch.setattr(routes, "calculate_portfolio_for_account", runner)
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(routes.router)
    return TestClient(app), account, native, runner


def test_trial_forwards_temporary_leverage_without_changing_account_or_saving(monkeypatch):
    client, account, native, runner = _client(monkeypatch)
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "custom_calc_py_code": "code",
            "account_id": 7,
            "leverage_overrides": {"long_leverage": 4, "short_leverage": 0},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["target"] == {"long": 4, "short": 0}
    native.assert_awaited_once_with("portfolio_operation", {"op": "validation_context", "account_id": 7})
    assert runner.await_args.kwargs == {"leverage_overrides": {"long_leverage": 4, "short_leverage": 0}}
    context = runner.await_args.args[0]
    assert context.long_leverage == account["long_leverage"] == 3.0
    assert context.short_leverage == account["short_leverage"] == 2.0


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), 0.15, 1e100])
def test_trial_rejects_invalid_leverages_before_running(monkeypatch, value):
    client, _, _, runner = _client(monkeypatch)
    response = client.post(
        "/portfolio/validate_custom_calc",
        content=json.dumps(
            {
                "custom_calc_py_code": "code",
                "account_id": 7,
                "leverage_overrides": {"long_leverage": value},
            }
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    runner.assert_not_awaited()


def test_trial_requires_account_for_overrides(monkeypatch):
    client, _, native, runner = _client(monkeypatch)
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "custom_calc_py_code": "code",
            "leverage_overrides": {"long_leverage": 1},
        },
    )
    assert response.status_code == 422
    native.assert_not_awaited()
    runner.assert_not_awaited()


def test_trial_accepts_channel_leverage_boundary(monkeypatch):
    client, _, _, runner = _client(monkeypatch)
    limits = get_channel("ctp").descriptor.leverage
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "custom_calc_py_code": "code",
            "account_id": 7,
            "leverage_overrides": {"long_leverage": limits.max},
        },
    )
    assert response.status_code == 200, response.text
    runner.assert_awaited_once()
