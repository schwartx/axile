"""通知编排只跨业务阶段边界，不持有 Python 数据库事务。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.common.notification_function import NotificationFunctionResult
from axile.server.db.models.system_notification import SystemNotificationStatePublic
from axile.server.execution import notification_outbox, notification_state


@pytest.fixture(autouse=True)
def isolate_dispatch(monkeypatch):
    monkeypatch.setattr(notification_outbox, "_tasks", {})
    monkeypatch.setattr(notification_outbox, "_rerun", set())


@pytest.mark.parametrize("ok", [False, True])
def test_user_function_starts_after_claim_and_summary_failure_keeps_dispatching(monkeypatch, ok):
    stages = []
    event = {
        "sequence": 1,
        "event_type": "execution.finished",
        "context": {"execution": {"id": "first"}},
        "code": "def notify(context): pass",
        "feishu_key": None,
    }
    pending = [event, event | {"sequence": 2}]

    async def operation(name, request):
        stages.append(name)
        if name == "notifications_claim":
            assert request == {"account_id": 1}
            return pending.pop(0) if pending else None
        assert stages[-2] == "user_function"
        assert request["ok"] is ok
        assert request["record_summary"] is True
        return {"updated": True, "summary_error": "summary unavailable"}

    def invoke(*_args, **_kwargs):
        assert stages[-1] == "notifications_claim"
        stages.append("user_function")
        return NotificationFunctionResult(ok, None if ok else "send failed")

    monkeypatch.setattr(notification_outbox, "native_operation", operation)
    monkeypatch.setattr(notification_outbox, "run_notification_function", invoke)
    asyncio.run(notification_outbox._dispatch(1))
    assert stages == ["notifications_claim", "user_function", "notifications_finish"] * 2 + ["notifications_claim"]


@pytest.mark.parametrize(
    "code,key,ok,summary",
    [(None, None, True, False), (DEFAULT_ACCOUNT_NOTIFICATION_CODE, None, False, True)],
    ids=["disabled", "missing_webhook"],
)
def test_unconfigured_and_disabled_functions_finish_without_invoking_user_code(monkeypatch, code, key, ok, summary):
    receipts = []
    claimed = False

    async def operation(name, request):
        nonlocal claimed
        if name == "notifications_claim":
            if claimed:
                return None
            claimed = True
            return {"sequence": 1, "event_type": "execution.finished", "context": {}, "code": code, "feishu_key": key}
        receipts.append(request)
        return {"updated": True}

    monkeypatch.setattr(notification_outbox, "native_operation", operation)
    monkeypatch.setattr(
        notification_outbox, "run_notification_function", lambda *_args, **_kwargs: pytest.fail("不应调用用户函数")
    )
    asyncio.run(notification_outbox._dispatch(1))
    assert receipts[0]["ok"] is ok
    assert receipts[0]["record_summary"] is summary


def test_control_warning_uses_system_sender_and_skips_account_summary(monkeypatch):
    from axile.server import error_notifications

    sender = AsyncMock(return_value=NotificationFunctionResult(True))
    native = AsyncMock(
        side_effect=[
            {
                "sequence": 1,
                "event_type": ACCOUNT_CONTROL_WARNING_EVENT,
                "context": {"event_type": ACCOUNT_CONTROL_WARNING_EVENT},
                "code": None,
                "feishu_key": None,
            },
            {"updated": True},
            None,
        ]
    )
    monkeypatch.setattr(error_notifications, "send_system_notification", sender)
    monkeypatch.setattr(notification_outbox, "native_operation", native)
    asyncio.run(notification_outbox._dispatch(1))
    sender.assert_awaited_once()
    assert native.await_args_list[1].args[1]["record_summary"] is False


def test_recovery_wakes_pending_accounts_despite_summary_failure(monkeypatch):
    native = AsyncMock(return_value={"account_ids": [1, 3], "summary_errors": ["summary unavailable"]})
    wakes = []
    monkeypatch.setattr(notification_outbox, "native_operation", native)
    monkeypatch.setattr(notification_outbox, "wake_notifications", wakes.append)
    asyncio.run(notification_outbox.recover_notifications())
    assert wakes == [1, 3]
    assert native.await_args.args[0] == "notifications_recover"
    assert native.await_args.args[1]["finished_at"]


def test_summary_wrappers_preserve_models_and_reject_unfinished_system_results(monkeypatch):
    calls = []

    def operation(name, request):
        calls.append((name, request))
        return {"finished_at": "2026-10-23T12:00:00", "ok": False, "error": "timeout"}

    monkeypatch.setattr(notification_state, "native_operation_sync", operation)
    result = notification_state.get_system_notification_result()
    assert result == SystemNotificationStatePublic(finished_at="2026-10-23T12:00:00", ok=False, error="timeout")
    notification_state.record_system_notification_result(result)
    assert calls[-1] == ("notifications_record_system", result.model_dump(mode="json"))
    notification_state.record_notification_result(2, "execution", "2026-10-23T13:00:00", True, None)
    assert calls[-1][0] == "notifications_record_account"
    assert calls[-1][1]["event_type"] == "execution.finished"
    with pytest.raises(ValueError, match="完成时间"):
        notification_state.record_system_notification_result(SystemNotificationStatePublic())
