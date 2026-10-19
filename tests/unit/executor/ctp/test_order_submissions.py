"""CTP 额度放行、本地拒绝与真实原生调用必须能够分别核对。"""

from datetime import datetime

import pytest

from axile.executor.ctp.ctp_execute import CtpRequestError
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.order_submissions import OrderSubmissionRecorder
from tests.unit.executor.ctp.test_requests import _SHANGHAI, _at_clock, _submit_point_executor, config  # noqa: F401
from tests.unit.executor.test_account_control_monitor import make_guard, used


def test_local_validation_consumes_allowance_but_does_not_record_native_submission(config):  # noqa: F811
    executor = _submit_point_executor(config)
    guard = make_guard(baseline_count=0)
    executor.set_account_control_guard(guard)
    facts = []
    executor.order_submission_recorder = OrderSubmissionRecorder(1, facts.append)

    with pytest.raises(ValueError):
        executor.place_order("ag2612", "invalid", OrderType.LIMIT, 1, 9000)

    assert used(guard.monitor_snapshot()) == 1
    assert facts == []
    executor._trader_api.ReqOrderInsert.assert_not_called()


@pytest.mark.parametrize("public", [True, False])
@pytest.mark.parametrize("code", [0, -3])
def test_public_and_direct_channel_calls_share_native_recording(config, monkeypatch, public, code):  # noqa: F811
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))
    guard = make_guard(baseline_count=0)
    if public:
        executor.set_account_control_guard(guard)
    facts = []
    executor.order_submission_recorder = OrderSubmissionRecorder(1, facts.append, source="script")
    executor._trader_api.ReqOrderInsert.return_value = code
    place = executor.place_order if public else executor._place_order_impl
    if code:
        with pytest.raises(CtpRequestError):
            place("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")
    else:
        place("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_called_once()
    assert len({fact.submission_uid for fact in facts}) == 1
    assert facts[-1].return_code == code
    assert facts[-1].execution_id == (guard.execution_id if public else None)
    assert used(guard.monitor_snapshot()) == int(public)

    executor._control_order_result(None, "broker_rejected", {"error_id": 31}, order_id=facts[-1].order_id)
    assert facts[-1].broker_status == "rejected"
    assert facts[-1].return_code == code


def test_native_cancel_recorded_once_without_overwriting_order_callback(config, monkeypatch):  # noqa: F811
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))
    executor.set_account_control_guard(make_guard(baseline_count=0))
    facts = []
    executor.order_submission_recorder = OrderSubmissionRecorder(1, facts.append)
    order = executor.place_order("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")
    executor._trader_api.ReqOrderAction.return_value = 0
    executor.cancel_order("ag2612", order.order_id)
    executor._control_order_result(None, "broker_rejected", {"error_id": 31}, order_id=order.order_id)
    latest = {fact.submission_uid: fact for fact in facts}
    assert len(latest) == 2
    by_operation = {fact.operation: fact for fact in latest.values()}
    assert by_operation["place_order"].broker_status == "rejected"
    assert by_operation["cancel_order"].send_status == "accepted"
    assert by_operation["cancel_order"].broker_status == "unknown"
    executor._trader_api.ReqOrderAction.assert_called_once()
