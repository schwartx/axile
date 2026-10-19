"""原生调用事实独立于业务放行，并保留同步及异步竞态的已知结果。"""

from unittest.mock import Mock

import pytest

from axile.executor import order_submissions as submissions_module
from axile.executor.order_submissions import OrderSubmissionRecorder


def test_native_call_keeps_early_broker_reject_and_repeated_callbacks_do_not_duplicate():
    facts = []
    recorder = OrderSubmissionRecorder(1, facts.append)

    def send():
        recorder.broker_result("order", rejected=True, error_id=31)
        return 0

    assert recorder.submit(send, symbol="ag2612", order_id="order", request_id=1) == 0
    recorder.broker_result("order", rejected=False)
    recorder.broker_result("another-session-order", rejected=True)
    final = facts[-1]
    assert len({fact.submission_uid for fact in facts}) == 1
    assert final.send_status == "accepted"
    assert final.broker_status == "rejected"
    assert final.error_id == 31
    assert final.revision == 2


@pytest.mark.parametrize("code", [-1, -2, -3, 0])
def test_each_native_call_is_recorded_once_without_retry(code):
    facts = []
    recorder = OrderSubmissionRecorder(1, facts.append, source="main_program")
    send = Mock(return_value=code)
    assert recorder.submit(send, symbol="ag2612", order_id="order", request_id=1, execution_id="exec") == code
    send.assert_called_once()
    assert facts[0].send_status == "pending"
    assert facts[-1].return_code == code
    assert facts[-1].source == "main_program"
    assert facts[-1].execution_id == "exec"
    assert facts[-1].send_status == ("accepted" if code == 0 else "rejected")


def test_persistence_failure_before_send_prevents_unrecorded_submission():
    recorder = OrderSubmissionRecorder(1, Mock(side_effect=RuntimeError("database unavailable")))
    send = Mock()
    with pytest.raises(RuntimeError, match="database unavailable"):
        recorder.submit(send, symbol="ag2612", order_id="order", request_id=1)
    send.assert_not_called()


def test_failure_after_send_does_not_change_success_into_retryable_exception():
    saved = []

    def sink(fact):
        if fact.revision:
            raise RuntimeError("database unavailable")
        saved.append(fact)

    recorder = OrderSubmissionRecorder(1, sink)
    send = Mock(return_value=0)
    assert recorder.submit(send, symbol="ag2612", order_id="order", request_id=1) == 0
    send.assert_called_once()
    assert saved[0].send_status == "pending"


def test_native_exception_preserves_uncertainty_instead_of_assuming_no_order():
    facts = []
    recorder = OrderSubmissionRecorder(1, facts.append)
    send = Mock(side_effect=TimeoutError("unknown delivery"))
    with pytest.raises(TimeoutError):
        recorder.submit(send, symbol="ag2612", order_id="order", request_id=1)
    assert facts[-1].send_status == "unknown"
    assert facts[-1].broker_status == "unknown"
    send.assert_called_once()


def test_request_identity_cannot_be_reused_in_same_recorder():
    recorder = OrderSubmissionRecorder(1, lambda _: None)
    recorder.submit(lambda: 0, symbol="ag2612", order_id="order", request_id=1)
    send = Mock()
    with pytest.raises(ValueError, match="request_id"):
        recorder.submit(send, symbol="ag2612", order_id="order2", request_id=1)
    send.assert_not_called()


def test_call_time_is_sampled_after_initial_durable_write(monkeypatch):
    now = [1000]
    facts = []
    monkeypatch.setattr(submissions_module, "clock_now_ms", lambda: now[0])

    def sink(fact):
        facts.append(fact)
        now[0] = 2000

    recorder = OrderSubmissionRecorder(1, sink)
    recorder.submit(lambda: 0, symbol="ag2612", order_id="order", request_id=1)
    assert facts[0].occurred_at_ms == 1000
    assert facts[-1].occurred_at_ms == 2000
