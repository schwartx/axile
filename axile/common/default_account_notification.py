"""账户默认飞书通知函数的可编辑源码。"""

DEFAULT_ACCOUNT_NOTIFICATION_CODE = '''import os

from axile.common.feishu import push_feishu_card
from axile.common.notification_context import AccountNotificationContext


def notify(context: AccountNotificationContext) -> None:
    """发送与内置默认通知一致的飞书卡片。"""
    key = os.environ.get("AXILE_ACCOUNT_FEISHU_KEY")
    if not key:
        return

    if context.get("event_type") == "supplement.cancelled":
        group = context["supplement"]
        card = {
            "header": {"title": {"tag": "plain_text", "content": "剩余补发已取消"}},
            "elements": [{"tag": "markdown", "content": f"账户：{context['account'].get('name', '')}\\n原因：{group['reason']}\\n请求时间：{group['cancel_requested_at']}"}],
        }
        push_feishu_card(card, key, timeout=10)
        return

    # 保留原默认卡片的口径；可以在这里修改字段、持仓或成交的展示。
    defaults = context["default_feishu_variables"]
    variables = {
        "account_mark": defaults["account_mark"],
        "dt": defaults["dt"],
        "algorithm": defaults["algorithm"],
        "total_assets": defaults["total_assets"],
        "available_cash": defaults["available_cash"],
        "market_value": defaults["market_value"],
        "positions": defaults["positions"],
        "trades": defaults["trades"],
    }
    card: dict[str, object] = {
        "type": "template",
        "data": {
            "template_id": "AAqRUQhyOM90g",
            "template_variable": variables,
        },
    }
    push_feishu_card(card, key, timeout=10)
'''


def build_signal_feishu_card(
    account: str,
    targets: dict[str, float],
    unit: str,
    error: str | None = None,
) -> dict[str, object]:
    """按目标清单生成信号卡片，保留完整数量精度。"""
    rows = [
        f"{symbol}  {int(quantity) if quantity == int(quantity) else quantity} {unit}".rstrip()
        for symbol, quantity in targets.items()
    ]
    content = error or "\n".join(rows) or "目标空仓"
    return {
        "header": {"title": {"tag": "plain_text", "content": f"调仓信号 · {account}"}},
        "elements": [{"tag": "markdown", "content": f"**目标持仓**\n{content}"}],
    }


SIGNAL_NOTIFICATION_CODE = """import os

from axile.common.default_account_notification import build_signal_feishu_card
from axile.common.feishu import push_feishu_card


def notify(context):
    key = os.environ.get("AXILE_ACCOUNT_FEISHU_KEY")
    if not key:
        return
    card = build_signal_feishu_card(
        context["account"]["name"],
        context["targets"]["current"],
        context["quantity_unit"],
        context["execution"]["error"],
    )
    push_feishu_card(card, key)
"""
