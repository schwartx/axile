"""账户级算法与渠道参数模型在落库前完成配对校验。"""

from __future__ import annotations

import pytest

from axile.common.order_param_model import OrderParamModel
from axile.executor.algorithms.core.base import register_algorithm
from axile.server.db.models.account_validation import _check_algorithm_channel_compat
from tests.unit.server.test_account_control_routes import (
    _account_payload,
    _load_account,
    _seed_account,
)
from tests.unit.server.test_account_control_routes import (
    account_client as account_client,
)


@register_algorithm(
    "TEST-POS-SIDE-ONLY", order_param_models=[OrderParamModel.POSITION_SIDE], label="仅双向持仓模型测试算法"
)
def _fake_pos_side_only_algorithm(executor, algorithm_input):
    raise NotImplementedError


def test_restricted_algorithm_rejected_on_offset_channel() -> None:
    with pytest.raises(ValueError, match="未适配渠道 ctp 的订单参数模型"):
        _check_algorithm_channel_compat({"method": "TEST-POS-SIDE-ONLY", "params": {}}, "ctp", "下单算法")


def test_create_account_rejects_incompatible_algorithm_channel_pairing(account_client) -> None:
    client, _ = account_client
    response = client.post(
        "/account/", json=_account_payload() | {"algorithm": {"method": "TEST-POS-SIDE-ONLY", "params": {}}}
    )
    assert response.status_code == 422
    assert "未适配渠道 ctp 的订单参数模型" in response.json()["detail"]


def test_update_account_rejects_incompatible_algorithm_for_next_channel(account_client) -> None:
    client, database = account_client
    _seed_account(database)
    response = client.patch("/account/1", json={"algorithm": {"method": "TEST-POS-SIDE-ONLY", "params": {}}})
    assert response.status_code == 422
    assert "未适配渠道 ctp 的订单参数模型" in response.json()["detail"]
    assert _load_account(database).algorithm["method"] == "SINGLE-MAKER"


def test_update_account_rejects_current_algorithm_when_switching_channel(account_client) -> None:
    client, database = account_client
    _seed_account(database, algorithm={"method": "TEST-POS-SIDE-ONLY", "params": {}})
    response = client.patch(
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
    assert str(_load_account(database).trade_channel) == "ctp"
