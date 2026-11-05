"""回放能力可在未安装渠道 SDK 时发现，源数据缺口按层声明。"""

import pytest

from axile.channels import get_channel
from axile.server.execution_replay.contracts import freeze_data


@pytest.mark.parametrize("channel", ["tq", "gm"])
def test_adapter_capability_is_partial_and_does_not_invent_market_trades(channel):
    capability = get_channel(channel).descriptor.replay
    assert capability.supported is True
    assert capability.streams["market_trade"]["availability"] == "unavailable"
    for kind in ("depth", "own_order", "own_trade", "position"):
        assert capability.streams[kind]["availability"] == "partial"
        assert capability.streams[kind]["intermediate_states"] == "observed_only"
    assert capability.display["order"]


@pytest.mark.parametrize(
    "channel,attributes,identity",
    [
        ("tq", {"native_status": "ALIVE", "account_mode": "sim"}, {"order_id": "O"}),
        ("gm", {"status": 1, "filled_amount": 0}, {"cl_ord_id": "O"}),
    ],
)
def test_replay_declaration_preserves_native_fields_and_rejects_undeclared_fields(channel, attributes, identity):
    context = get_channel(channel).descriptor.replay.model_dump(mode="json")
    data = {"symbol": "X", "order_id": "O", "attributes": attributes, "native_identity": identity}
    assert freeze_data("/order", data, context) == data
    with pytest.raises(ValueError, match="undeclared_extension"):
        freeze_data("/order", {**data, "attributes": {"unreviewed_field": 1}}, context)
    with pytest.raises(ValueError, match="forbidden_field"):
        freeze_data("/order", {**data, "native_identity": {"account_config": "hidden"}}, context)
