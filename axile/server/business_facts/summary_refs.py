"""审计引用的公共身份校验与异常类型。"""

from __future__ import annotations

from collections.abc import Mapping

REFERENCE_KEY = "business_facts_ref"


class BusinessFactReferenceError(ValueError):
    """审计引用缺失、越权或与冻结事实不一致。"""


def validate_summary_reference(reference: Mapping[str, object]) -> None:
    """只校验不可变执行身份与业务视图版本。"""
    record_id = reference.get("record_id")
    if not isinstance(record_id, int) or isinstance(record_id, bool) or record_id < 1:
        raise BusinessFactReferenceError("摘要引用缺少终态记录身份")
    if type(reference.get("projection_version")) is not int or reference["projection_version"] != 2:
        raise BusinessFactReferenceError("摘要业务视图版本无效")
