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


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
@pytest.mark.parametrize("account_id", [None, 7])
def test_trial_rejects_invalid_leverages_before_running(monkeypatch, value, account_id):
    client, _, _, runner = _client(monkeypatch)
    response = client.post(
        "/portfolio/validate_custom_calc",
        content=json.dumps(
            {
                "custom_calc_py_code": "code",
                "account_id": account_id,
                "leverage_overrides": {"long_leverage": value},
            }
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    runner.assert_not_awaited()


def test_sample_trial_uses_temporary_leverage_and_complete_quotes_without_database(monkeypatch):
    client, _, native, runner = _client(monkeypatch)
    code = (
        "def calculate_portfolio(context):\n"
        "    quote = context.get_quote('FG701')\n"
        "    return {'long': context.settings.long_leverage, 'short': context.settings.short_leverage,\n"
        "            'notional': quote.last_price * quote.extra['volume_multiple']}\n"
    )
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "custom_calc_py_code": code,
            "leverage_overrides": {"long_leverage": 2.5, "short_leverage": 0},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["valid"] is True
    assert response.json()["target"] == {"long": 2.5, "short": 0, "notional": 100}
    restored = client.post("/portfolio/validate_custom_calc", json={"custom_calc_py_code": code})
    assert restored.status_code == 200, restored.text
    assert restored.json()["target"] == {"long": 1, "short": 1, "notional": 100}
    native.assert_not_awaited()
    runner.assert_not_awaited()


@pytest.mark.parametrize("value", [0.15, 1e100])
def test_account_trial_enforces_channel_step_and_range(monkeypatch, value):
    client, _, _, runner = _client(monkeypatch)
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "custom_calc_py_code": "code",
            "account_id": 7,
            "leverage_overrides": {"long_leverage": value},
        },
    )
    assert response.status_code == 422, response.text
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
