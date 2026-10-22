"""执行终态的类型化字段及终止证据转换。"""

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.domain.execution import ExecutionTaskStatus
from axile.executor.session_closed import session_closed_scope
from axile.server.business_facts.columns import audit_value, read_columns, store_columns
from axile.server.db.models.execution import ExecuteRecord
from axile.server.execution.legacy_compat import normalize_legacy_result

HEAD_FIELDS = {
    "symbol": "text",
    "technical_detail": "text",
    "status": "text",
    "task_status": "text",
    "error": "text",
    "outcome": "text",
    "outcome_reason": "text",
    "reason_code": "text",
    "msg": "text",
    "execution_kind": "text",
    "channel_type": "text",
    "execution_time": "number",
    "success": "boolean",
}
TERMINATION_FIELDS = {
    "reason": "text",
    "mode": "text",
    "trigger": "text",
    "requested_at": "text",
    "acked_at": "text",
    "finished_at": "text",
    "cancel_attempted": "boolean",
    "forced": "boolean",
    "cancel_unconfirmed": "boolean",
}
execution_head: sa.Table = getattr(ExecuteRecord, "__table__")
termination_cancel_failure = sa.Table(
    "execution_termination_cancel_failure",
    SQLModel.metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("order_id", sa.JSON),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
)
TABLES = (execution_head, termination_cancel_failure)


def _container_kind(payload: Mapping[str, object], key: str, expected: type) -> str:
    if key not in payload:
        return "absent"
    value = payload[key]
    if value is None:
        return "null"
    return "value" if isinstance(value, expected) else "invalid"


def _terminal_task_status(payload: Mapping[str, object], is_success: int) -> str:
    if payload.get("outcome") == "terminated":
        return ExecutionTaskStatus.TERMINATED.value
    if is_success == 1:
        return ExecutionTaskStatus.SUCCEEDED.value
    try:
        scope = session_closed_scope(dict(payload))
    except (TypeError, ValueError):
        scope = None
    if scope == "all":
        return ExecutionTaskStatus.SKIPPED.value
    if scope == "partial":
        return ExecutionTaskStatus.PARTIAL.value
    return ExecutionTaskStatus.FAILED.value


def execution_head_values(payload: Mapping[str, object], *, is_success: int | None = None) -> dict[str, Any]:
    """转换紧凑终态列，保留原字段存在性，供实时写入与历史迁移共用。"""
    fields, metadata = store_columns(
        payload, HEAD_FIELDS, child_keys=tuple(key for key in payload if key not in HEAD_FIELDS)
    )
    values: dict[str, Any] = {key: fields.get(key) for key in HEAD_FIELDS}
    try:
        normalized = normalize_legacy_result(dict(payload))
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        # 历史异常字段已保留为审计证据；展示摘要不反向阻断证据迁移。
        normalized = fields
    if not isinstance(normalized, Mapping):
        normalized = fields
    for name in ("status", "error", "outcome", "outcome_reason"):
        value = normalized.get(name)
        values["display_" + name] = value if isinstance(value, str) else None
    values["result_input_kind"] = _container_kind(payload, "inputs", Mapping)
    values["result_input_value"] = (
        audit_value(payload.get("inputs")) if values["result_input_kind"] == "invalid" else None
    )
    values["audit_evidence"] = metadata
    if "task_status" not in payload and is_success is not None:
        values["task_status"] = _terminal_task_status(payload, is_success)
    kind = _container_kind(payload, "termination", Mapping)
    values["termination_kind"] = kind
    values["termination_cancel_ids_kind"] = "absent"
    values["termination_cancel_count"] = None
    values.update({"termination_" + key: None for key in TERMINATION_FIELDS})
    term_metadata: dict[str, Any] = {}
    termination = payload.get("termination")
    if kind == "value" and isinstance(termination, Mapping):
        typed, term_metadata = store_columns(
            termination,
            TERMINATION_FIELDS,
            child_keys=("cancel_failed_order_ids",),
        )
        values.update({"termination_" + key: typed.get(key) for key in TERMINATION_FIELDS})
        ids_kind = _container_kind(termination, "cancel_failed_order_ids", list)
        values["termination_cancel_ids_kind"] = ids_kind
        ids = termination.get("cancel_failed_order_ids")
        if isinstance(ids, list):
            values["termination_cancel_count"] = len(ids)
        if ids_kind == "invalid":
            term_metadata["cancel_ids_value"] = termination["cancel_failed_order_ids"]
    elif kind == "invalid":
        term_metadata["value"] = termination
    values["termination_evidence"] = term_metadata
    return values


