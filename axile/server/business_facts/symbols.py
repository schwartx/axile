"""按原始出现位置保存品种结果、数量换算证据和首笔行情。"""

from collections.abc import Mapping, Sequence
from typing import cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from axile.server.business_facts.columns import join_pointer, read_columns, store_columns

SYMBOL_FIELDS = {
    "symbol": "text",
    "algorithm": "text",
    "status": "text",
    "error": "text",
    "technical_detail": "text",
    "reason_code": "text",
    "outcome": "text",
    "outcome_reason": "text",
    "target_volume": "number",
    "final_volume": "number",
    "success": "boolean",
    "memory": "json",
}
SIZING_FIELDS = {
    "symbol": "text",
    "sizing_mode": "text",
    "status": "text",
    "reason_code": "text",
    **dict.fromkeys(
        (
            "account_weight",
            "equity",
            "reference_price",
            "unit_multiplier",
            "unit_notional",
            "target_notional",
            "raw_quantity",
            "target_quantity",
            "current_quantity",
            "quantity_step",
            "min_quantity",
            "min_notional",
        ),
        "number",
    ),
}
CONVERSION_FIELDS = {"price_currency": "text", "value_currency": "text", "rate": "number", "reference": "json"}
TICK_FIELDS = {
    "symbol": "text",
    "last_price": "number",
    **{
        f"{side}_{field}{suffix}": "number"
        for side in ("bid", "ask")
        for field in ("price", "volume")
        for suffix in ("", "_2", "_3", "_4", "_5")
    },
    "volume": "number",
    "turnover": "number",
    "timestamp": "integer",
    "update_time": "text",
    "timestamp_datetime": "text",
    "book_valid": "boolean",
    "extra": "json",
}
_DELEGATED_SYMBOL_FIELDS = ("orders", "trades", "account_assets", "sizing", "first_tick", "availability")
_TARGET_FIELDS = {"value": "number"}
_SQL_TYPES = {"text": sa.Text, "number": sa.Float, "integer": sa.BigInteger, "boolean": sa.Boolean, "json": sa.JSON}


def _table(name: str, specs: Mapping[str, str]) -> sa.Table:
    """每个出现位置独立存储，原生品种名不承担行标识。"""
    return sa.Table(
        name,
        SQLModel.metadata,
        sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("map_key", sa.Text),
        sa.Column("node_kind", sa.Text, nullable=False),
        sa.Column("invalid_value", sa.JSON),
        sa.Column("audit_evidence", sa.JSON, nullable=False),
        *([sa.Column("member_count", sa.Integer)] if name == "execution_symbol_collection" else []),
        *(
            [
                sa.Column("sizing_recorded", sa.Boolean, nullable=False),
                sa.Column("tick_recorded", sa.Boolean, nullable=False),
                sa.Column("target_volume_state", sa.Text, nullable=False),
                sa.Column("final_volume_state", sa.Text, nullable=False),
            ]
            if name == "execution_symbol_result"
            else []
        ),
        *([sa.Column("conversion_recorded", sa.Boolean, nullable=False)] if name == "execution_symbol_sizing" else []),
        *[sa.Column(field, _SQL_TYPES[kind]) for field, kind in specs.items()],
        sa.CheckConstraint("node_kind IN ('object', 'value')"),
        sa.CheckConstraint("ordinal >= 0"),
        *([sa.Index(f"ix_{name}_record_ordinal", "record_id", "ordinal")] if name == "execution_symbol_result" else []),
    )


symbol_collection = _table("execution_symbol_collection", {})
symbol_result = _table("execution_symbol_result", SYMBOL_FIELDS)
symbol_sizing = _table("execution_symbol_sizing", SIZING_FIELDS)
symbol_conversion = _table("execution_symbol_conversion", CONVERSION_FIELDS)
symbol_tick = _table("execution_symbol_tick", TICK_FIELDS)
symbol_target = _table("execution_symbol_target", _TARGET_FIELDS)
TABLES = (symbol_collection, symbol_result, symbol_sizing, symbol_conversion, symbol_tick, symbol_target)

FactBatch = dict[sa.Table, list[dict[str, object]]]
LoadedFacts = Mapping[str, Sequence[Mapping[str, object]]]


def _string_mapping(value: object, context: str) -> Mapping[str, object]:
    """校验数据库 JSON 对象及查询行的键，再收窄为文本键映射。"""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"Invalid symbol mapping at {context}")
    return cast("Mapping[str, object]", value)


