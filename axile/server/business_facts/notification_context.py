"""冻结通知上下文的无损 outbox 引用；业务子树只保存在原通知中。"""

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import sqlalchemy as sa
from sqlmodel import col

from axile.server.business_facts.extensions import execution_extension
from axile.server.db.models.supplement import NotificationEvent

_MARKER = "$axile_notification_context"
_ROOT_METADATA = frozenset({"event_id", "event_type"})
_EXECUTION_METADATA = frozenset(
    {
        "id",
        "kind",
        "trigger_source",
        "notified_at",
        "execution_time",
        "outcome_reason",
        "channel_type",
        "status",
        "success",
        "outcome",
        "reason_code",
        "error",
        "is_test",
        "supplements",
    }
)
_SUPPLEMENT_METADATA = frozenset(
    {
        "group_id",
        "base_scheduled_at",
        "scheduled_at",
        "index",
        "configured_count",
        "effective_count",
        "is_last",
        "group_status",
        "end_reason",
    }
)
_REFERENCE_FIELDS = frozenset(
    {
        _MARKER,
        "event_id",
        "event_sha256",
        "capture_sha256",
        "root_order",
        "root_metadata",
        "execution_state",
        "execution_order",
        "execution_metadata",
    }
)


def _wire(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_wire(value)).hexdigest()


def is_notification_context_reference(value: object) -> bool:
    """识别保留的引用命名空间；未知版本同样交给严格解析报错。"""
    return isinstance(value, Mapping) and _MARKER in value


def _scalar(value: object) -> bool:
    return value is None or type(value) in (str, int, float, bool)


def _valid_execution_metadata(values: Mapping[str, object]) -> bool:
    for key, value in values.items():
        if key not in _EXECUTION_METADATA:
            return False
        if key != "supplements":
            if not _scalar(value):
                return False
        elif not isinstance(value, list) or any(
            not isinstance(item, dict)
            or not set(item).issubset(_SUPPLEMENT_METADATA)
            or not all(_scalar(field) for field in item.values())
            for item in value
        ):
            return False
    return True


def _business_context(context: Mapping[str, object]) -> dict[str, object] | None:
    result = {key: value for key, value in context.items() if key not in _ROOT_METADATA and key != "execution"}
    execution = context.get("execution")
    if isinstance(execution, Mapping):
        result["execution"] = {key: value for key, value in execution.items() if key not in _EXECUTION_METADATA}
    elif execution is not None:
        return None
    else:
        result["execution"] = {}
    return result


def _ordered_mapping(values: dict[str, Any], order: object) -> dict[str, Any]:
    if (
        not isinstance(order, list)
        or not all(isinstance(key, str) for key in order)
        or len(order) != len(set(order))
        or set(order) != set(values)
    ):
        raise ValueError("冻结通知引用的字段顺序无效")
    return {key: values[key] for key in order}


def compact_captured_context(
    captured: Mapping[str, object], event_context: Mapping[str, object], *, event_id: str
) -> dict[str, object] | None:
    """仅业务子树严格一致时引用 outbox，差异只允许受限的通知元数据。"""
    if is_notification_context_reference(captured):
        return None
    business = _business_context(captured)
    event_business = _business_context(event_context)
    if business is None or event_business is None:
        return None
    root_metadata = {key: value for key, value in captured.items() if key in _ROOT_METADATA}
    if not all(_scalar(value) for value in root_metadata.values()):
        return None
    execution = captured.get("execution")
    execution_metadata = (
        {key: value for key, value in execution.items() if key in _EXECUTION_METADATA}
        if isinstance(execution, Mapping)
        else {}
    )
    if not _valid_execution_metadata(execution_metadata):
        return None
    try:
        if _wire(business) != _wire(event_business):
            return None
        reference = {
            _MARKER: 1,
            "event_id": event_id,
            "event_sha256": _digest(event_context),
            "capture_sha256": _digest(captured),
            "root_order": list(captured),
            "root_metadata": deepcopy(root_metadata),
            "execution_state": "object"
            if isinstance(execution, Mapping)
            else "null"
            if "execution" in captured
            else "absent",
            "execution_order": list(execution) if isinstance(execution, Mapping) else [],
            "execution_metadata": deepcopy(execution_metadata),
        }
        if _wire(hydrate_captured_context(reference, event_id=event_id, event_context=event_context)) != _wire(
            captured
        ):
            return None
    except (TypeError, ValueError):
        return None
    return reference


