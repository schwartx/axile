"""账户流控编辑模型路由测试."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import AccountControlOverride
from axile.server.api.routes import account_control_policy_routes


def _account(*, channel: TradeChannel = TradeChannel.CTP) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        trade_channel=channel,
        account_control_preset="default",
        account_control_override=AccountControlOverride.model_validate(
            {"operations": {"place_order": {"account": {"per_day": {"limit": 12, "on_trigger": "block"}}}}}
        ),
    )


def test_policy_route_returns_chinese_catalog_and_effective_override(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_account(_account_id: int) -> SimpleNamespace:
        return _account()

    monkeypatch.setattr(account_control_policy_routes, "_get_account_or_404", fake_get_account)

    result = asyncio.run(account_control_policy_routes.get_account_control_policy(1, None))

    assert result["preset_display_name"] == "默认"
    assert set(result["preset_policy"]) == set(result["effective_policy"]) == {"timezone", "operations", "groups"}
    assert [item["display_name"] for item in result["compatible_presets"]] == ["默认", "CTP"]
    assert result["effective_policy"]["operations"]["place_order"]["account"]["per_day"] is not None
    assert result["effective_policy"]["operations"]["place_order"]["account"]["per_day"]["limit"] == 12
    assert {item["display_name"] for item in result["operations"]} >= {"下单", "撤单", "查询订单"}
    operation_names = {item["key"]: item["display_name"] for item in result["operations"]}
    assert "insert_order" not in operation_names
    assert operation_names["option_self_close"] == "期权自对冲"
    assert operation_names["query_account"] == "查询资金"
    assert "insert_order" not in result["preset_policy"]["operations"]
    assert "insert_order" not in result["effective_policy"]["operations"]


def test_policy_route_previews_ctp_without_mutating_account(monkeypatch: pytest.MonkeyPatch) -> None:
    account = _account()

    async def fake_get_account(_account_id: int) -> SimpleNamespace:
        return account

    monkeypatch.setattr(account_control_policy_routes, "_get_account_or_404", fake_get_account)

    result = asyncio.run(account_control_policy_routes.get_account_control_policy(1, "ctp"))

    assert result["preset_display_name"] == "CTP"
    assert result["effective_policy"]["operations"]["place_order"]["account"]["per_day"] is not None
    assert result["effective_policy"]["operations"]["place_order"]["account"]["per_day"]["limit"] == 12
    place_order = next(item for item in result["operations"] if item["key"] == "place_order")
    assert place_order["groups"] == ["ctp_td_global"]
    assert account.account_control_preset == "default"


def test_policy_route_roundtrips_daily_warning_without_losing_existing_quota(monkeypatch):
    account = _account()
    account.account_control_override = account.account_control_override.model_dump(mode="json")
    account.account_control_override["operations"]["place_order"]["daily_warning_percent"] = 80

    async def get_account(_account_id):
        return account

    monkeypatch.setattr(account_control_policy_routes, "_get_account_or_404", get_account)
    result = asyncio.run(account_control_policy_routes.get_account_control_policy(1, None))
    assert result["override"]["operations"]["place_order"]["daily_warning_percent"] == 80
    assert result["effective_policy"]["operations"]["place_order"]["daily_warning_percent"] == 80
    assert result["effective_policy"]["operations"]["place_order"]["account"]["per_day"]["limit"] == 12


def test_policy_route_rejects_incompatible_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_account(_account_id: int) -> SimpleNamespace:
        return _account(channel=TradeChannel("plugin-channel"))

    monkeypatch.setattr(account_control_policy_routes, "_get_account_or_404", fake_get_account)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(account_control_policy_routes.get_account_control_policy(1, "ctp"))

    assert exc_info.value.status_code == 422
