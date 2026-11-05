"""服务端飞书错误通知测试."""

import asyncio

import pytest

from axile.server import error_notifications


@pytest.fixture(autouse=True)
def isolate_notification_state(monkeypatch):
    monkeypatch.setattr(error_notifications, "record_system_notification_result", lambda _state: None)


def test_send_feishu_error_logs_sender_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """通知发送失败应记日志且不影响执行异常处理链路."""
    errors: list[str] = []

    async def _external_ip() -> str:
        return ""

    def _push(_card: dict[str, object], _key: str) -> None:
        raise RuntimeError("webhook unavailable")

    monkeypatch.setattr(error_notifications, "get_external_ip", _external_ip)
    monkeypatch.setattr(error_notifications, "push_feishu_card", _push)
    monkeypatch.setattr(error_notifications.loguru.logger, "error", lambda message: errors.append(str(message)))

    asyncio.run(error_notifications.send_feishu_error(RuntimeError("boom"), None, "hook-error"))

    assert errors == ["发送飞书错误通知失败: webhook unavailable"]


def test_persisted_connection_failure_uses_system_sender_without_exception_object(monkeypatch):
    from axile.common.notification_context import CONNECTION_FAILURE_EVENT

    cards = []
    monkeypatch.setattr(error_notifications.settings, "system_execution_notification_mode", "default")
    monkeypatch.setattr(error_notifications.settings, "exe_err_feishu_key", "test-hook")
    monkeypatch.setattr(error_notifications, "push_feishu_card", lambda card, key: cards.append(card))
    result = asyncio.run(
        error_notifications.send_system_notification(
            {
                "event_id": "fault",
                "event_type": CONNECTION_FAILURE_EVENT,
                "execution_id": None,
                "occurred_at": "now",
                "account": {"id": 1, "name": "测试账户"},
                "error": {"type": "AccountConnectionFailure", "message": "请连接交易账户", "traceback": ""},
                "is_test": False,
            }
        )
    )
    assert result.ok
    assert len(cards) == 1
    assert cards[0]["header"]["title"]["content"] == "账户连接告警"
    assert "请连接交易账户" in str(cards[0])
