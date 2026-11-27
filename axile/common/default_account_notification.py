"""账户默认飞书通知函数的可编辑源码。"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    targets: dict[str, float] | None,
    unit: str,
    error: str | None = None,
    *,
    market: str = "",
    at: str | None = None,
    trigger_source: str = "",
    is_test: bool = False,
    node_change: dict | None = None,
    adjustment: dict | None = None,
    follow_ledger: dict | None = None,
) -> dict[str, object]:
    """生成正式推送和测试发送共用的目标持仓卡片。"""
    status = "调仓信号失败" if error else "调仓信号"
    prefix = "【测试】" if is_test else ""
    timestamp = datetime.fromisoformat(at).astimezone(ZoneInfo("Asia/Shanghai")) if at else None
    trigger = {"manual": "手动生成", "scheduler": "定时生成"}.get(trigger_source)
    subtitle = " · ".join(filter(None, (status, market, trigger)))
    header = {
        "title": {"tag": "plain_text", "content": f"{prefix}{account}"},
        "subtitle": {"tag": "plain_text", "content": subtitle},
        "template": "red" if error else "grey",
    }

    if error or not targets:
        content = error or ("尚未计算目标持仓" if targets is None else "目标空仓")
        elements = [{"tag": "div", "text": {"tag": "plain_text", "content": content}}]
    else:
        quantities = []
        for quantity in targets.values():
            formatted = f"{int(quantity):,}" if quantity == int(quantity) else f"{Decimal(str(quantity)):,f}"
            quantities.append(f"{formatted} {unit}".rstrip())
        elements = [
            {"tag": "markdown", "content": "**目标持仓**", "text_size": "notation"},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "elements": [
                            {"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(targets)}},
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 2,
                        "elements": [
                            {
                                "tag": "markdown",
                                "text_align": "right",
                                "content": "\n".join(quantities),
                            },
                        ],
                    },
                ],
            },
        ]
    if not error:
        sections = []
        order_instructions = adjustment is not None and adjustment.get("kind") == "order"
        adjustment_label = "下单指示" if order_instructions else "待调整"
        for label, comparison in ((adjustment_label, adjustment), ("较上一节点", node_change)):
            if comparison is None:
                continue
            changes = comparison.get("changes")
            if changes and label == "下单指示":
                lines = [
                    f"{symbol}  {'买入' if change['delta'] > 0 else '卖出'} "
                    f"{Decimal(str(abs(change['delta']))).normalize():,f} {unit}"
                    for symbol, change in changes.items()
                ]
                basis = "按结转前信号持仓" if comparison["basis"] == "signal_holdings" else "按生成时的实际持仓"
                content = "\n".join([basis, *lines])
            elif changes:
                lines = [
                    f"{symbol}  {Decimal(str(change['before'])):,f} → {Decimal(str(change['after'])):,f}"
                    f" ({Decimal(str(change['delta'])):+,f} {unit})"
                    for symbol, change in changes.items()
                ]
                content = "\n".join(lines)
            elif comparison["status"] == "compared":
                content = {"较上一节点": "无变化", "待调整": "已对齐目标", "下单指示": "无需下单"}[label]
            elif label == "下单指示":
                content = (
                    "持仓未知，无法生成下单指示"
                    if comparison["status"] == "unknown"
                    else "含空头持仓，无法生成 A 股下单指示"
                )
            else:
                content = {"first": "首次生成", "missing": "上一节点无结果", "unknown": "实仓未知"}[
                    comparison["status"]
                ]
            sections.extend(
                [
                    {"tag": "markdown", "content": f"**{label}**", "text_size": "notation"},
                    {"tag": "div", "text": {"tag": "plain_text", "content": content}},
                ]
            )
        elements = sections + elements
    if follow_ledger:
        label = {
            "settled": "跟随已结转",
            "insufficient_cash": "资金不足，本次未跟随",
            "long_only": "目标含空头，本次未跟随",
        }[follow_ledger["status"]]
        content = (
            f"{label}\n权益 ¥{follow_ledger['equity']:,.2f} · 可用资金 ¥{follow_ledger['cash']:,.2f}"
            f"\n手续费 ¥{follow_ledger['fee']:,.2f}"
        )
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": content}})
    if timestamp:
        elements.append({"tag": "markdown", "content": timestamp.strftime("%Y-%m-%d %H:%M"), "text_size": "notation"})
    return {
        "schema": "2.0",
        "config": {"width_mode": "compact"},
        "header": header,
        "body": {"vertical_spacing": "8px", "elements": elements},
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
        market=context["account"]["market"],
        at=context.get("scheduled_at") or context["execution"]["notified_at"],
        trigger_source=context["execution"]["trigger_source"],
        is_test=context["is_test"],
        node_change=context.get("node_change"),
        adjustment=context.get("adjustment"),
        follow_ledger=context.get("follow_ledger"),
    )
    push_feishu_card(card, key)
"""
