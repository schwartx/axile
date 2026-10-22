"""按来源与顺序保存订单快照，不以原生订单号合并不同观察。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.business_facts.columns import join_pointer, read_columns, split_pointer, store_columns

ORDER_FIELDS = {
    "order_id": "identity",
    "symbol": "text",
    "direction": "text",
    "order_type": "text",
    "volume": "number",
    "price": "number",
    "status": "text",
    "filled_volume": "number",
    "avg_price": "number",
    "create_time": "text",
    "update_time": "text",
    "update_timestamp": "number",
    "remaining_volume": "number",
    "filled_ratio": "number",
    "extra": "json",
}
CONVERSION_FIELDS = {
    "price_currency": "text",
    "value_currency": "text",
    "rate": "number",
    "reference": "json",
}

_metadata = SQLModel.metadata
order_collection = sa.Table(
    "execution_order_collection",
    _metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("map_key", sa.Text),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("invalid_value", sa.JSON),
    sa.Column("item_count", sa.Integer),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    sa.CheckConstraint("kind IN ('array', 'null', 'invalid')"),
)
order = sa.Table(
    "execution_order",
    _metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("map_key", sa.Text),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("invalid_value", sa.JSON),
    *[
        sa.Column(name, sa.JSON if kind in {"json", "identity"} else sa.Float if kind == "number" else sa.Text)
        for name, kind in ORDER_FIELDS.items()
    ],
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    sa.Column("order_id_state", sa.Text, nullable=False),
    sa.Column("conversion_kind", sa.Text),
    sa.Column("conversion_invalid_value", sa.JSON),
    *[
        sa.Column(
            f"conversion_{name}", sa.JSON if kind in {"json", "identity"} else sa.Float if kind == "number" else sa.Text
        )
        for name, kind in CONVERSION_FIELDS.items()
    ],
    sa.Column("conversion_evidence", sa.JSON),
    sa.CheckConstraint("kind IN ('object', 'null', 'invalid')"),
    sa.CheckConstraint("conversion_kind IS NULL OR conversion_kind IN ('object', 'null', 'invalid')"),
    sa.ForeignKeyConstraint(
        ["record_id", "path"],
        ["execution_order_collection.record_id", "execution_order_collection.path"],
        ondelete="CASCADE",
    ),
)
TABLES = (order_collection, order)

FactBatch = dict[sa.Table, list[dict[str, object]]]
LoadedFacts = Mapping[str, Sequence[Mapping[str, object]]]


def _kind(value: object, expected: type) -> str:
    """区分有效容器、显式空值与历史畸形值。"""
    if value is None:
        return "null"
    return "object" if isinstance(value, expected) else "invalid"


def _encode_conversion(value: object) -> dict[str, object]:
    """将金额换算稳定字段写列，参考证据保留为所属对象扩展。"""
    kind = _kind(value, Mapping)
    result: dict[str, object] = {"conversion_kind": kind}
    if not isinstance(value, Mapping):
        columns, metadata = store_columns({"invalid_value": value}, {"invalid_value": "json"})
        result["conversion_invalid_value"] = columns["invalid_value"]
        result["conversion_evidence"] = metadata
        return result
    columns, metadata = store_columns(value, CONVERSION_FIELDS)
    result.update({f"conversion_{key}": val for key, val in columns.items()})
    result["conversion_evidence"] = metadata
    return result


def _encode_order(record_id: int, path: str, map_key: str | None, ordinal: int, value: object) -> dict[str, object]:
    """保存单个来源位置；缺少身份的订单同样占据独立行。"""
    kind = _kind(value, Mapping)
    row: dict[str, object] = {column.name: None for column in order.columns}
    row.update(
        {
            "record_id": record_id,
            "path": path,
            "map_key": map_key,
            "ordinal": ordinal,
            "kind": kind,
            "order_id_state": "absent",
            "audit_evidence": {},
        }
    )
    if not isinstance(value, Mapping):
        columns, metadata = store_columns({"invalid_value": value}, {"invalid_value": "json"})
        row["invalid_value"] = columns["invalid_value"]
        row["audit_evidence"] = metadata
        return row
    columns, metadata = store_columns(value, ORDER_FIELDS, child_keys=("value_conversion",))
    row.update(columns)
    row["order_id_state"] = (
        "absent"
        if "order_id" not in value
        else "null"
        if value["order_id"] is None
        else "value"
        if columns["order_id"] is not None
        else "invalid"
    )
    row["audit_evidence"] = metadata
    if "value_conversion" in value:
        row.update(_encode_conversion(value["value_conversion"]))
    return row


def _groups(payload: Mapping[str, object]) -> Iterator[tuple[str, str | None, int, Mapping[str, object]]]:
    """遍历根对象与有效品种对象；畸形品种节点归品种模块所有。"""
    yield "", None, 0, payload
    symbols = payload.get("symbol_results")
    if not isinstance(symbols, Mapping):
        return
    for ordinal, (key, value) in enumerate(symbols.items()):
        if not isinstance(key, str):
            raise ValueError("订单所属 symbol_results 的键必须是字符串")
        if isinstance(value, Mapping):
            yield join_pointer("/symbol_results", key), key, ordinal, value


def encode_orders(record_id: int, payload: Mapping[str, object]) -> FactBatch:
    """编码顶层和各品种的订单集合，保留空容器与畸形历史值。"""
    batches: FactBatch = {table: [] for table in TABLES}
    for scope, map_key, ordinal, group in _groups(payload):
        if "orders" not in group:
            continue
        values = group["orders"]
        path = join_pointer(scope, "orders")
        kind = "array" if isinstance(values, list) else "null" if values is None else "invalid"
        collection: dict[str, object] = {
            "record_id": record_id,
            "path": path,
            "map_key": map_key,
            "ordinal": ordinal,
            "kind": kind,
            "invalid_value": None,
            "item_count": len(values) if isinstance(values, list) else None,
            "audit_evidence": {},
        }
        if kind != "array":
            columns, metadata = store_columns({"invalid_value": values}, {"invalid_value": "json"})
            collection["invalid_value"] = columns["invalid_value"]
            collection["audit_evidence"] = metadata
        batches[order_collection].append(collection)
        if isinstance(values, list):
            batches[order].extend(
                _encode_order(record_id, path, map_key, index, value) for index, value in enumerate(values)
            )
    return batches


def _metadata_value(value: object) -> Mapping[str, object]:
    """读取元数据对象，损坏的存储显式失败。"""
    if not isinstance(value, Mapping):
        raise ValueError("订单字段元数据必须是映射")
    return value


def _decode_order(row: Mapping[str, object]) -> object:
    """重建单个快照，不调用会推导默认值的统一订单模型。"""
    if row["kind"] in {"null", "invalid"}:
        return read_columns(row, _metadata_value(row["audit_evidence"]), {"invalid_value": "json"})["invalid_value"]
    if row["kind"] != "object":
        raise ValueError("未知订单快照类型")
    metadata = _metadata_value(row["audit_evidence"])
    value = read_columns({key: row.get(key) for key in ORDER_FIELDS}, metadata, ORDER_FIELDS)
    if row["order_id_state"] == "null":
        value["order_id"] = None
    value["availability"] = {"order_id": row["order_id_state"]}
    conversion_kind = row.get("conversion_kind")
    if conversion_kind == "object":
        columns = {key: row.get(f"conversion_{key}") for key in CONVERSION_FIELDS}
        value["value_conversion"] = read_columns(
            columns, _metadata_value(row["conversion_evidence"]), CONVERSION_FIELDS
        )
    elif conversion_kind in {"null", "invalid"}:
        value["value_conversion"] = read_columns(
            {"invalid_value": row.get("conversion_invalid_value")},
            _metadata_value(row["conversion_evidence"]),
            {"invalid_value": "json"},
        )["invalid_value"]
    elif conversion_kind is not None:
        raise ValueError("未知订单金额换算类型")
    return dict(value)


def _put_collection(fragment: dict[str, object], path: str, value: object) -> None:
    """按冻结路径规则生成可递归合并的结果片段。"""
    parts = split_pointer(path)
    if parts == ("orders",):
        fragment["orders"] = value
    elif len(parts) == 3 and parts[0] == "symbol_results" and parts[2] == "orders":
        symbols = fragment.setdefault("symbol_results", {})
        if not isinstance(symbols, dict):
            raise ValueError("订单所属品种集合必须是映射")
        symbol = symbols.setdefault(parts[1], {})
        if not isinstance(symbol, dict):
            raise ValueError("订单所属品种必须是映射")
        symbol["orders"] = value
    else:
        raise ValueError(f"不支持的订单集合路径: {path}")


def _nonnegative_integer(value: object, field: str) -> int:
    """只接受数据库中的非负整数，不将布尔、浮点或字符串猜成序号。"""
    if type(value) is not int or value < 0:
        raise ValueError(f"订单{field}必须是非负整数")
    return value


def _ordinal(row: Mapping[str, object]) -> int:
    """校验来源或成员顺序的类型，并返回其原值。"""
    return _nonnegative_integer(row["ordinal"], "序号")


def decode_orders(rows: LoadedFacts) -> dict[str, object]:
    """读取已加载的类型化订单事实，返回原作用域的结果片段。"""
    items: dict[str, list[Mapping[str, object]]] = {}
    for row in rows.get(order.name, ()):
        items.setdefault(str(row["path"]), []).append(row)
    fragment: dict[str, object] = {}
    collections = sorted(rows.get(order_collection.name, ()), key=_ordinal)
    for collection in collections:
        path = str(collection["path"])
        if collection["kind"] == "array":
            ordered = sorted(items.pop(path, ()), key=_ordinal)
            item_count = _nonnegative_integer(collection["item_count"], "集合长度")
            if [row["ordinal"] for row in ordered] != list(range(item_count)):
                raise ValueError(f"订单集合成员不完整: {path}")
            value = [_decode_order(row) for row in ordered]
        elif collection["kind"] in {"null", "invalid"}:
            value = read_columns(collection, _metadata_value(collection["audit_evidence"]), {"invalid_value": "json"})[
                "invalid_value"
            ]
        else:
            raise ValueError(f"未知订单集合类型: {path}")
        _put_collection(fragment, path, value)
    if items:
        raise ValueError("订单快照缺少所属集合")
    return fragment
