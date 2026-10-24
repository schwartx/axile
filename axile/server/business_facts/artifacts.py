"""用不可变业务事实身份引用审计附件，显式解析后仍保留原始引用。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from axile.server.business_facts.summary_refs import (
    REFERENCE_KEY,
    BusinessFactReferenceError,
)

ArtifactView = Literal["input", "targets", "rules", "assets_before", "assets_after"]
_VIEWS = {"input", "targets", "rules", "assets_before", "assets_after"}
_TARGET_FIELDS = ("curr_target", "last_target", "strategy_target", "sizing_context", "plan", "audit_evidence")
_RULE_FIELDS = ("trade_rules", "futures_close_policy")


def make_artifact_reference(
    *,
    view: ArtifactView,
    input_id: str | None = None,
    asset_id: str | None = None,
    select_fields: list[str] | None = None,
) -> dict[str, object]:
    """构造版本固定的引用；身份必须来自已经持久化的事实。"""
    reference: dict[str, object] = {"version": 1, "view": view}
    if input_id is not None:
        reference["input_id"] = input_id
    if asset_id is not None:
        reference["asset_id"] = asset_id
    if select_fields is not None:
        reference["select_fields"] = list(select_fields)
    parse_artifact_reference({REFERENCE_KEY: reference})
    return {REFERENCE_KEY: reference}


def parse_artifact_reference(content: object) -> Mapping[str, object] | None:
    """历史正文原样保留；显式新引用损坏时直接报错。"""
    if not isinstance(content, Mapping) or REFERENCE_KEY not in content:
        return None
    reference = content[REFERENCE_KEY]
    if not isinstance(reference, Mapping):
        raise BusinessFactReferenceError("审计业务事实引用不是对象")
    view = reference.get("view")
    if (
        type(reference.get("version")) is not int
        or reference["version"] != 1
        or not isinstance(view, str)
        or view not in _VIEWS
    ):
        raise BusinessFactReferenceError("审计业务事实引用版本或视图无效")
    _validate_select_fields(reference, view)
    for key in ("input_id", "asset_id"):
        value = reference.get(key)
        if key in reference and (not isinstance(value, str) or not value):
            raise BusinessFactReferenceError("审计业务事实引用身份无效")
    required = "asset_id" if view in {"assets_before", "assets_after"} else "input_id"
    if required not in reference:
        raise BusinessFactReferenceError("审计业务事实引用缺少不可变身份")
    return reference


def _validate_select_fields(reference: Mapping[str, object], view: str) -> None:
    if "select_fields" not in reference:
        return
    selected = reference["select_fields"]
    allowed = _TARGET_FIELDS if view == "targets" else _RULE_FIELDS if view == "rules" else ()
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(key, str) or key not in allowed for key in selected)
        or len(set(selected)) != len(selected)
    ):
        raise BusinessFactReferenceError("审计业务事实引用的字段选择无效")


async def resolve_artifact_content(artifact: Any, *, account_id: int | None = None) -> dict[str, object]:
    """在一次原生只读快照中校验归属并解析附件，保留独立历史证据。"""
    from axile.server._native import _core
    from axile.server.native_store import native_operation

    fields = {
        key: artifact.get(key) if isinstance(artifact, Mapping) else getattr(artifact, key)
        for key in ("execution_id", "artifact_type", "content")
    }
    fields["artifact_type"] = getattr(fields["artifact_type"], "value", fields["artifact_type"])
    try:
        return await native_operation("resolve_execution_artifact", {"artifact": fields, "account_id": account_id})
    except _core.NativeError as error:
        if error.args and error.args[0] == "BUSINESS_FACTS_REFERENCE":
            raise BusinessFactReferenceError(error.args[1]) from None
        raise
