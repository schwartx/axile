"""渠道无关的连接问题展示契约；内容不参与连接或执行判断。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from axile.common.serialization import from_payload, to_payload


@dataclass(kw_only=True)
class ConnectionFact:
    """本次观察已确认的键值事实。"""

    label: str
    value: str


@dataclass(kw_only=True)
class ConnectionSection:
    """有序信息块，可组合说明、事实与检查步骤。"""

    title: str | None = None
    text: str | None = None
    fields: list[ConnectionFact] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ConnectionAction:
    """通用 UI 操作意图，不包含路径、脚本或接口请求。"""

    label: str
    intent: str


@dataclass(kw_only=True)
class ConnectionPresentation:
    """渠道提供的动态故障内容，仅在详情接口返回。"""

    title: str | None = None
    summary: str | None = None
    sections: list[ConnectionSection] = field(default_factory=list)
    actions: list[ConnectionAction] = field(default_factory=list)


PresentationInput = ConnectionPresentation | Mapping[str, object] | Callable[[], object] | None


def presentation_snapshot(value: object) -> dict[str, object] | None:
    """构造或校验失败只丢弃展示内容；序列化生成独立快照。"""
    try:
        content = value() if callable(value) else value
        if content is None:
            return None
        model = from_payload(ConnectionPresentation, content)
        if not (
            (model.title and model.title.strip())
            or (model.summary and model.summary.strip())
            or any(section.text or section.fields or section.steps for section in model.sections)
            or any(action.label.strip() and action.intent.strip() for action in model.actions)
        ):
            return None
        return to_payload(model, mode="json", exclude_none=True)
    except Exception:
        return None


def connection_problem(
    title: str,
    summary: str,
    *,
    facts: Mapping[str, object] | None = None,
    steps: Sequence[str] = (),
    settings: bool = True,
    facts_title: str = "已确认的信息",
    steps_title: str = "建议检查",
) -> dict[str, object]:
    """供渠道组合常见信息块；事实与建议的具体文字由调用方决定。"""
    sections: list[dict[str, object]] = []
    if facts:
        sections.append(
            {
                "title": facts_title,
                "fields": [{"label": key, "value": str(value)} for key, value in facts.items() if value is not None],
            }
        )
    if steps:
        sections.append({"title": steps_title, "steps": list(steps)})
    return {
        "title": title,
        "summary": summary,
        "sections": sections,
        "actions": [{"label": "检查连接设置", "intent": "open_connection_settings"}] if settings else [],
    }
