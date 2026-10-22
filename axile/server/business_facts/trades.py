"""按原始出现位置保存成交事实，保留原生金额、手续费及换算证据。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.business_facts.columns import join_pointer, read_columns, store_columns

_TRADE_FIELDS = {
    "trade_id": "identity",
    "symbol": "text",
    "order_id": "identity",
    "trade_time": "text",
    "trade_volume": "number",
    "trade_price": "number",
    "trade_value": "number",
    "extra": "json",
}
_CONVERSION_FIELDS = {
    "price_currency": "text",
    "value_currency": "text",
    "rate": "number",
    "reference": "json",
}


def _record_column() -> sa.Column:
    """让每条成交证据随其终态执行记录删除。"""
    return sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True)


trade_collection = sa.Table(
    "business_trade_collection",
    SQLModel.metadata,
    _record_column(),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("parent_path", sa.Text, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("map_key", sa.Text),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("member_count", sa.Integer),
    sa.Column("malformed_value", sa.JSON),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
)

trade = sa.Table(
    "business_trade",
    SQLModel.metadata,
    _record_column(),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("collection_path", sa.Text, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("malformed_value", sa.JSON),
    *[sa.Column(name, sa.Text) for name in ("symbol", "trade_time")],
    *[sa.Column(name, sa.JSON) for name in ("trade_id", "order_id")],
    *[sa.Column(name, sa.Float) for name in ("trade_volume", "trade_price", "trade_value")],
    sa.Column("extra", sa.JSON),
    sa.Column("trade_id_state", sa.Text, nullable=False),
    sa.Column("order_id_state", sa.Text, nullable=False),
    sa.Column("conversion_recorded", sa.Boolean, nullable=False),
    sa.Column("fee", sa.Float),
    sa.Column("fee_state", sa.Text, nullable=False),
    sa.Column("fee_currency", sa.Text),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
)

trade_conversion = sa.Table(
    "business_trade_conversion",
    SQLModel.metadata,
    _record_column(),
    sa.Column("trade_path", sa.Text, primary_key=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("malformed_value", sa.JSON),
    sa.Column("price_currency", sa.Text),
    sa.Column("value_currency", sa.Text),
    sa.Column("rate", sa.Float),
    sa.Column("reference", sa.JSON),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
)

TABLES = (trade_collection, trade, trade_conversion)


def _numeric_fee(value: object) -> int | float | None:
    """只投影明确的有限手续费，保留零与负返佣。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
        return value if math.isfinite(number) and number == value else None
    except OverflowError:
        return None


def _malformed_fields(value: object) -> tuple[dict[str, object], dict[str, object]]:
    """异常容器也经过公共编码器，显式保存非有限数值证据。"""
    return store_columns({"malformed_value": value}, {"malformed_value": "json"})


def _decode_malformed(row: Mapping[str, Any]) -> object:
    """恢复异常容器自身，不将异常值当作缺失或空集合。"""
    return read_columns(row, row.get("audit_evidence") or {}, {"malformed_value": "json"})["malformed_value"]


def _conversion_row(record_id: int, path: str, value: object) -> dict[str, object]:
    """保存换算对象的稳定字段，异常旧值单独作为证据保留。"""
    columns, metadata = (
        store_columns(value, _CONVERSION_FIELDS) if isinstance(value, Mapping) else _malformed_fields(value)
    )
    return {
        "record_id": record_id,
        "trade_path": path,
        "state": "object" if isinstance(value, Mapping) else "malformed",
        "malformed_value": columns.get("malformed_value"),
        **{name: columns.get(name) for name in _CONVERSION_FIELDS},
        "audit_evidence": metadata,
    }


def _trade_row(record_id: int, collection_path: str, ordinal: int, value: object) -> dict[str, object]:
    """按数组序号保存一份观察，不合并重复的原生成交编号。"""
    columns, metadata = (
        store_columns(value, _TRADE_FIELDS, child_keys=("value_conversion",))
        if isinstance(value, Mapping)
        else _malformed_fields(value)
    )
    extra = value.get("extra") if isinstance(value, Mapping) else None
    extra = extra if isinstance(extra, Mapping) else {}
    currency = extra.get("commission_asset")
    return {
        "record_id": record_id,
        "path": join_pointer(collection_path, ordinal),
        "collection_path": collection_path,
        "ordinal": ordinal,
        "state": "object" if isinstance(value, Mapping) else "malformed",
        "malformed_value": columns.get("malformed_value"),
        **{name: columns.get(name) for name in _TRADE_FIELDS},
        **{
            key + "_state": "absent"
            if not isinstance(value, Mapping) or key not in value
            else "null"
            if value[key] is None
            else "value"
            if columns[key] is not None
            else "invalid"
            for key in ("trade_id", "order_id")
        },
        "conversion_recorded": isinstance(value, Mapping) and "value_conversion" in value,
        "fee": _numeric_fee(extra.get("commission")),
        "fee_state": "absent"
        if "commission" not in extra
        else "null"
        if extra["commission"] is None
        else "value"
        if _numeric_fee(extra["commission"]) is not None
        else "invalid",
        "fee_currency": currency if isinstance(currency, str) else None,
        "audit_evidence": metadata,
    }


