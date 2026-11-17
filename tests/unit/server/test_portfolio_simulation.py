"""模拟账户契约、校验与一次性执行边界。"""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from axile.channels import SimulationAccount, get_channel
from axile.common.serialization import parse_input, to_payload
from axile.server.api.routes import portfolio as routes
from axile.server.app import validation_exception_handler
from axile.server.portfolio_runner import SimulationPortfolioExecutor, calculate_sample_portfolio


@pytest.fixture
def client(monkeypatch):
    native = AsyncMock(side_effect=AssertionError("模拟试跑不得访问真实账户或保存目标"))
    monkeypatch.setattr(routes, "_portfolio_native", native)
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client
    native.assert_not_awaited()


def snapshot():
    value = to_payload(get_channel("ctp").descriptor.simulation_account, mode="json")
    value["account"]["positions"] = [
        {"symbol": symbol, "direction": "多头", "volume": 2, "available_volume": 2, "market_value": 100}
        for symbol in ("rb2610", "ag2612")
    ]
    return value


@pytest.mark.parametrize("channel", ["ctp", "gm", "tq"])
def test_channel_presets_roundtrip_and_contain_every_example(channel):
    descriptor = get_channel(channel).descriptor
    simulation = parse_input(SimulationAccount, to_payload(descriptor.simulation_account, mode="json"))
    simulation.validate_channel(descriptor)
    assert simulation.account.currency == descriptor.currency
    assert simulation.account.positions == []
    assert simulation.account.market_value == 0
    assert simulation.account.available_cash == simulation.account.total_asset
    assert {quote.symbol for quote in simulation.quotes} == set(descriptor.portfolio.example_symbols)
    assert simulation.long_leverage == descriptor.defaults.long_leverage
    assert simulation.short_leverage == descriptor.defaults.short_leverage


def test_simulation_queries_and_settings_execute_in_fresh_child(client):
    simulation = snapshot()
    simulation["account"]["total_asset"] = 123456
    simulation["long_leverage"] = 2.5
    code = (
        "def calculate_portfolio(context):\n"
        "    assert context.executor.get_account_assets() is context.account\n"
        "    assert context.get_pending_orders() == []\n"
        "    assert context.query_trades('rb2610', '1') == []\n"
        "    assert len(context.get_positions('rb2610', '多头')) == 1\n"
        "    value = context.account.total_asset\n"
        "    context.account.total_asset = 0\n"
        "    return {'equity': value, 'price': context.get_price('rb2610'),\n"
        "            'multiplier': context.get_quote('rb2610').extra['volume_multiple'],\n"
        "            'leverage': context.settings.long_leverage}\n"
    )
    for _ in range(2):
        response = client.post(
            "/portfolio/validate_custom_calc",
            json={"custom_calc_py_code": code, "simulation_channel": "ctp", "simulation_account": simulation},
        )
        assert response.status_code == 200, response.text
        assert response.json()["target"] == {"equity": 123456, "price": 3500, "multiplier": 10, "leverage": 2.5}
    assert get_channel("ctp").descriptor.simulation_account.account.total_asset == 1_000_000


@pytest.mark.parametrize(
    "expression,error",
    [
        ("context.get_price('MISSING')", "缺少模拟行情: MISSING"),
        ("context.executor.place_order('rb2610')", "place_order"),
    ],
)
def test_simulation_missing_quote_and_trading_are_explicit_failures(client, expression, error):
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "simulation_channel": "ctp",
            "custom_calc_py_code": f"def calculate_portfolio(context):\n    return {{'x': {expression}}}\n",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["valid"] is False and result["error_line"] == 2
    assert error in result["error_message"]


