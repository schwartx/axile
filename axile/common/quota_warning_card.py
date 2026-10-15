"""默认发送和自定义函数模板共用的飞书额度预警卡片。"""


def build_quota_warning_card(context: dict) -> dict:
    """展示触发瞬间的额度事实，明确计数来源与预警语义。"""
    quota = context["quota"]
    account = context.get("account") or {}
    operation = "报单" if quota["operation"] == "place_order" else "撤单"
    title = "系统告警试跑" if context.get("is_test") else "账户额度预警"
    content = (
        f"**账户：** {account.get('name', '未指定')}\n"
        f"**操作：** {operation}\n"
        f"**已用：** {quota['used']} / {quota['limit']} 次\n"
        f"**剩余：** {quota['remaining']} 次\n"
        f"**预警线：** {quota['warning_percent']}%（{quota['threshold_count']} 次）\n"
        f"**统计日期：** {quota['control_date']}（{quota['timezone']}）\n"
        f"**触发时间：** {context['occurred_at']}\n"
        f"**执行：** {context.get('execution_id') or '无'}\n"
        "**口径：** Axile 本地流控放行次数，不代表柜台受理或成交次数。\n"
        "本消息仅提醒用量，实际请求继续按现有额度规则处理。"
    )
    return {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "orange"},
        "elements": [{"tag": "markdown", "content": content}],
    }