def _append_collection(
    batch: dict[sa.Table, list[dict[str, object]]],
    record_id: int,
    parent: Mapping[str, object],
    parent_path: str,
    ordinal: int,
    map_key: str | None,
) -> None:
    """保存显式出现的成交集合，包括空集合和异常旧集合。"""
    if "trades" not in parent:
        return
    value = parent["trades"]
    path = join_pointer(parent_path, "trades")
    columns, metadata = ({}, {}) if isinstance(value, list) else _malformed_fields(value)
    batch[trade_collection].append(
        {
            "record_id": record_id,
            "path": path,
            "parent_path": parent_path,
            "ordinal": ordinal,
            "map_key": map_key,
            "state": "array" if isinstance(value, list) else "malformed",
            "member_count": len(value) if isinstance(value, list) else None,
            "malformed_value": columns.get("malformed_value"),
            "audit_evidence": metadata,
        }
    )
    if not isinstance(value, list):
        return
    for index, member in enumerate(value):
        row = _trade_row(record_id, path, index, member)
        batch[trade].append(row)
        if isinstance(member, Mapping) and "value_conversion" in member:
            batch[trade_conversion].append(_conversion_row(record_id, str(row["path"]), member["value_conversion"]))


def encode_trades(record_id: int, payload: Mapping[str, object]) -> dict[sa.Table, list[dict[str, object]]]:
    """将顶层与逐品种成交各自映射为结构化事实。"""
    batch: dict[sa.Table, list[dict[str, object]]] = {table: [] for table in TABLES}
    _append_collection(batch, record_id, payload, "", 0, None)
    symbols = payload.get("symbol_results")
    if isinstance(symbols, Mapping):
        for ordinal, (key, value) in enumerate(symbols.items()):
            if isinstance(value, Mapping):
                path = join_pointer("/symbol_results", str(key))
                _append_collection(batch, record_id, value, path, ordinal, str(key))
    return batch


def _decode_trade(row: Mapping[str, Any], conversions: Mapping[str, Mapping[str, Any]]) -> object:
    """重建原始成交观察，金额与时间始终使用已保存值。"""
    if row["state"] != "object":
        return _decode_malformed(row)
    metadata = row.get("audit_evidence") or {}
    result = read_columns(row, metadata, _TRADE_FIELDS)
    result["availability"] = {key: row[key + "_state"] for key in ("trade_id", "order_id")}
    for key in ("trade_id", "order_id"):
        if row[key + "_state"] == "null":
            result[key] = None
    conversion = conversions.get(str(row["path"]))
    if row["conversion_recorded"] and conversion is None:
        raise ValueError("Missing trade conversion facts")
    if conversion is not None:
        value = (
            read_columns(conversion, conversion.get("audit_evidence") or {}, _CONVERSION_FIELDS)
            if conversion["state"] == "object"
            else _decode_malformed(conversion)
        )
        result["value_conversion"] = value
    return dict(result)


def _decode_collection(
    row: Mapping[str, Any],
    members: Mapping[str, Sequence[Mapping[str, Any]]],
    conversions: Mapping[str, Mapping[str, Any]],
) -> object:
    """按原始数组序号重建集合，异常容器保留其原值。"""
    if row["state"] != "array":
        return _decode_malformed(row)
    ordered = sorted(members.get(str(row["path"]), ()), key=lambda member: int(member["ordinal"]))
    if [member["ordinal"] for member in ordered] != list(range(int(row["member_count"]))):
        raise ValueError(f"Incomplete trade collection at {row['path']}")
    return [_decode_trade(member, conversions) for member in ordered]


def decode_trades(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, object]:
    """重建独立成交集合的结果片段，交由仓储层与其他域合并。"""
    members: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows.get(trade.name, ()):
        members.setdefault(str(row["collection_path"]), []).append(row)
    conversions = {str(row["trade_path"]): row for row in rows.get(trade_conversion.name, ())}
    result: dict[str, Any] = {}
    collections = sorted(rows.get(trade_collection.name, ()), key=lambda row: int(row["ordinal"]))
    for row in collections:
        value = _decode_collection(row, members, conversions)
        key = row.get("map_key")
        if key is None:
            result["trades"] = value
        else:
            result.setdefault("symbol_results", {})[str(key)] = {"trades": value}
    return result
