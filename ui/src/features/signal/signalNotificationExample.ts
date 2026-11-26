export const SIGNAL_NOTIFICATION_EXAMPLE = `import json
import os
from urllib import request

from axile.common.notification_context import AccountNotificationContext


def notify(context: AccountNotificationContext) -> None:
    """把调仓结果推送到自有 Webhook。"""
    if context.get("is_test") or context.get("event_type") != "signal.calculated":
        return

    webhook = os.environ.get("AXILE_NOTIFY_WEBHOOK")
    if not webhook:
        raise RuntimeError("请在 Axile 服务环境中配置 AXILE_NOTIFY_WEBHOOK")

    execution = context["execution"]
    assert execution is not None
    message: dict[str, object] = {
        "account": context["account"]["name"],
        "status": execution["status"],
        "quantity_unit": context.get("quantity_unit"),
    }
    if execution["success"]:
        message["targets"] = context["targets"]["current"]
    else:
        message["error"] = execution["error"]

    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    req = request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=5) as response:
        response.read()
`
