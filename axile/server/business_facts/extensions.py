"""执行结果中具有明确归属的动态扩展，不保存完整业务文档副本。"""

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.business_facts.columns import read_columns, store_columns

EXTENSION_FIELDS = {"memory": "json", "extra": "json", "reconciliation": "json"}
# 所有已知业务子域均由其负责人保存；此表只保存其余根字段。
DOMAIN_FIELDS = (
    "status",
    "symbol",
    "technical_detail",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
    "channel_type",
    "execution_time",
    "success",
    "termination",
    "inputs",
    "account_assets",
    "symbol_results",
    "orders",
    "trades",
    "first_ticks",
    "target_volume",
)

execution_extension = sa.Table(
    "execution_extension",
    SQLModel.metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("memory", sa.JSON),
    sa.Column("extra", sa.JSON),
    sa.Column("reconciliation", sa.JSON),
    sa.Column("root_kind", sa.Text, nullable=False, server_default="object"),
    sa.Column("root_value", sa.JSON),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
)
TABLES = (execution_extension,)


def sanitize_input(payload: Mapping[str, object]) -> dict[str, object]:
    """
    显式复用审计输入的脱敏边界，不改变调用方原对象。

    延迟导入使纯编解码模块可被执行记录写入端使用，避免循环导入。
    通知所需的受限配置应由通知服务独立提供，不从审计证据恢复。
    """
    from axile.server.execution.execution_records_output import sanitize_standard_input_for_audit

    return sanitize_standard_input_for_audit(dict(payload))


def encode_extensions(record_id: int, payload: Mapping[str, object]) -> dict[sa.Table, list[dict[str, Any]]]:
    """只保存根部动态内容与未归属字段，已知子域不进入残余 JSON。"""
    columns, metadata = store_columns(payload, EXTENSION_FIELDS, child_keys=DOMAIN_FIELDS)
    return {
        execution_extension: [
            {"record_id": record_id, **columns, "root_kind": "object", "root_value": None, "audit_evidence": metadata}
        ]
    }


def encode_result_root_state(record_id: int, payload: object) -> dict[sa.Table, list[dict[str, Any]]]:
    """显式保存旧结果的空值、标量或空数组；不以残余 JSON 隐藏完整文档。"""
    if isinstance(payload, Mapping):
        return encode_extensions(record_id, payload)
    if payload is None:
        kind = "null"
    elif isinstance(payload, (str, bool)) or type(payload) in (int, float):
        kind = "scalar"
    elif isinstance(payload, list) and not payload:
        kind = "empty_list"
    else:
        raise ValueError("Unsupported historical result root; expected object, null, scalar or empty list")
    columns, metadata = store_columns({"root_value": payload}, {"root_value": "json"})
    return {
        execution_extension: [
            {
                "record_id": record_id,
                "memory": None,
                "extra": None,
                "reconciliation": None,
                "root_kind": kind,
                **columns,
                "audit_evidence": metadata,
            }
        ]
    }


def _extension_row(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> Mapping[str, object]:
    values = rows.get(execution_extension.name, ())
    if len(values) != 1:
        raise ValueError("Execution extension requires exactly one record")
    return values[0]


def decode_result_root_state(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> tuple[str, object]:
    """返回文档状态与异常根值；正常对象由各业务域继续组装。"""
    row = _extension_row(rows)
    kind = row.get("root_kind")
    if kind == "object":
        return "object", None
    if kind not in ("null", "scalar", "empty_list"):
        raise ValueError("Execution result root state is missing or invalid")
    metadata = row.get("audit_evidence")
    if not isinstance(metadata, Mapping):
        raise ValueError("Execution result root metadata is missing")
    value = read_columns(row, metadata, {"root_value": "json"})
    if "root_value" not in value:
        raise ValueError("Execution result root value is missing")
    root_value = value["root_value"]
    valid = (
        (kind == "null" and root_value is None)
        or (kind == "empty_list" and isinstance(root_value, list) and not root_value)
        or (
            kind == "scalar"
            and (
                isinstance(root_value, (str, bool))
                or type(root_value) in (int, float)
                or isinstance(root_value, Mapping)
                and set(root_value) == {"invalid_number"}
            )
        )
    )
    if not valid:
        raise ValueError("Execution result root value disagrees with its state")
    return kind, root_value


def decode_extensions(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> dict[str, object]:
    """恢复根部扩展片段；不存在记录时不制造空扩展。"""
    values = rows.get(execution_extension.name, ())
    if not values:
        return {}
    row = _extension_row(rows)
    if row.get("root_kind") != "object":
        return {}
    metadata = row.get("audit_evidence")
    if not isinstance(metadata, Mapping):
        raise ValueError("Execution extension metadata is missing")
    return read_columns(row, metadata, EXTENSION_FIELDS)
