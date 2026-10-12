"""只打印本地摘要的教学通知函数；不发送任何外部消息。"""

import json

from axile.common.notification_context import AccountNotificationContext


def notify(context: AccountNotificationContext) -> None:
    """同时兼容执行完成与没有 execution 的通知事件。"""
    execution = context.get("execution") or {}
    message = {
        "account": context["account"].get("name"),
        "status": execution.get("status"),
        "trades": context["summary"]["trade_count"],
        "is_test": context.get("is_test", False),
    }
    print(json.dumps(message, ensure_ascii=False))
