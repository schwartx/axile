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
