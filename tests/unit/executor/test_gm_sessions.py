"""GM 最后一道 SDK 门控以及执行参数限制。"""

from datetime import datetime
from unittest.mock import Mock

import pytest

from axile.channels.gm_sessions import SHANGHAI, gm_session_context
from axile.executor.gm.core import api_bridge
from axile.executor.gm.core.api_bridge import (
    GetCashRequest,
    GMApiBridge,
    GMOrderKind,
    GMOrderSide,
    GMPositionEffect,
    OrderCancelRequest,
    OrderVolumeRequest,
)
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig, UnifiedStandardInput
from axile.executor.order_cancellation import CancelNotAllowedError
from axile.server.db.models.account_settings import GMSessionSettings


def test_sdk_dequeue_rechecks_clock_for_both_order_and_cancel(monkeypatch):
    now = datetime(2026, 9, 1, 9, 19, tzinfo=SHANGHAI)
    context = gm_session_context(
        GMSessionSettings(opening_cancelable={"enabled": True, "time": "09:15"}).model_dump(), now
    )
    order = OrderVolumeRequest(
        "SHSE.600000", 100, GMOrderSide.BUY, GMOrderKind.LIMIT, GMPositionEffect.OPEN, 10, "test", context
    )
    order_sdk, cancel_sdk, query_sdk = Mock(), Mock(), Mock(return_value="cash")
    monkeypatch.setitem(GMApiBridge._SDK_OPERATION_METHODS, "order_volume", order_sdk)
    monkeypatch.setitem(GMApiBridge._SDK_OPERATION_METHODS, "order_cancel", cancel_sdk)
    monkeypatch.setitem(GMApiBridge._SDK_OPERATION_METHODS, "get_cash", query_sdk)
    monkeypatch.setattr(api_bridge, "clock_now", lambda **_: now)
    GMApiBridge.execute_request(order)
    GMApiBridge.execute_request(OrderCancelRequest([]))
    now = now.replace(minute=20)
    with pytest.raises(ValueError, match="已结束"):
        GMApiBridge.execute_request(order)
    with pytest.raises(CancelNotAllowedError, match="不允许撤单"):
        GMApiBridge.execute_request(OrderCancelRequest([]))
    assert GMApiBridge.execute_request(GetCashRequest()) == "cash"
    assert order_sdk.call_count == cancel_sdk.call_count == 1
    assert "session" not in order_sdk.call_args.kwargs


@pytest.mark.parametrize("symbol_override", [False, True])
def test_auction_preserves_default_and_symbol_algorithms(symbol_override):
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    settings = GMSessionSettings(opening_locked={"enabled": True, "time": "09:20"})
    context = gm_session_context(settings.model_dump(), datetime(2026, 9, 1, 9, 20, tzinfo=SHANGHAI))
    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": "gm",
            "account_config": instance.account_config.model_dump(),
            "curr_target": {"600000.SH": 0.1},
            "algorithm": {"method": "SINGLE-MAKER" if symbol_override else "TWAP"},
            "symbol_algorithms": {"600000.SH": {"method": "TWAP"}} if symbol_override else {},
            "extra": {"audit": {"gm_session": context}},
        }
    )
    normalized = instance._normalize_standard_input(standard_input)
    assert normalized.algorithm == standard_input.algorithm
    assert normalized.symbol_algorithms == standard_input.symbol_algorithms


def test_cancel_in_locked_phase_does_not_send_request_or_claim_success(monkeypatch):
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    monkeypatch.setattr(
        "axile.executor.gm.gm_execute.clock_now", lambda **_: datetime(2026, 9, 1, 9, 20, tzinfo=SHANGHAI)
    )
    bridge = Mock()
    monkeypatch.setattr(instance, "_call_bridge", bridge)
    with pytest.raises(CancelNotAllowedError, match="不允许撤单"):
        instance._cancel_order_impl("600000.SH", "pending-order")
    bridge.assert_not_called()