def encode_head(
    record_id: int, payload: Mapping[str, object], *, is_success: int | None = None
) -> dict[sa.Table, list[dict[str, object]]]:
    """生成执行主表更新值与有序撤单失败明细。"""
    result = {execution_head: [{"id": record_id, **execution_head_values(payload, is_success=is_success)}]}
    cancellation_rows = []
    termination = payload.get("termination")
    ids = termination.get("cancel_failed_order_ids") if isinstance(termination, Mapping) else None
    if isinstance(ids, list):
        for ordinal, order_id in enumerate(ids):
            fields, metadata = store_columns({"order_id": order_id}, {"order_id": "identity"})
            cancellation_rows.append({"record_id": record_id, "ordinal": ordinal, **fields, "audit_evidence": metadata})
    result[termination_cancel_failure] = cancellation_rows
    return result


def _metadata(row: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = row.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"执行字段元数据无效: {name}")
    return value


def _ordinal(row: Mapping[str, object]) -> int:
    ordinal = row["ordinal"]
    if type(ordinal) is not int:
        raise ValueError("执行撤单失败明细序号无效")
    return ordinal


def decode_head(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> dict[str, object]:
    """只为显式明细读取还原结果主字段及终止证据。"""
    head_rows = rows.get(execution_head.name, ())
    if not head_rows:
        raise ValueError("执行主记录不存在")
    row = head_rows[0]
    result = read_columns(row, _metadata(row, "audit_evidence"), HEAD_FIELDS)
    kind = row["termination_kind"]
    if kind == "absent":
        return result
    metadata = _metadata(row, "termination_evidence")
    if kind == "null":
        result["termination"] = None
    elif kind == "invalid":
        result["termination"] = metadata["value"]
    else:
        typed = {key: row.get("termination_" + key) for key in TERMINATION_FIELDS}
        termination = read_columns(typed, metadata, TERMINATION_FIELDS)
        ids_kind = row["termination_cancel_ids_kind"]
        if ids_kind == "value":
            ids = sorted(rows.get(termination_cancel_failure.name, ()), key=_ordinal)
            if len(ids) != row["termination_cancel_count"] or [_ordinal(item) for item in ids] != list(range(len(ids))):
                raise ValueError("Incomplete cancellation failure facts")
            termination["cancel_failed_order_ids"] = [
                read_columns(item, _metadata(item, "audit_evidence"), {"order_id": "identity"})["order_id"]
                for item in ids
            ]
            invalid_ids = {}
            for item in ids:
                invalid = _metadata(item, "audit_evidence").get("invalid_fields")
                if isinstance(invalid, Mapping) and "order_id" in invalid:
                    invalid_ids[str(item["ordinal"])] = invalid["order_id"]
            if invalid_ids:
                issues = termination.get("audit_evidence", {})
                if not isinstance(issues, dict):
                    raise ValueError("Invalid termination audit evidence")
                issues["invalid_cancel_order_ids"] = invalid_ids
                termination["audit_evidence"] = issues
        elif ids_kind == "null":
            termination["cancel_failed_order_ids"] = None
        elif ids_kind == "invalid":
            termination["cancel_failed_order_ids"] = metadata["cancel_ids_value"]
        result["termination"] = termination
    return result
