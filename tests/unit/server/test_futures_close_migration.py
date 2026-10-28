"""旧平仓规则的无静默冲突迁移与账户设置持久化。"""

import pytest

from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.server.db.models import Account, AccountCreate
from axile.server.db.models.account import AccountSnapshot
from tests.conftest import native_store_call
from tests.unit.server._native_read_views import account_view


def _account_data():
    return {
        "id": 1,
        "name": "账户",
        "market": "期货",
        "trade_channel": "ctp",
        "brokerage": "test",
        "remark": None,
        "account_config": {"nested": {"secret": "test-only"}},
        "account_control_preset": "default",
        "is_started": True,
        "cron_expr": "0 9 * * *",
        "weight_precision": 0.01,
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "feishu_key": "test-key",
        "execution_notification_code": "def notify(context):\n    pass",
        "trade_rules": None,
        "forbidden_symbols": None,
        "risk_symbols": None,
    }


def test_account_policy_survives_json_settings_public_and_worker_snapshot(native_database):
    policy = FuturesClosePolicy(overrides={"ag": "today_first"})
    data = _account_data() | {"futures_close_policy": policy}
    data.pop("id")
    created = native_store_call(
        native_database,
        "account_create",
        {
            "account": AccountCreate(**data).model_dump(mode="json"),
            "now": "2026-01-01T09:00:00",
        },
    )
    stored = native_store_call(native_database, "account_get", {"account_id": created["account"]["id"]})
    account = Account.model_validate(stored)
    assert account.futures_close_policy.overrides == {"AG": "today_first"}
    assert AccountSnapshot.model_validate(account.model_dump()).futures_close_policy == policy
    assert account_view(account).futures_close_policy == policy


def test_public_capability_keeps_modes_and_supplied_fee_columns():
    from axile.server.api.routes.capabilities import list_channel_capabilities

    channels = {row.channel: row.model_dump(mode="json") for row in list_channel_capabilities()}
    assert channels["ctp"]["futures_close"]["modes"] == ["rules", "fee"]
    assert channels["tq"]["futures_close"]["modes"] == ["rules"]
    ag = next(p for p in channels["tq"]["futures_close"]["presets"] if p["product"] == "AG")
    assert (
        ag["yesterday_fee"] == ag["today_fee"] == "万分之0.5（AG2610—2612、2706）；万分之0.1（AG2701—2705、2707—2709）"
    )
    zn = next(p for p in channels["ctp"]["futures_close"]["presets"] if p["product"] == "ZN")
    assert zn["priority"] == "today_first"
    assert zn["yesterday_fee"] == "3元/手" and zn["today_fee"] == "免收"


def test_real_account_patch_rejects_tq_automatic_before_any_commit(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from fastapi import HTTPException, Response

    from axile.server.api.routes import account_crud
    from axile.server.db.models import AccountUpdate

    account = Account(
        **(
            _account_data()
            | {
                "trade_channel": "tq",
                "account_config": {"account_mode": "sim", "tq_username": "fixture", "tq_password": "fixture"},
            }
        )
    )

    async def get_account(*args):
        return account

    monkeypatch.setattr(account_crud, "_get_account_or_404", get_account)
    persist = AsyncMock()
    monkeypatch.setattr(account_crud, "_account_operation", persist)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            account_crud._update_account(None, 1, AccountUpdate(futures_close_policy={"mode": "fee"}), Response())
        )
    assert raised.value.status_code == 422
    assert "不支持低手续费优先" in str(raised.value.detail)
    persist.assert_not_called()
    assert account.futures_close_policy.mode == "rules"