def _metadata(row: Mapping[str, object]) -> Mapping[str, object]:
    """读取已验证的字段元数据，不假设 JSON 列总是合法对象。"""
    return _string_mapping(row["audit_evidence"], f"{row.get('path', '')}/audit_evidence")


def _integer(row: Mapping[str, object], name: str) -> int:
    """标识和序号只接受实际整型，不转换历史业务字段。"""
    value = row[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Invalid symbol integer {name} at {row.get('path', '')}")
    return value


def _map_key(row: Mapping[str, object]) -> str:
    """映射键和嵌入品种名独立保存，并验证映射键类型。"""
    value = row["map_key"]
    if not isinstance(value, str):
        raise ValueError(f"Invalid symbol map key at {row['path']}")
    return value


def _volume_state(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        return "absent"
    states = value.get("availability")
    if isinstance(states, Mapping) and states.get(key) in {"absent", "null", "value", "invalid"}:
        return str(states[key])
    if key not in value:
        return "absent"
    if value[key] is None:
        return "null"
    typed, _ = store_columns({key: value[key]}, {key: "number"})
    return "value" if typed[key] is not None else "invalid"


def _encode_object(
    record_id: int,
    path: str,
    ordinal: int,
    map_key: str | None,
    value: object,
    specs: Mapping[str, str],
    child_keys: Sequence[str] = (),
) -> dict[str, object]:
    """只把合法对象的稳定字段拆列，异常历史节点单独保留。"""
    row: dict[str, object] = {"record_id": record_id, "path": path, "ordinal": ordinal, "map_key": map_key}
    if path in {"/symbol_results", "/first_ticks", "/target_volume"}:
        row["member_count"] = len(value) if isinstance(value, Mapping) else None
    if specs is SYMBOL_FIELDS:
        row.update({key + "_state": _volume_state(value, key) for key in ("target_volume", "final_volume")})
        row.update(
            sizing_recorded=isinstance(value, Mapping) and "sizing" in value,
            tick_recorded=isinstance(value, Mapping) and "first_tick" in value,
        )
    if specs is SIZING_FIELDS:
        row["conversion_recorded"] = isinstance(value, Mapping) and "value_conversion" in value
    if not isinstance(value, Mapping):
        columns, metadata = store_columns({"invalid_value": value}, {"invalid_value": "json"})
        return {**row, **dict.fromkeys(specs), **columns, "node_kind": "value", "audit_evidence": metadata}
    columns, metadata = store_columns(value, specs, child_keys=child_keys)
    return {**row, **columns, "node_kind": "object", "invalid_value": None, "audit_evidence": metadata}


def _decode_object(row: Mapping[str, object], specs: Mapping[str, str]) -> object:
    """还原原始节点，不校验执行模型或填入默认值。"""
    metadata = _metadata(row)
    if row["node_kind"] == "value":
        return read_columns(row, metadata, {"invalid_value": "json"})["invalid_value"]
    view = read_columns(row, metadata, specs)
    if specs is SYMBOL_FIELDS:
        view["availability"] = {key: row[key + "_state"] for key in ("target_volume", "final_volume")}
        for key in ("target_volume", "final_volume"):
            if row[key + "_state"] == "null":
                view[key] = None
    return view


def _append_nested(
    batch: FactBatch,
    record_id: int,
    path: str,
    ordinal: int,
    map_key: str,
    payload: Mapping[str, object],
) -> None:
    """品种明细只拆本域拥有的换算与行情，不重复订单成交数组。"""
    if "sizing" in payload:
        sizing_path = join_pointer(path, "sizing")
        sizing = payload["sizing"]
        batch[symbol_sizing].append(
            _encode_object(record_id, sizing_path, ordinal, map_key, sizing, SIZING_FIELDS, ("value_conversion",))
        )
        if isinstance(sizing, Mapping) and "value_conversion" in sizing:
            batch[symbol_conversion].append(
                _encode_object(
                    record_id,
                    join_pointer(sizing_path, "value_conversion"),
                    ordinal,
                    map_key,
                    sizing["value_conversion"],
                    CONVERSION_FIELDS,
                )
            )
    if "first_tick" in payload:
        batch[symbol_tick].append(
            _encode_object(
                record_id, join_pointer(path, "first_tick"), ordinal, map_key, payload["first_tick"], TICK_FIELDS
            )
        )


def encode_symbols(record_id: int, payload: Mapping[str, object]) -> FactBatch:
    """拆分品种结果及旧版顶层映射，保留空映射、顺序和独立数值。"""
    batch: FactBatch = {table: [] for table in TABLES}
    for collection_name in ("symbol_results", "first_ticks", "target_volume"):
        if collection_name not in payload:
            continue
        collection = payload[collection_name]
        path = join_pointer("", collection_name)
        batch[symbol_collection].append(
            _encode_object(
                record_id, path, 0, None, collection, {}, tuple(collection) if isinstance(collection, Mapping) else ()
            )
        )
        if not isinstance(collection, Mapping):
            continue
        for ordinal, (map_key, value) in enumerate(collection.items()):
            item_path = join_pointer(path, map_key)
            if collection_name == "symbol_results":
                batch[symbol_result].append(
                    _encode_object(
                        record_id, item_path, ordinal, map_key, value, SYMBOL_FIELDS, _DELEGATED_SYMBOL_FIELDS
                    )
                )
                if isinstance(value, Mapping):
                    _append_nested(batch, record_id, item_path, ordinal, map_key, value)
            elif collection_name == "first_ticks":
                batch[symbol_tick].append(_encode_object(record_id, item_path, ordinal, map_key, value, TICK_FIELDS))
            else:
                batch[symbol_target].append(
                    _encode_object(record_id, item_path, ordinal, map_key, {"value": value}, _TARGET_FIELDS)
                )
    return batch


def _by_path(rows: LoadedFacts, table: sa.Table) -> dict[str, Mapping[str, object]]:
    """索引同一执行的节点；重复出现位置属于损坏事实。"""
    result: dict[str, Mapping[str, object]] = {}
    for row in rows.get(table.name, ()):
        path = str(row["path"])
        if path in result:
            raise ValueError(f"Duplicate symbol fact at {path}")
        result[path] = row
    return result


def _restore_nested(
    item: dict[str, object],
    path: str,
    sizing_rows: Mapping[str, Mapping[str, object]],
    conversion_rows: Mapping[str, Mapping[str, object]],
    tick_rows: Mapping[str, Mapping[str, object]],
) -> None:
    """把已拆列节点接回本域片段，缺失的节点保持缺失。"""
    sizing_path = join_pointer(path, "sizing")
    if sizing_path in sizing_rows:
        row = sizing_rows[sizing_path]
        sizing = _decode_object(row, SIZING_FIELDS)
        conversion_path = join_pointer(sizing_path, "value_conversion")
        if isinstance(sizing, dict) and row["conversion_recorded"] and conversion_path not in conversion_rows:
            raise ValueError(f"Missing sizing conversion fact at {conversion_path}")
        if isinstance(sizing, dict) and conversion_path in conversion_rows:
            sizing["value_conversion"] = _decode_object(conversion_rows[conversion_path], CONVERSION_FIELDS)
            sizing = dict(sizing)
        item["sizing"] = sizing
    tick_path = join_pointer(path, "first_tick")
    if tick_path in tick_rows:
        item["first_tick"] = _decode_object(tick_rows[tick_path], TICK_FIELDS)


def _collection_rows(
    path: str,
    collection_row: Mapping[str, object],
    children: Mapping[str, Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """校验映射成员完整性，缺少类型化事实时直接暴露存储损坏。"""
    rows = sorted(
        (row for child_path, row in children.items() if child_path.startswith(path + "/")),
        key=lambda item: _integer(item, "ordinal"),
    )
    expected = collection_row["member_count"]
    if len(rows) != expected:
        raise ValueError(f"Incomplete symbol collection at {path}")
    for ordinal, row in enumerate(rows):
        if row["ordinal"] != ordinal or row["path"] != join_pointer(path, _map_key(row)):
            raise ValueError(f"Invalid symbol occurrence at {row['path']}")
    return rows


def decode_symbols(rows: LoadedFacts) -> dict[str, object]:
    """还原本域拥有的结果片段，供显式详情组装合并。"""
    collections = _by_path(rows, symbol_collection)
    results = _by_path(rows, symbol_result)
    sizings = _by_path(rows, symbol_sizing)
    conversions = _by_path(rows, symbol_conversion)
    ticks = _by_path(rows, symbol_tick)
    targets = _by_path(rows, symbol_target)
    fragment: dict[str, object] = {}
    target_issues: dict[str, object] = {}
    for name in ("symbol_results", "first_ticks", "target_volume"):
        path = join_pointer("", name)
        collection_row = collections.get(path)
        if collection_row is None:
            continue
        collection = _decode_object(collection_row, {})
        fragment[name] = collection
        if not isinstance(collection, dict):
            continue
        child_rows = results if name == "symbol_results" else ticks if name == "first_ticks" else targets
        for row in _collection_rows(path, collection_row, child_rows):
            map_key = _map_key(row)
            if name == "symbol_results":
                item = _decode_object(row, SYMBOL_FIELDS)
                if isinstance(item, dict):
                    _restore_nested(item, str(row["path"]), sizings, conversions, ticks)
                    for child in ("sizing", "first_tick"):
                        if row["sizing_recorded" if child == "sizing" else "tick_recorded"] and child not in item:
                            raise ValueError(f"Missing symbol fact at {join_pointer(str(row['path']), child)}")
                    item = dict(item)
            elif name == "first_ticks":
                item = _decode_object(row, TICK_FIELDS)
            else:
                target = _decode_object(row, _TARGET_FIELDS)
                item = target["value"] if isinstance(target, Mapping) else target
                if isinstance(target, Mapping) and target.get("audit_evidence"):
                    target_issues[map_key] = target["audit_evidence"]
            collection[map_key] = item
    if target_issues:
        fragment["audit_evidence"] = {"invalid_target_values": {"target_volume": target_issues}}
    return fragment


async def read_symbol_rows(
    session: AsyncSession,
    record_ids: Sequence[int],
    *,
    fields: Sequence[str] | None = None,
) -> dict[int, dict[str, dict[str, object]]]:
    """批量读取品种标量，状态列表无需展开结果、订单或资产。"""
    if not record_ids:
        return {}
    selected = tuple(SYMBOL_FIELDS) if fields is None else tuple(fields)
    unknown = set(selected) - SYMBOL_FIELDS.keys()
    if unknown:
        raise ValueError(f"Unsupported symbol scalar fields: {sorted(unknown)}")
    columns = [symbol_result.c[key] for key in ("record_id", "path", "ordinal", "map_key", "node_kind", *selected)]
    query = (
        sa.select(*columns)
        .where(symbol_result.c.record_id.in_(record_ids))
        .order_by(symbol_result.c.record_id, symbol_result.c.ordinal)
    )
    loaded = (await session.execute(query)).mappings().all()
    result: dict[int, dict[str, dict[str, object]]] = {record_id: {} for record_id in record_ids}
    specs = {field: SYMBOL_FIELDS[field] for field in selected}
    for loaded_row in loaded:
        row = _string_mapping(loaded_row, "symbol query row")
        if row["node_kind"] != "object":
            continue
        item = read_columns(row, None, specs)
        result[_integer(row, "record_id")][_map_key(row)] = {
            field: item[field] for field in selected if field in item and item[field] is not None
        }
    return result


async def read_outcome_symbols(session: AsyncSession, record_id: int) -> list[str]:
    """按现有展示归一规则读取未到位或受阻品种，保留映射顺序。"""
    from axile.server.execution.legacy_compat import normalize_legacy_result

    by_record = await read_symbol_rows(session, [record_id], fields=("status", "outcome"))
    result: list[str] = []
    for symbol, fields in by_record[record_id].items():
        normalized = normalize_legacy_result(fields)
        if isinstance(normalized, dict) and normalized.get("outcome") in {"not_reached", "blocked"}:
            result.append(symbol)
    return result


async def read_sizing_rows(session: AsyncSession, record_id: int) -> dict[str, dict[str, object]]:
    """只读取最终品种换算及汇率证据，不加载完整执行详情。"""
    tables = (symbol_sizing, symbol_conversion)
    loaded: dict[str, Sequence[Mapping[str, object]]] = {}
    for table in tables:
        query = sa.select(table).where(table.c.record_id == record_id).order_by(table.c.ordinal)
        loaded[table.name] = [
            _string_mapping(row, f"{table.name} query row") for row in (await session.execute(query)).mappings().all()
        ]
    conversions = _by_path(loaded, symbol_conversion)
    result: dict[str, dict[str, object]] = {}
    for row in loaded[symbol_sizing.name]:
        sizing = _decode_object(row, SIZING_FIELDS)
        if not isinstance(sizing, dict):
            continue
        path = join_pointer(str(row["path"]), "value_conversion")
        if row["conversion_recorded"] and path not in conversions:
            raise ValueError(f"Missing sizing conversion fact at {path}")
        if path in conversions:
            sizing["value_conversion"] = _decode_object(conversions[path], CONVERSION_FIELDS)
            sizing = dict(sizing)
        result[_map_key(row)] = sizing
    return result