def hydrate_captured_context(
    reference: Mapping[str, object], *, event_id: str, event_context: Mapping[str, object]
) -> dict[str, Any]:
    """验证冻结通知身份与哈希，恢复原始捕获上下文的值、缺失状态和顺序。"""
    if (
        set(reference) != _REFERENCE_FIELDS
        or type(reference.get(_MARKER)) is not int
        or reference.get(_MARKER) != 1
        or reference.get("event_id") != event_id
        or reference.get("event_sha256") != _digest(event_context)
    ):
        raise ValueError("冻结通知引用无效或原通知已变化")
    root_metadata = reference.get("root_metadata")
    execution_metadata = reference.get("execution_metadata")
    if (
        not isinstance(root_metadata, Mapping)
        or not set(root_metadata).issubset(_ROOT_METADATA)
        or not all(_scalar(value) for value in root_metadata.values())
        or not isinstance(execution_metadata, Mapping)
        or not _valid_execution_metadata(execution_metadata)
    ):
        raise ValueError("冻结通知引用包含非元数据覆盖")
    context = deepcopy(dict(event_context))
    for key in _ROOT_METADATA:
        context.pop(key, None)
    context.update(deepcopy(dict(root_metadata)))
    state = reference.get("execution_state")
    if state == "object":
        execution = context.get("execution")
        if not isinstance(execution, dict):
            raise ValueError("冻结通知引用缺少执行元数据")
        for key in _EXECUTION_METADATA:
            execution.pop(key, None)
        execution.update(deepcopy(dict(execution_metadata)))
        context["execution"] = _ordered_mapping(execution, reference.get("execution_order"))
    elif state == "null":
        context["execution"] = None
    elif state == "absent":
        context.pop("execution", None)
    else:
        raise ValueError("冻结通知引用执行状态无效")
    restored = _ordered_mapping(context, reference.get("root_order"))
    if reference.get("capture_sha256") != _digest(restored):
        raise ValueError("冻结通知引用不能恢复原捕获快照")
    return restored


async def hydrate_result_notification_context(
    session: Any, result: dict[str, object], *, account_id: int, execution_id: str | None
) -> dict[str, object]:
    """唯一业务冷读取显式解引用；缺失、跨账户或变化的 outbox 直接报错。"""
    extra = result.get("extra")
    captured = extra.get("server_notification_context") if isinstance(extra, dict) else None
    if not is_notification_context_reference(captured):
        return result
    if not isinstance(extra, dict) or not isinstance(captured, Mapping) or execution_id is None:
        raise ValueError("冻结通知引用缺少执行身份")
    event_id = f"execution:{execution_id}"
    event = await session.scalar(sa.select(NotificationEvent).where(col(NotificationEvent.id) == event_id))
    if event is None or event.account_id != account_id or event.event_type != "execution.finished":
        raise ValueError("冻结通知引用不存在或属于其他账户")
    restored = hydrate_captured_context(captured, event_id=event_id, event_context=event.context)
    return {**result, "extra": {**extra, "server_notification_context": restored}}


def hydrate_result_notification_context_sync(
    connection: Any, result: dict[str, object], *, account_id: int, execution_id: str | None
) -> dict[str, object]:
    """同步升级、降级和私有冷读取复用相同冻结引用与所有权校验。"""
    extra = result.get("extra")
    captured = extra.get("server_notification_context") if isinstance(extra, dict) else None
    if not is_notification_context_reference(captured):
        return result
    if not isinstance(extra, dict) or not isinstance(captured, Mapping) or execution_id is None:
        raise ValueError("冻结通知引用缺少执行身份")
    event_id = f"execution:{execution_id}"
    table: sa.Table = getattr(NotificationEvent, "__table__")
    event = connection.execute(sa.select(table).where(table.c.id == event_id)).mappings().first()
    if event is None or event["account_id"] != account_id or event["event_type"] != "execution.finished":
        raise ValueError("冻结通知引用不存在或属于其他账户")
    restored = hydrate_captured_context(captured, event_id=event_id, event_context=event["context"])
    return {**result, "extra": {**extra, "server_notification_context": restored}}


async def compact_record_notification_context(session: Any, record: Any, event: NotificationEvent) -> bool:
    """在 outbox 原事务内去重并重建失效的分析投影；不提交、不修改通知。"""
    if record.id is None or record.execution_id is None:
        return False
    if (
        event.account_id != record.account_id
        or event.id != f"execution:{record.execution_id}"
        or event.event_type != "execution.finished"
    ):
        raise ValueError("冻结通知与执行记录身份不一致")
    extra = await session.scalar(
        sa.select(execution_extension.c.extra).where(execution_extension.c.record_id == record.id)
    )
    captured = extra.get("server_notification_context") if isinstance(extra, dict) else None
    if not isinstance(captured, Mapping):
        return False
    reference = compact_captured_context(captured, event.context, event_id=event.id)
    if reference is None:
        return False
    await session.execute(
        execution_extension.update()
        .where(execution_extension.c.record_id == record.id)
        .values(extra={**extra, "server_notification_context": reference})
    )
    return True
