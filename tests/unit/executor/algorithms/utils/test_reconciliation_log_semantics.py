"""REST终态对账不能据此断言WS回报缺失。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.models.order_channel_health import OrderChannelHealth


@pytest.mark.parametrize("health", [OrderChannelHealth.HEALTHY, OrderChannelHealth.UNKNOWN])
def test_rest_confirmation_preserves_callback_and_reports_only_observed_source(health):
    terminal = SimpleNamespace(is_completed=lambda: True)
    executor = SimpleNamespace(
        logger=MagicMock(),
        audit_context={},
        get_order_channel_health=lambda: health,
        reconcile_terminal_order=lambda symbol, order_id: terminal,
    )
    tracker = OrderTracker(executor=executor)
    tracker._logger = MagicMock()
    tracker.pending_orders["old"] = SimpleNamespace(symbol="XRPUSDT")
    confirmed = []
    tracker.on_order_update = confirmed.append
    tracker._reconcile_missing_pending([])
    assert confirmed == [terminal]
    message = tracker._logger.info.call_args.args[0]
    assert "WS 回报缺失" not in message
    assert (
        "REST确认终态，可能与WS回调并发" if health == OrderChannelHealth.HEALTHY else "WS 未启用，纯 REST 对账"
    ) in message
