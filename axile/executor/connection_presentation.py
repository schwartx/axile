"""渠道无关的连接问题展示契约；内容不参与连接或执行判断。"""

from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


class ConnectionFact(BaseModel):
    """本次观察已确认的键值事实。"""

    model_config = ConfigDict(strict=True)
    label: str
    value: str


class ConnectionSection(BaseModel):
    """有序信息块，可组合说明、事实与检查步骤。"""

    model_config = ConfigDict(strict=True)
    title: str | None = None
    text: str | None = None
    fields: list[ConnectionFact] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)


class ConnectionAction(BaseModel):
    """通用 UI 操作意图，不包含路径、脚本或接口请求。"""

    model_config = ConfigDict(strict=True)
    label: str
    intent: str


class ConnectionPresentation(BaseModel):
    """渠道提供的动态故障内容，仅在详情接口返回。"""

    model_config = ConfigDict(strict=True)
    title: str | None = None
    summary: str | None = None
    sections: list[ConnectionSection] = Field(default_factory=list)
    actions: list[ConnectionAction] = Field(default_factory=list)


PresentationInput = ConnectionPresentation | Mapping[str, object] | Callable[[], object] | None


def presentation_snapshot(value: object) -> dict[str, object] | None:
    """构造或校验失败只丢弃展示内容；序列化生成独立快照。"""
    try:
        content = value() if callable(value) else value
        if content is None:
            return None
        model = ConnectionPresentation.model_validate(content)
        if not (
            (model.title and model.title.strip())
            or (model.summary and model.summary.strip())
            or any(section.text or section.fields or section.steps for section in model.sections)
            or any(action.label.strip() and action.intent.strip() for action in model.actions)
        ):
            return None
        return model.model_dump(mode="json", exclude_none=True)
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
