"""业务列投影和稀疏的异常证据，不保存 JSON 表示或字段顺序。"""

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy

from axile.server.business_facts.paths import join_pointer, split_pointer

__all__ = ["join_pointer", "read_columns", "split_pointer", "store_columns"]


def _valid(value: object, kind: str) -> bool:
    if value is None or kind == "json":
        return True
    if kind == "identity":
        return isinstance(value, str) or type(value) is int
    if kind == "text":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return type(value) is int and -(2**63) <= value < 2**63
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and float(value) == value
    except OverflowError:
        return False


def audit_value(value: object) -> object:
    """异常非有限数作为明确证据值保存，不让 SQLite 把它吞成 NULL。"""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_number": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, Mapping):
        return {key: audit_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [audit_value(item) for item in value]
    return value


def store_columns(
    payload: Mapping[str, object], specs: Mapping[str, str], *, child_keys: Sequence[str] = ()
) -> tuple[dict[str, object], dict[str, object]]:
    """读取固定业务列，无法解释的历史字段单独进入审计证据。"""
    columns: dict[str, object] = {}
    invalid: dict[str, object] = {}
    for name, kind in specs.items():
        value = payload.get(name)
        if _valid(value, kind):
            columns[name] = audit_value(value) if kind in {"json", "identity"} else value
        else:
            columns[name] = None
            invalid[name] = audit_value(value)
    unknown = {
        key: value
        for key, value in payload.items()
        if key not in specs and key not in child_keys and key != "audit_evidence"
    }
    existing = payload.get("audit_evidence")
    evidence: dict[str, object] = deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
    if invalid:
        previous = evidence.get("invalid_fields", {})
        if not isinstance(previous, Mapping):
            raise ValueError("Invalid audit evidence fields")
        evidence["invalid_fields"] = {**previous, **invalid}
    if unknown:
        previous = evidence.get("unrecognized_fields", {})
        if not isinstance(previous, Mapping):
            raise ValueError("Invalid unrecognized audit fields")
        evidence["unrecognized_fields"] = {**previous, **{key: audit_value(value) for key, value in unknown.items()}}
    return columns, evidence


def read_columns(
    columns: Mapping[str, object], evidence: Mapping[str, object] | None, specs: Mapping[str, str]
) -> dict[str, object]:
    """返回固定业务视图；异常值不伪装成可用业务字段。"""
    result = {name: columns[name] for name in specs if columns[name] is not None or len(specs) == 1}
    if evidence:
        result["audit_evidence"] = deepcopy(dict(evidence))
    return result