@pytest.mark.parametrize(
    "path,value",
    [
        (("account", "total_asset"), float("nan")),
        (("account", "available_cash"), -1),
        (("account", "currency"), "USD"),
        (("account", "positions", 0, "symbol"), " "),
        (("account", "positions", 0, "volume"), -1),
        (("account", "positions", 0, "available_volume"), 3),
        (("account", "positions", 0, "direction"), "invalid"),
        (("account", "positions", 0, "market_value"), float("inf")),
        (("quotes", 0, "last_price"), 0),
        (("quotes", 0, "symbol"), ""),
        (("quotes", 0, "extra", "volume_multiple"), 0),
        (("quotes", 0, "extra", "price_tick"), True),
        (("quotes", 0, "extra", "custom"), float("inf")),
        (("long_leverage",), 0.15),
        (("short_leverage",), 126),
        (("long_leverage",), True),
    ],
)
def test_invalid_snapshots_are_rejected_before_execution(client, path, value):
    simulation = snapshot()
    target = simulation
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    response = client.post(
        "/portfolio/validate_custom_calc",
        content=json.dumps(
            {"simulation_channel": "ctp", "simulation_account": simulation, "custom_calc_py_code": "code"}
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("change", ["duplicate", "missing"])
def test_duplicate_or_missing_quotes_rejected(client, change):
    simulation = snapshot()
    if change == "duplicate":
        simulation["quotes"].append(deepcopy(simulation["quotes"][0]))
    else:
        simulation["quotes"].pop()
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "simulation_channel": "ctp",
            "simulation_account": simulation,
            "custom_calc_py_code": "code",
        },
    )
    assert response.status_code == 422
    assert ("重复模拟行情" if change == "duplicate" else "缺少模拟行情") in response.text


@pytest.mark.parametrize("direction,valid", [("多头", False), ("空头", True)])
def test_position_identity_is_symbol_and_direction(client, direction, valid):
    simulation = snapshot()
    position = deepcopy(simulation["account"]["positions"][0])
    position["direction"] = direction
    simulation["account"]["positions"].append(position)
    response = client.post(
        "/portfolio/validate_custom_calc",
        json={
            "simulation_channel": "ctp",
            "simulation_account": simulation,
            "custom_calc_py_code": "def calculate_portfolio(context):\n    return {}\n",
        },
    )
    assert response.status_code == (200 if valid else 422), response.text
    if valid:
        assert response.json()["valid"] is True
    else:
        assert "重复模拟持仓: rb2610 多头" in response.text


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"simulation_account": {}},
        {"simulation_channel": "unknown"},
        {"account_id": 1, "simulation_channel": "ctp"},
        {"account_id": 1, "simulation_account": snapshot()},
    ],
)
def test_account_selection_is_required_and_mutually_exclusive(client, inputs):
    response = client.post("/portfolio/validate_custom_calc", json={"custom_calc_py_code": "code", **inputs})
    assert response.status_code == 422


def test_simulation_executor_copies_input_and_exposes_only_query_methods():
    source = parse_input(SimulationAccount, snapshot())
    executor = SimulationPortfolioExecutor(source)
    executor.get_account_assets().positions.clear()
    assert source.account.positions
    with pytest.raises(ValueError, match="缺少模拟行情"):
        executor.get_market_data(["missing"])
    assert not hasattr(executor, "connect") and not hasattr(executor, "place_order")


def test_capabilities_publish_presets_even_without_sdk(monkeypatch):
    from axile.server.api.routes import capabilities

    monkeypatch.setattr(capabilities, "missing_packages", lambda channel: ["missing_sdk"])
    values = capabilities.list_channel_capabilities()
    for channel in ("ctp", "gm", "tq"):
        value = next(item for item in values if item["channel"] == channel)
        assert value["available"] is False
        assert value["simulation_account"]["account"]["positions"] == []
        assert value["simulation_account"]["quotes"]


def test_refresh_without_real_account_does_not_save_or_run_sample(monkeypatch):
    from axile.server.db.models import Portfolio

    portfolio = Portfolio(id=991, name="draft", market="futures", custom_calc_py_code="code")
    native = AsyncMock(return_value={"portfolio": to_payload(portfolio), "account_id": None, "account": None})
    sample = AsyncMock(side_effect=AssertionError("正式刷新不能运行模拟账户"))
    save = AsyncMock()
    monkeypatch.setattr(routes, "_portfolio_native", native)
    monkeypatch.setattr(routes, "calculate_sample_portfolio", sample)
    monkeypatch.setattr(routes, "append_target_weight_snapshot", save)
    app = FastAPI()
    app.include_router(routes.router)
    response = TestClient(app).post("/portfolio/991/target_snapshot/refresh")
    assert response.status_code == 422
    sample.assert_not_called()
    save.assert_not_awaited()
    assert routes.try_register_target_refresh(991, None)
    routes.clear_target_refresh(991, None)


def test_sample_runner_remains_compatible_without_simulation():
    assert calculate_sample_portfolio("def calculate_portfolio(context):\n    return {'x': 1}\n").targets == {"x": 1}
