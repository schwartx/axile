"""系统告警函数模板；凭据在运行时注入，模板本身不携带 Key。"""

DEFAULT_SYSTEM_NOTIFICATION_CODE = '''import json
import os
from urllib.request import Request, urlopen

from axile.common.notification_context import SystemNotificationContext
from axile.common.quota_warning_card import build_quota_warning_card


def notify(context: SystemNotificationContext) -> None:
    """将执行异常、超时或额度预警发送到飞书。"""
    key = os.environ.get("AXILE_SYSTEM_FEISHU_KEY", "")
    if not key:
        raise ValueError("请先配置系统飞书 Webhook")
    account = context["account"]
    name = account["name"] if account else "未指定"
    title = "系统告警试跑" if context["is_test"] else "系统执行告警"
    if context["event_type"] == "account_control.warning":
        card = build_quota_warning_card(context)
    else:
        error = context["error"]
        assert error is not None
        card = {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "orange"},
            "elements": [{"tag": "markdown", "content": (
                f"**事件：** {context['event_type']}\\n"
                f"**时间：** {context['occurred_at']}\\n"
                f"**账户：** {name}\\n"
                f"**执行：** {context['execution_id'] or '无'}\\n"
                f"**错误：** {error['type']}: {error['message']}\\n"
                f"```\\n{error['traceback']}\\n```"
            )}],
        }
    request = Request(
        f"https://open.feishu.cn/open-apis/bot/v2/hook/{key}",
        data=json.dumps({"msg_type": "interactive", "card": card}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=8) as response:
        result = json.load(response)
    if result.get("code") != 0 and result.get("StatusMessage") != "success":
        raise RuntimeError(f"飞书返回失败：{result.get('msg') or result.get('StatusMessage')}")
'''
