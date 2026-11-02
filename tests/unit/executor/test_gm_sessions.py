"""GM 最后一道 SDK 门控以及执行参数限制。"""

from datetime import datetime
from unittest.mock import Mock

import pytest

pytest.importorskip("gm.api")

from axile.channels.gm_sessions import SHANGHAI, gm_session_context
from axile.executor.abstract_executor import AbstractExecutor
from axile.executor.gm.core import api_bridge
from axile.executor.gm.core.api_bridge import GMApiBridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig, UnifiedStandardInput
from axile.executor.order_cancellation import CancelNotAllowedError
from axile.executor.termination import ExecutionTerminated
from axile.server.db.models.account_settings import GMSessionSettings


def test_sdk_dequeue_rechecks_clock_for_both_order_and_cancel(monkeypatch):
    now = datetime(2026, 9, 1, 9, 19, tzinfo=SHANGHAI)
    context = gm_session_context(
        GMSessionSettings(opening_cancelable={"enabled": True, "time": "09:15"}).model_dump(), now
    )
    order = {
        "symbol": "SHSE.600000",
        "volume": 100,
        "side": 1,
        "order_type": api_bridge.OrderType_Limit,
        "position_effect": 1,
        "price": 10,
        "account": "test",
        "session": context,
    }
    order_sdk, cancel_sdk, query_sdk = Mock(), Mock(), Mock(return_value="cash")
    monkeypatch.setattr(api_bridge, "order_volume", order_sdk)
    monkeypatch.setattr(api_bridge, "order_cancel", cancel_sdk)
    monkeypatch.setattr(api_bridge, "get_cash", query_sdk)
    monkeypatch.setattr(api_bridge, "clock_now", lambda **_: now)
    GMApiBridge.execute_request("order_volume", **order)
    GMApiBridge.execute_request("order_cancel", wait_cancel_orders=[])
    now = now.replace(minute=20)
    with pytest.raises(ValueError, match="已结束"):
        GMApiBridge.execute_request("order_volume", **order)
    with pytest.raises(CancelNotAllowedError, match="不允许撤单"):
        GMApiBridge.execute_request("order_cancel", wait_cancel_orders=[])
    assert GMApiBridge.execute_request("get_cash") == "cash"
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


@pytest.mark.parametrize("cleanup", [True, False])
@pytest.mark.parametrize("retain_runtime", [True, False])
@pytest.mark.parametrize("exit_kind", ["return", "error", "terminate"])
def test_execution_always_releases_phase_context(monkeypatch, cleanup, retain_runtime, exit_kind):
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    context = gm_session_context(
        GMSessionSettings(opening_locked={"enabled": True, "time": "09:20"}).model_dump(),
        datetime(2026, 9, 1, 9, 20, tzinfo=SHANGHAI),
    )
    request = {
        "symbol": "SHSE.600000",
        "volume": 100,
        "side": 1,
        "order_type": api_bridge.OrderType_Limit,
        "position_effect": 1,
        "price": 10,
        "account": "test",
        "session": context,
    }
    result = Mock()

    def execute(self, standard_input, cleanup, retain_runtime, notification_snapshot):
        self._gm_session = context
        if exit_kind == "error":
            raise ValueError("execution failed")
        if exit_kind == "terminate":
            raise ExecutionTerminated(reason="stop", mode="graceful")
        return result

    monkeypatch.setattr(AbstractExecutor, "execute", execute)
    if exit_kind == "return":
        assert instance.execute(Mock(), cleanup=cleanup, retain_runtime=retain_runtime) is result
    else:
        with pytest.raises(ValueError if exit_kind == "error" else ExecutionTerminated):
            instance.execute(Mock(), cleanup=cleanup, retain_runtime=retain_runtime)
    assert instance._gm_session is None
    assert request["session"] is context
    monkeypatch.setattr(api_bridge, "clock_now", lambda **_: datetime(2026, 9, 1, 9, 30, tzinfo=SHANGHAI))
    sdk = Mock()
    monkeypatch.setattr(api_bridge, "order_volume", sdk)
    with pytest.raises(ValueError, match="已结束"):
        GMApiBridge.execute_request("order_volume", **request)
    sdk.assert_not_called()
