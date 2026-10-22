"""资产观测与持仓事实；保存每次观测的来源、时点及原生字段。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.business_facts.columns import audit_value, join_pointer, read_columns, store_columns

ASSET_FIELDS = {
    "available_cash": "number",
    "total_asset": "number",
    "market_value": "number",
    "currency": "text",
    "update_time": "text",
    "source": "text",
    "extra": "json",
}
_POSITION_AMOUNTS = ("volume", "available_volume", "market_value", "avg_price")

POSITION_FIELDS = {
    "symbol": "text",
    "volume": "number",
    "available_volume": "number",
    "market_value": "number",
    "direction": "text",
    "avg_price": "number",
    "extra": "json",
}


def _columns(specs: Mapping[str, str]) -> list[sa.Column]:
    """为统一模型的稳定字段建立类型化列。"""
    kinds = {"number": sa.Float, "text": sa.Text, "json": sa.JSON}
    return [sa.Column(name, kinds[kind], nullable=True) for name, kind in specs.items()]


asset_snapshot = sa.Table(
    "business_asset_snapshot",
    SQLModel.metadata,
    sa.Column("asset_id", sa.Text, primary_key=True),
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE")),
    sa.Column("input_id", sa.Text, sa.ForeignKey("execution_input.id", ondelete="CASCADE")),
    sa.Column("account_snapshot_id", sa.Integer, sa.ForeignKey("account_asset_snapshot.id", ondelete="CASCADE")),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("map_key", sa.Text),
    sa.Column("payload_kind", sa.Text, nullable=False),
    sa.Column("payload_value", sa.JSON),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    sa.Column("cash_state", sa.Text, nullable=False),
    sa.Column("equity_state", sa.Text, nullable=False),
    sa.Column("market_state", sa.Text, nullable=False),
    sa.Column("positions_kind", sa.Text, nullable=False),
    sa.Column("positions_count", sa.Integer),
    sa.Column("positions_value", sa.JSON),
    sa.Column("observation_valid", sa.Boolean, nullable=False),
    *_columns(ASSET_FIELDS),
    sa.CheckConstraint(
        "(record_id IS NOT NULL) + (input_id IS NOT NULL) + (account_snapshot_id IS NOT NULL) = 1",
        name="ck_business_asset_snapshot_one_owner",
    ),
    sa.Index("ix_business_asset_snapshot_record_path", "record_id", "path", unique=True),
    sa.Index("ix_business_asset_snapshot_input_path", "input_id", "path", unique=True),
    sa.Index("ix_business_asset_snapshot_account_snapshot", "account_snapshot_id", unique=True),
)
asset_position = sa.Table(
    "business_asset_position",
    SQLModel.metadata,
    sa.Column(
        "asset_id", sa.Text, sa.ForeignKey("business_asset_snapshot.asset_id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("payload_kind", sa.Text, nullable=False),
    sa.Column("payload_value", sa.JSON),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    *[sa.Column(name + "_state", sa.Text, nullable=False) for name in _POSITION_AMOUNTS],
    *_columns(POSITION_FIELDS),
)
asset_binding = sa.Table(
    "business_asset_binding",
    SQLModel.metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column(
        "asset_id", sa.Text, sa.ForeignKey("business_asset_snapshot.asset_id", ondelete="RESTRICT"), nullable=False
    ),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("map_key", sa.Text),
    sa.Index("ix_business_asset_binding_asset", "asset_id"),
)
TABLES = (asset_snapshot, asset_position, asset_binding)


def asset_fact_id(record_id: int, path: str = "/account_assets") -> str:
    """返回终态记录中某个资产观测的稳定发生标识。"""
    return f"record:{record_id}:{path}"


def input_asset_fact_id(input_id: str) -> str:
    """返回执行前资产观测标识，允许尚未产生终态记录。"""
    return f"input:{input_id}:/before_account_assets"


def input_after_asset_fact_id(input_id: str) -> str:
    """返回 worker 在终态记录之前保存的执行后观测标识。"""
    return f"input:{input_id}:/after_account_assets"


def snapshot_asset_fact_id(snapshot_id: int) -> str:
    """返回一次手工资产观测的稳定发生标识。"""
    return f"snapshot:{snapshot_id}:/account_assets"


def _kind(value: object) -> str:
    if isinstance(value, Mapping):
        return "object"
    return "null" if value is None else "invalid"


def _position_rows(asset_id: str, value: object) -> list[dict[str, object]]:
    """按原始列表顺序保存持仓，不按品种或方向去重。"""
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for ordinal, position in enumerate(value):
        kind = _kind(position)
        columns, metadata = store_columns(
            position if isinstance(position, Mapping) else {}, POSITION_FIELDS, child_keys=("availability",)
        )
        rows.append(
            {
                "asset_id": asset_id,
                "ordinal": ordinal,
                **{
                    name + "_state": _amount_state(
                        position if isinstance(position, Mapping) else {}, name, columns, label=name
                    )
                    for name in _POSITION_AMOUNTS
                },
                "payload_kind": kind,
                "payload_value": None if kind == "object" else audit_value(position),
                "audit_evidence": metadata,
                **columns,
            }
        )
    return rows


def _amount_state(
    payload: Mapping[str, object], key: str, columns: Mapping[str, object], *, label: str | None = None
) -> str:
    availability = payload.get("availability")
    if isinstance(availability, Mapping):
        state = availability.get(label or ("cash" if key == "available_cash" else "equity"))
        if state in {"absent", "null", "value", "invalid"}:
            return str(state)
    if key not in payload:
        return "absent"
    if payload[key] is None:
        return "null"
    return "value" if columns[key] is not None else "invalid"


def _encode_observation(
    value: object,
    *,
    asset_id: str,
    path: str,
    scope: str,
    ordinal: int = 0,
    map_key: str | None = None,
    record_id: int | None = None,
    input_id: str | None = None,
    account_snapshot_id: int | None = None,
    result: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    from axile.server.asset_observations import is_asset_observation

    kind = _kind(value)
    payload = value if isinstance(value, Mapping) else {}
    columns, metadata = store_columns(payload, ASSET_FIELDS, child_keys=("positions", "availability"))
    positions = payload.get("positions")
    positions_kind = (
        "absent" if "positions" not in payload else "array" if isinstance(positions, list) else _kind(positions)
    )
    root = {
        "asset_id": asset_id,
        "record_id": record_id,
        "input_id": input_id,
        "account_snapshot_id": account_snapshot_id,
        "path": path,
        "scope": scope,
        "ordinal": ordinal,
        "map_key": map_key,
        "payload_kind": kind,
        "payload_value": None if kind == "object" else audit_value(value),
        "audit_evidence": metadata,
        "cash_state": _amount_state(payload, "available_cash", columns),
        "equity_state": _amount_state(payload, "total_asset", columns),
        "market_state": _amount_state(payload, "market_value", columns, label="market_value"),
        "positions_kind": positions_kind,
        "positions_count": len(positions) if isinstance(positions, list) else None,
        "positions_value": None if positions_kind in ("absent", "array") else audit_value(positions),
        "observation_valid": is_asset_observation(dict(payload), result),
        **columns,
    }
    return root, _position_rows(asset_id, positions)


def encode_assets(
    record_id: int, payload: Mapping[str, object], *, root_asset_id: str | None = None
) -> dict[sa.Table, list[dict[str, object]]]:
    """将执行后与逐品种资产观测写入独立事实发生。"""
    batch: dict[sa.Table, list[dict[str, object]]] = {table: [] for table in TABLES}
    occurrences: list[tuple[str, str, int, str | None, object]] = []
    if "account_assets" in payload:
        occurrences.append(("/account_assets", "after", 0, None, payload["account_assets"]))
    symbols = payload.get("symbol_results")
    if isinstance(symbols, Mapping):
        for ordinal, (key, item) in enumerate(symbols.items()):
            if isinstance(item, Mapping) and "account_assets" in item:
                path = join_pointer(join_pointer("/symbol_results", key), "account_assets")
                occurrences.append((path, "symbol", ordinal, key, item["account_assets"]))
    for path, scope, ordinal, map_key, value in occurrences:
        linked_id = root_asset_id if scope == "after" and root_asset_id is not None else asset_fact_id(record_id, path)
        batch[asset_binding].append(
            {
                "record_id": record_id,
                "path": path,
                "asset_id": linked_id,
                "scope": scope,
                "ordinal": ordinal,
                "map_key": map_key,
            }
        )
        if scope == "after" and root_asset_id is not None:
            continue
        root, positions = _encode_observation(
            value,
            asset_id=asset_fact_id(record_id, path),
            path=path,
            scope=scope,
            ordinal=ordinal,
            map_key=map_key,
            record_id=record_id,
            result=payload if scope == "after" else None,
        )
        batch[asset_snapshot].append(root)
        batch[asset_position].extend(positions)
    return batch


def encode_input_assets(input_id: str, payload: Mapping[str, object]) -> dict[sa.Table, list[dict[str, object]]]:
    """保存下单前资产；参数通过 account_assets 键明确区分缺失与空值。"""
    if "account_assets" not in payload:
        return {table: [] for table in TABLES}
    root, positions = _encode_observation(
        payload["account_assets"],
        asset_id=input_asset_fact_id(input_id),
        path="/before_account_assets",
        scope="before",
        input_id=input_id,
    )
    return {asset_snapshot: [root], asset_position: positions, asset_binding: []}


def encode_input_after_assets(
    input_id: str, payload: Mapping[str, object], *, result: Mapping[str, object] | None = None
) -> dict[sa.Table, list[dict[str, object]]]:
    """在 worker 返回前保存执行后观测，终态记录随后只绑定同一事实。"""
    if "account_assets" not in payload:
        return {table: [] for table in TABLES}
    root, positions = _encode_observation(
        payload["account_assets"],
        asset_id=input_after_asset_fact_id(input_id),
        path="/after_account_assets",
        scope="after_pending",
        input_id=input_id,
        result=result,
    )
    return {asset_snapshot: [root], asset_position: positions, asset_binding: []}


def encode_snapshot_assets(
    snapshot_id: int, payload: Mapping[str, object], *, result: Mapping[str, object] | None = None
) -> dict[sa.Table, list[dict[str, object]]]:
    """迁移或保存一次手工资产观测，保留历史快照编号。"""
    if "account_assets" not in payload:
        raise ValueError("账户资产观测缺少 account_assets")
    root, positions = _encode_observation(
        payload["account_assets"],
        asset_id=snapshot_asset_fact_id(snapshot_id),
        path="/account_assets",
        scope="manual",
        account_snapshot_id=snapshot_id,
        result=result,
    )
    return {asset_snapshot: [root], asset_position: positions, asset_binding: []}


def _position_view(row: Mapping[str, object]) -> object:
    if row["payload_kind"] != "object":
        return row.get("payload_value")
    evidence = cast(Mapping[str, object], row["audit_evidence"])
    view = read_columns(row, evidence, POSITION_FIELDS)
    states = {key: row[key + "_state"] for key in _POSITION_AMOUNTS}
    view["availability"] = states
    for key, state in states.items():
        if state == "null":
            view[key] = None
    return view


def _asset_scalars(row: Mapping[str, object]) -> dict[str, object]:
    view = read_columns(row, cast(Mapping[str, object], row["audit_evidence"]), ASSET_FIELDS)
    states = {"cash": row["cash_state"], "equity": row["equity_state"], "market_value": row["market_state"]}
    view["availability"] = states
    for label, key in (("cash", "available_cash"), ("equity", "total_asset"), ("market_value", "market_value")):
        if states[label] == "null":
            view[key] = None
    return view


def _decode_observation(row: Mapping[str, object], positions: Sequence[Mapping[str, object]]) -> object:
    if row["payload_kind"] != "object":
        return row.get("payload_value")
    payload = _asset_scalars(row)
    kind = row["positions_kind"]
    if kind == "array":
        if len(positions) != row["positions_count"]:
            raise ValueError("Incomplete position facts")
        if sorted(cast(int, position["ordinal"]) for position in positions) != list(range(len(positions))):
            raise ValueError("Invalid position occurrence sequence")
        payload["positions"] = [
            _position_view(position) for position in sorted(positions, key=lambda item: cast(int, item["ordinal"]))
        ]
    elif kind != "absent":
        payload["positions"] = row.get("positions_value")
    return payload


def decode_assets(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> dict[str, object]:
    """只重组资产域；输入前快照以 before_account_assets 单独返回。"""
    positions: dict[object, list[Mapping[str, object]]] = {}
    for row in rows.get(asset_position.name, ()):
        positions.setdefault(row["asset_id"], []).append(row)
    result: dict[str, object] = {}
    roots = {row["asset_id"]: row for row in rows.get(asset_snapshot.name, ())}
    bindings = rows.get(asset_binding.name, ())
    bound_ids = {row["asset_id"] for row in bindings}
    unbound = [row for key, row in roots.items() if key not in bound_ids]
    occurrences = sorted((*bindings, *unbound), key=lambda row: cast("int", row["ordinal"]))
    for occurrence in occurrences:
        row = roots.get(occurrence["asset_id"])
        if row is None:
            raise ValueError(f"资产绑定缺少观测事实: asset_id={occurrence['asset_id']}")
        value = _decode_observation(row, positions.get(row["asset_id"], ()))
        if occurrence["scope"] == "symbol":
            symbols = cast("dict[str, object]", result.setdefault("symbol_results", {}))
            symbols[cast("str", occurrence["map_key"])] = {"account_assets": value}
        else:
            key = {
                "before": "before_account_assets",
                "after_pending": "after_account_assets",
            }.get(cast("str", occurrence["scope"]), "account_assets")
            result[key] = value
    return result


async def _load_asset_rows(session: Any, asset_ids: Sequence[str]) -> dict[str, list[Mapping[str, object]]]:
    if not asset_ids:
        return {table.name: [] for table in TABLES}
    roots = await session.execute(sa.select(asset_snapshot).where(asset_snapshot.c.asset_id.in_(asset_ids)))
    positions = await session.execute(sa.select(asset_position).where(asset_position.c.asset_id.in_(asset_ids)))
    return {
        asset_snapshot.name: list(roots.mappings()),
        asset_position.name: list(positions.mappings()),
        asset_binding.name: [],
    }


async def load_result_asset_rows(session: Any, record_id: int) -> dict[str, list[Mapping[str, object]]]:
    """加载终态结果直接和间接引用的资产事实，保留原发生标识。"""
    bindings = await _validated_result_bindings(session, record_id)
    rows = await _load_asset_rows(session, [cast("str", row["asset_id"]) for row in bindings])
    rows[asset_binding.name] = bindings
    return rows


async def validate_result_asset_ownership(session: Any, record_id: int) -> None:
    """在载荷预算检查前仅查询标量身份，拒绝跨账户或跨执行绑定。"""
    await _validated_result_bindings(session, record_id)


async def _validated_result_bindings(session: Any, record_id: int) -> list[Mapping[str, object]]:
    bindings = list(
        (await session.execute(sa.select(asset_binding).where(asset_binding.c.record_id == record_id))).mappings()
    )
    records = SQLModel.metadata.tables["executerecord"]
    record = (
        (
            await session.execute(
                sa.select(
                    records.c.id,
                    records.c.account_id,
                    records.c.execution_id,
                    records.c.input_id,
                    records.c.result_input_id,
                ).where(records.c.id == record_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise ValueError("资产事实所属执行记录不存在")
    roots = list(
        (
            await session.execute(
                sa.select(
                    *[
                        asset_snapshot.c[key]
                        for key in (
                            "asset_id",
                            "record_id",
                            "input_id",
                            "account_snapshot_id",
                            "path",
                            "scope",
                            "ordinal",
                            "map_key",
                        )
                    ]
                ).where(asset_snapshot.c.asset_id.in_([row["asset_id"] for row in bindings]))
            )
        ).mappings()
    )
    input_ids = [row["input_id"] for row in roots if row["input_id"] is not None]
    inputs = SQLModel.metadata.tables["execution_input"]
    input_rows = (
        list(
            (
                await session.execute(
                    sa.select(
                        inputs.c.id, inputs.c.account_id, inputs.c.execution_id, inputs.c.owner_execution_id
                    ).where(inputs.c.id.in_(input_ids))
                )
            ).mappings()
        )
        if input_ids
        else []
    )
    validate_result_asset_owners(record, roots, input_rows, bindings=bindings)
    return bindings


def validate_result_asset_owners(
    record: Mapping[str, object],
    roots: Sequence[Mapping[str, object]],
    input_rows: Sequence[Mapping[str, object]],
    *,
    bindings: Sequence[Mapping[str, object]] = (),
) -> None:
    """拒绝结果引用其他执行或其他账户的观测，供同步与异步读取共用。"""
    by_input = {row["id"]: row for row in input_rows}
    for root in roots:
        _validate_asset_owner(record, root, by_input)
    by_asset = {row["asset_id"]: row for row in roots}
    for binding in bindings:
        root = by_asset.get(binding["asset_id"])
        if root is None:
            raise ValueError("资产绑定缺少观测事实")
        if binding["record_id"] != record["id"]:
            raise ValueError("资产绑定属于其他执行记录")
        if root["record_id"] is not None:
            if any(binding[key] != root[key] for key in ("path", "scope", "ordinal", "map_key")):
                raise ValueError("资产绑定与原始观测发生不一致")
        elif binding["path"] != "/account_assets" or binding["scope"] != "after":
            raise ValueError("执行前资产不能绑定为执行后结果")


def _validate_asset_owner(
    record: Mapping[str, object], root: Mapping[str, object], inputs: Mapping[object, Mapping[str, object]]
) -> None:
    if root["record_id"] is not None:
        if root["record_id"] != record["id"]:
            raise ValueError("资产观测属于其他执行记录")
        return
    input_id = root["input_id"]
    owner = inputs.get(input_id)
    if owner is None or input_id not in (record["input_id"], record["result_input_id"]):
        raise ValueError("资产观测输入与执行记录不匹配")
    if owner["account_id"] != record["account_id"]:
        raise ValueError("资产观测属于其他账户")
    expected = record["execution_id"]
    owner_execution_id = owner.get("owner_execution_id")
    if owner_execution_id is None:
        owner_execution_id = owner.get("execution_id")
    if owner_execution_id != expected:
        raise ValueError("资产观测属于其他执行")
    if owner.get("execution_id") is not None and owner["execution_id"] != expected:
        raise ValueError("资产观测输入执行标识不匹配")
    if root["path"] != "/after_account_assets" or root["scope"] != "after_pending":
        raise ValueError("执行前资产不能绑定为执行后结果")


async def read_input_asset_observations(session: Any, input_id: str) -> list[dict[str, object]]:
    """显式读取未结束输入的原始前后观测，不伪造终态记录或审计附件。"""
    roots = list(
        (
            await session.execute(
                sa.select(asset_snapshot)
                .where(asset_snapshot.c.input_id == input_id)
                .order_by(asset_snapshot.c.ordinal, asset_snapshot.c.path)
            )
        ).mappings()
    )
    asset_ids = [row["asset_id"] for row in roots]
    if not asset_ids:
        return []
    positions: dict[object, list[Mapping[str, object]]] = {}
    result = await session.execute(sa.select(asset_position).where(asset_position.c.asset_id.in_(asset_ids)))
    for row in result.mappings():
        positions.setdefault(row["asset_id"], []).append(row)
    return [
        {
            "asset_id": row["asset_id"],
            "path": row["path"],
            "scope": row["scope"],
            "account_assets": _decode_observation(row, positions.get(row["asset_id"], ())),
        }
        for row in roots
    ]


async def find_matching_after_asset(session: Any, input_id: str, payload: Mapping[str, object]) -> str | None:
    """仅复用完全相同的 worker 后观测；存在不同观测时拒绝覆盖证据。"""
    asset_id = input_after_asset_fact_id(input_id)
    rows = await _load_asset_rows(session, [asset_id])
    roots = rows[asset_snapshot.name]
    if not roots:
        return None
    root = roots[0]
    if "account_assets" not in payload:
        raise ValueError("已持久化执行后观测，但终态结果缺少 account_assets")
    actual = _decode_observation(root, rows[asset_position.name])
    if not _same_value(actual, payload["account_assets"]):
        raise ValueError("终态结果与已持久化执行后资产观测不一致")
    return asset_id


def _same_value(left: object, right: object) -> bool:
    """按资产业务事实比较；字段排列和数字表示不构成新观测。"""
    if isinstance(right, Mapping) and "availability" not in right:
        root, positions = _encode_observation(right, asset_id="compare", path="", scope="after", input_id="compare")
        right = _decode_observation(root, positions)
    return left == right


async def write_snapshot_assets(
    session: Any,
    snapshot: Any,
    assets: Mapping[str, object],
    *,
    result: Mapping[str, object] | None = None,
) -> None:
    """在调用方事务中保存手工观测并绑定原有快照行。"""
    if snapshot.id is None:
        session.add(snapshot)
        await session.flush()
    snapshot_id = snapshot.id
    if snapshot_id is None:
        raise ValueError("账户资产快照必须先分配 id")
    batch = encode_snapshot_assets(snapshot_id, {"account_assets": assets}, result=result)
    for table, values in batch.items():
        if values:
            await session.execute(table.insert(), values)
    snapshot.asset_fact_id = snapshot_asset_fact_id(snapshot_id)


async def ensure_execution_asset_snapshot(session: Any, record: Any) -> Any | None:
    """为有效执行后观测保存资产历史索引，复用原事实并由调用方提交。"""
    from axile.server.db.models.account_asset import AccountAssetSnapshot

    if record.id is None:
        raise ValueError("执行记录必须先分配 id")
    await validate_result_asset_ownership(session, record.id)
    reference = await session.scalar(
        sa.select(asset_binding.c.asset_id)
        .select_from(asset_binding.join(asset_snapshot, asset_binding.c.asset_id == asset_snapshot.c.asset_id))
        .where(
            asset_binding.c.record_id == record.id,
            asset_binding.c.path == "/account_assets",
            asset_snapshot.c.observation_valid.is_(True),
        )
    )
    if reference is None:
        return None
    condition = (
        AccountAssetSnapshot.execution_id == record.execution_id
        if record.execution_id is not None
        else AccountAssetSnapshot.asset_fact_id == reference
    )
    existing = (await session.execute(sa.select(AccountAssetSnapshot).where(condition))).scalar_one_or_none()
    if existing is not None:
        if (
            existing.account_id != record.account_id
            or existing.asset_fact_id != reference
            or existing.source != "execution"
        ):
            raise ValueError("执行资产历史索引与原观测不一致")
        return existing
    snapshot = AccountAssetSnapshot(
        account_id=record.account_id,
        asset_fact_id=reference,
        source="execution",
        execution_id=record.execution_id,
        created_at=record.created_at,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def read_snapshots_assets(
    session: Any, snapshots: Sequence[Any], *, include_positions: bool = True
) -> dict[int, dict[str, object]]:
    """批量显式读取资产；标量调用不查询持仓子表。"""
    if not snapshots:
        return {}
    references = {snapshot.asset_fact_id for snapshot in snapshots}
    if None in references:
        raise ValueError("账户资产观测缺少结构化事实引用")
    roots = (
        await session.execute(sa.select(asset_snapshot).where(asset_snapshot.c.asset_id.in_(references)))
    ).mappings()
    by_id = {row["asset_id"]: row for row in roots}
    await _validate_snapshot_references(session, snapshots, by_id)
    positions: dict[object, list[Mapping[str, object]]] = {}
    if include_positions:
        result = await session.execute(sa.select(asset_position).where(asset_position.c.asset_id.in_(references)))
        for row in result.mappings():
            positions.setdefault(row["asset_id"], []).append(row)
    output: dict[int, dict[str, object]] = {}
    for snapshot in snapshots:
        row = by_id.get(snapshot.asset_fact_id)
        if row is None:
            raise ValueError(f"账户资产观测结构化事实缺失: snapshot_id={snapshot.id}")
        if include_positions:
            payload = _decode_observation(row, positions.get(row["asset_id"], ()))
        else:
            payload = _asset_scalars(row)
        if not isinstance(payload, dict):
            raise ValueError(f"账户资产观测不是对象: snapshot_id={snapshot.id}")
        output[snapshot.id] = payload
    return output


async def _validate_snapshot_references(
    session: Any, snapshots: Sequence[Any], roots: Mapping[object, Mapping[str, object]]
) -> None:
    indirect: list[Any] = []
    for snapshot in snapshots:
        root = roots.get(snapshot.asset_fact_id)
        if root is None:
            raise ValueError(f"账户资产观测结构化事实缺失: snapshot_id={snapshot.id}")
        if root["account_snapshot_id"] is not None:
            if root["account_snapshot_id"] != snapshot.id:
                raise ValueError("账户资产观测引用属于其他快照")
        else:
            indirect.append(snapshot)
    if not indirect:
        return
    records = SQLModel.metadata.tables["executerecord"]
    statement = (
        sa.select(
            records.c.id,
            records.c.account_id,
            records.c.execution_id,
            records.c.input_id,
            records.c.result_input_id,
            asset_binding.c.asset_id,
        )
        .select_from(asset_binding.join(records, asset_binding.c.record_id == records.c.id))
        .where(
            asset_binding.c.asset_id.in_([snapshot.asset_fact_id for snapshot in indirect]),
            asset_binding.c.path == "/account_assets",
        )
    )
    owners = list((await session.execute(statement)).mappings())
    inputs = SQLModel.metadata.tables["execution_input"]
    input_ids = [row["input_id"] for row in roots.values() if row["input_id"] is not None]
    input_rows = (
        list(
            (
                await session.execute(
                    sa.select(
                        inputs.c.id, inputs.c.account_id, inputs.c.execution_id, inputs.c.owner_execution_id
                    ).where(inputs.c.id.in_(input_ids))
                )
            ).mappings()
        )
        if input_ids
        else []
    )
    for snapshot in indirect:
        matching = [
            row
            for row in owners
            if row["asset_id"] == snapshot.asset_fact_id
            and row["account_id"] == snapshot.account_id
            and row["execution_id"] == snapshot.execution_id
        ]
        if not matching:
            raise ValueError("账户资产观测引用与账户或执行不匹配")
        for owner in matching:
            validate_result_asset_owners(owner, [roots[snapshot.asset_fact_id]], input_rows)


async def read_snapshot_assets(session: Any, snapshot: Any, *, include_positions: bool = True) -> dict[str, object]:
    """显式读取一条资产观测，缺失结构化数据直接报错。"""
    return (await read_snapshots_assets(session, [snapshot], include_positions=include_positions))[snapshot.id]
