"""一份冻结输入、三种业务目标及稀疏审计异常。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from axile.server.business_facts.columns import audit_value, read_columns, store_columns
from axile.server.business_facts.extensions import sanitize_input

_INPUT_FIELDS = {
    "channel_type": "text",
    "execution_timeout": "integer",
    "algorithm": "json",
    "symbol_algorithms": "json",
    "trade_rules": "json",
    "futures_close_policy": "json",
    "feishu_key": "json",
    "feishu_account": "json",
    "extra": "json",
}
TARGET_ROLES = ("curr_target", "last_target", "strategy_target")
_TARGET_ALIASES = {
    "account_target": "curr_target",
    "account_weights": "curr_target",
    "normalized_weights": "curr_target",
    "strategy_weights": "strategy_target",
    "raw_weights": "strategy_target",
}
_SYMBOL_ROLES = ("forbidden_symbols", "risk_symbols")
_SIZING_FIELDS: dict[str, str] = dict.fromkeys(("long_leverage", "short_leverage", "weight_precision"), "number")
_PLAN_FIELDS = ("quantities", "target_units")
_QUANTITY_ALIASES = ("target_quantities", "target_quantity", "target_volume")
_CHILD_FIELDS = (*TARGET_ROLES, *_SYMBOL_ROLES, "sizing_context", *_PLAN_FIELDS)


def _config_column(name: str, kind: str) -> sa.Column:
    if kind == "text":
        return sa.Column(name, sa.Text)
    if kind == "integer":
        return sa.Column(name, sa.Integer)
    return sa.Column(name, sa.JSON)


execution_input = sa.Table(
    "execution_input",
    SQLModel.metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
    sa.Column("execution_id", sa.Text, nullable=True, unique=True),
    sa.Column("owner_execution_id", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("root_kind", sa.Text, nullable=False),
    sa.Column("root_value", sa.JSON),
    *[_config_column(name, kind) for name, kind in _INPUT_FIELDS.items()],
    *[sa.Column(name, sa.Float) for name in _SIZING_FIELDS],
    *[sa.Column(role + "_state", sa.Text, nullable=False) for role in _CHILD_FIELDS],
    *[
        sa.Column(role + "_count", sa.Integer)
        for role in (*TARGET_ROLES, *_PLAN_FIELDS, "forbidden_symbols", "risk_symbols")
    ],
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    sa.Index("ix_execution_input_account_id_created_at", "account_id", "created_at"),
    sa.Index("ix_execution_input_owner_execution_id", "owner_execution_id"),
)
execution_input_target = sa.Table(
    "execution_input_target",
    SQLModel.metadata,
    sa.Column("input_id", sa.Text, sa.ForeignKey("execution_input.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role", sa.Text, primary_key=True),
    sa.Column("map_key", sa.Text, primary_key=True),
    sa.Column("value", sa.Float),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    sqlite_with_rowid=False,
)
execution_input_symbol = sa.Table(
    "execution_input_symbol",
    SQLModel.metadata,
    sa.Column("input_id", sa.Text, sa.ForeignKey("execution_input.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role", sa.Text, primary_key=True),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("value", sa.Text),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
)
execution_input_plan = sa.Table(
    "execution_input_plan",
    SQLModel.metadata,
    sa.Column("input_id", sa.Text, sa.ForeignKey("execution_input.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("quantity", sa.Float),
    sa.Column("units", sa.Float),
    sa.Column("quantity_state", sa.Text, nullable=False),
    sa.Column("units_state", sa.Text, nullable=False),
    sa.Column("audit_evidence", sa.JSON, nullable=False),
    sqlite_with_rowid=False,
)
TABLES = (execution_input, execution_input_target, execution_input_symbol, execution_input_plan)


def sanitize_input_payload(payload: object) -> object:
    """保留无法解释的历史根值；执行凭据不进入冻结事实。"""
    if isinstance(payload, Mapping):
        return sanitize_input(payload)
    if payload is None or isinstance(payload, (str, bool)) or type(payload) in (int, float) or payload == []:
        return payload
    raise ValueError("Unsupported execution input document root type")


def normalize_targets(payload: Mapping[str, object]) -> dict[str, object]:
    """字段别名归入业务角色，冲突值单独保留，不依赖键顺序。"""
    result = dict(payload)
    conflicts = {}
    for alias, role in _TARGET_ALIASES.items():
        if alias not in result:
            continue
        value = result.pop(alias)
        if role not in result:
            result[role] = value
        elif result[role] != value:
            conflicts[alias] = value
    if conflicts:
        result["target_conflicts"] = conflicts
    _normalize_plan_input(result)
    return result


def _normalize_plan_input(result: dict[str, object]) -> None:
    has_plan = "plan" in result
    plan = result.pop("plan", None)
    if has_plan and not isinstance(plan, Mapping):
        result["plan_evidence"] = plan
    if isinstance(plan, Mapping):
        remainder = {key: value for key, value in plan.items() if key not in {"quantities", "units", "availability"}}
        if remainder:
            result["plan_evidence"] = remainder
        if "quantities" in plan:
            result.setdefault("quantities", plan["quantities"])
        if "units" in plan:
            result.setdefault("target_units", plan["units"])
    quantity_conflicts = {}
    for alias in _QUANTITY_ALIASES:
        if alias not in result:
            continue
        value = result.pop(alias)
        if "quantities" not in result:
            result["quantities"] = value
        elif result["quantities"] != value:
            quantity_conflicts[alias] = value
    if quantity_conflicts:
        result["quantity_conflicts"] = quantity_conflicts


def _state(payload: Mapping[str, object], key: str, expected: type) -> str:
    evidence = payload.get("audit_evidence")
    invalid = evidence.get("invalid_collections") if isinstance(evidence, Mapping) else None
    if isinstance(invalid, Mapping) and key in invalid:
        return "invalid"
    if key not in payload:
        return "absent"
    value = payload[key]
    if value is None:
        return "null"
    return "value" if isinstance(value, expected) else "invalid"


def _resolve_owner_execution_id(execution_id: str | None, owner_execution_id: str | None) -> str | None:
    if execution_id is not None and owner_execution_id is not None and execution_id != owner_execution_id:
        raise ValueError("Execution input primary identity and owner differ")
    return owner_execution_id if owner_execution_id is not None else execution_id


def encode_inputs(
    input_id: str,
    payload: object,
    *,
    account_id: int,
    execution_id: str | None,
    created_at: str,
    owner_execution_id: str | None = None,
) -> dict[sa.Table, list[dict[str, object]]]:
    """直接映射冻结配置、业务目标和风险名单，不保存字段排列和数值表示。"""
    payload = sanitize_input_payload(payload)
    body = normalize_targets(payload) if isinstance(payload, Mapping) else {}
    columns, evidence = store_columns(body, _INPUT_FIELDS, child_keys=_CHILD_FIELDS)
    root = {
        "id": input_id,
        "account_id": account_id,
        "execution_id": execution_id,
        "owner_execution_id": _resolve_owner_execution_id(execution_id, owner_execution_id),
        "created_at": created_at,
        "root_kind": "object" if isinstance(payload, Mapping) else "null" if payload is None else "invalid",
        "root_value": None if isinstance(payload, Mapping) else audit_value(payload),
        **columns,
        **dict.fromkeys(_SIZING_FIELDS),
        "audit_evidence": evidence,
    }
    targets = []
    symbols = []
    previous_collections = evidence.get("invalid_collections", {})
    invalid_collections: dict[str, object] = (
        dict(previous_collections) if isinstance(previous_collections, Mapping) else {}
    )
    invalid_targets: dict[str, dict[str, object]] = {}
    invalid_symbols: dict[str, dict[str, object]] = {}
    for role in _CHILD_FIELDS:
        if role in _PLAN_FIELDS:
            continue
        expected = dict if role in TARGET_ROLES or role == "sizing_context" else list
        state = _state(body, role, expected)
        root[role + "_state"] = state
        if role != "sizing_context":
            value = body.get(role)
            root[role + "_count"] = len(value) if state == "value" and isinstance(value, (Mapping, list)) else None
        if state == "invalid":
            if role not in invalid_collections or body[role] is not None:
                invalid_collections[role] = audit_value(body[role])
        if state != "value":
            continue
        value = body[role]
        if role == "sizing_context":
            sizing, sizing_evidence = store_columns(cast(Mapping[str, object], value), _SIZING_FIELDS)
            root.update(sizing)
            if sizing_evidence:
                evidence["sizing"] = sizing_evidence
        elif role in TARGET_ROLES:
            rows, issues = _target_rows(input_id, role, cast(Mapping[str, object], value))
            targets.extend(rows)
            if issues:
                invalid_targets[role] = issues
        else:
            symbols.extend(_risk_rows(input_id, role, cast(Sequence[object], value), invalid_symbols))
    if invalid_collections:
        evidence["invalid_collections"] = invalid_collections
    if invalid_targets:
        evidence["invalid_target_values"] = invalid_targets
    if invalid_symbols:
        evidence["invalid_symbol_values"] = invalid_symbols
    plan = _encode_plan(input_id, body, root, evidence)
    return {
        execution_input: [root],
        execution_input_target: targets,
        execution_input_symbol: symbols,
        execution_input_plan: plan,
    }


def _risk_rows(
    input_id: str, role: str, values: Sequence[object], invalid_symbols: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    rows = []
    for ordinal, symbol in enumerate(values):
        typed, evidence = store_columns({"value": symbol}, {"value": "text"})
        invalid = evidence.get("invalid_fields")
        if isinstance(invalid, Mapping):
            invalid_symbols.setdefault(role, {})[str(ordinal)] = invalid["value"]
        rows.append({"input_id": input_id, "role": role, "ordinal": ordinal, **typed, "audit_evidence": evidence})
    return rows


def _target_rows(
    input_id: str, role: str, weights: Mapping[str, object]
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows = []
    issues = {}
    for symbol, weight in weights.items():
        if not isinstance(symbol, str):
            raise ValueError("Target symbol must be text")
        typed, evidence = store_columns({"value": weight}, {"value": "number"})
        rows.append({"input_id": input_id, "role": role, "map_key": symbol, **typed, "audit_evidence": evidence})
        if evidence:
            issues[symbol] = evidence
    return rows, issues


def _encode_plan(
    input_id: str, body: Mapping[str, object], root: dict[str, object], evidence: dict[str, object]
) -> list[dict[str, object]]:
    maps = {}
    for role in _PLAN_FIELDS:
        state = _state(body, role, dict)
        root[role + "_state"] = state
        value = body.get(role)
        root[role + "_count"] = len(value) if isinstance(value, Mapping) else None
        maps[role] = value if state == "value" and isinstance(value, Mapping) else {}
        if state == "invalid":
            issues = evidence.get("invalid_collections", {})
            if not isinstance(issues, dict):
                raise ValueError("Invalid input collection evidence")
            issues.setdefault(role, audit_value(value))
            evidence["invalid_collections"] = issues
    rows = []
    for symbol in set(maps["quantities"]) | set(maps["target_units"]):
        if not isinstance(symbol, str):
            raise ValueError("Quantity plan symbol must be text")
        payload = {
            key: maps[role][symbol]
            for role, key in (("quantities", "quantity"), ("target_units", "units"))
            if symbol in maps[role]
        }
        existing = evidence.get("invalid_plan_values", {})
        if isinstance(existing, Mapping) and symbol in existing:
            payload["audit_evidence"] = existing[symbol]
        columns, issues = store_columns(payload, {"quantity": "number", "units": "number"})
        if issues:
            invalid = evidence.get("invalid_plan_values", {})
            if not isinstance(invalid, dict):
                raise ValueError("Invalid quantity plan evidence")
            invalid[symbol] = issues
            evidence["invalid_plan_values"] = invalid
        invalid_fields = issues.get("invalid_fields")
        invalid_names = set(invalid_fields) if isinstance(invalid_fields, Mapping) else set()
        rows.append(
            {
                "input_id": input_id,
                "symbol": symbol,
                **columns,
                "audit_evidence": issues,
                **{
                    key + "_state": "absent"
                    if key not in payload
                    else "invalid"
                    if key in invalid_names
                    else "null"
                    if payload[key] is None
                    else "value"
                    if columns[key] is not None
                    else "invalid"
                    for key in ("quantity", "units")
                },
            }
        )
    return rows


def _plan_view(head: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    plan = {}
    for role, key in (("quantities", "quantity"), ("target_units", "units")):
        state = head[role + "_state"]
        if state == "absent":
            continue
        if state in {"null", "invalid"}:
            plan["quantities" if role == "quantities" else "units"] = None
            continue
        members = [row for row in rows if row[key + "_state"] != "absent"]
        if len(members) != head[role + "_count"]:
            raise ValueError("Incomplete frozen quantity plan")
        plan["quantities" if role == "quantities" else "units"] = {row["symbol"]: row[key] for row in members}
    if rows:
        plan["availability"] = {
            row["symbol"]: {"quantity": row["quantity_state"], "units": row["units_state"]} for row in rows
        }
    return plan


def _symbol_list(head: Mapping[str, object], rows: Sequence[Mapping[str, object]], role: str) -> list[object]:
    members = sorted((row for row in rows if row["role"] == role), key=lambda row: cast(int, row["ordinal"]))
    if len(members) != head[role + "_count"] or [row["ordinal"] for row in members] != list(range(len(members))):
        raise ValueError("Incomplete frozen risk symbol facts")
    return [row["value"] for row in members]


def decode_inputs(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> object:
    """返回业务 DTO；集合缺失、null、空集合和真实零分别保留。"""
    heads = rows.get(execution_input.name, ())
    if len(heads) != 1:
        raise ValueError("Expected exactly one execution input")
    head = heads[0]
    if head["root_kind"] != "object":
        return head["root_value"]
    evidence = head.get("audit_evidence") or {}
    if not isinstance(evidence, Mapping):
        raise ValueError("Invalid input audit evidence")
    result = read_columns(head, evidence, _INPUT_FIELDS)
    for role in _CHILD_FIELDS:
        if role in _PLAN_FIELDS:
            continue
        state = head[role + "_state"]
        if state == "absent":
            continue
        if state == "null":
            result[role] = None
        elif state == "invalid":
            result[role] = None
        elif state != "value":
            raise ValueError("Unknown input collection state")
        elif role == "sizing_context":
            sizing_evidence = evidence.get("sizing", {})
            if not isinstance(sizing_evidence, Mapping):
                raise ValueError("Invalid sizing audit evidence")
            result[role] = read_columns(head, sizing_evidence, _SIZING_FIELDS)
        elif role in TARGET_ROLES:
            if sum(row["role"] == role for row in rows.get(execution_input_target.name, ())) != head[role + "_count"]:
                raise ValueError("Incomplete frozen target facts")
            result[role] = {
                r["map_key"]: r["value"] for r in rows.get(execution_input_target.name, ()) if r["role"] == role
            }
        else:
            result[role] = _symbol_list(head, rows.get(execution_input_symbol.name, ()), role)
    plan = _plan_view(head, rows.get(execution_input_plan.name, ()))
    if plan:
        result["plan"] = plan
    return result


def canonical_input(payload: object) -> object:
    """比较冻结输入使用同一业务口径，忽略旧对象排列及数值表示。"""
    batch = encode_inputs("", payload, account_id=0, execution_id=None, created_at="")
    return decode_inputs({table.name: rows for table, rows in batch.items()})


async def _load_rows(
    session: Any, input_id: str, *, head_row: Mapping[str, object] | None = None
) -> dict[str, list[Mapping[str, object]]]:
    loaded = {}
    for table in TABLES:
        if table is execution_input and head_row is not None:
            if head_row["id"] != input_id:
                raise ValueError("Execution input header identity differs")
            loaded[table.name] = [head_row]
            continue
        key = table.c.id if table is execution_input else table.c.input_id
        loaded[table.name] = list((await session.execute(sa.select(table).where(key == input_id))).mappings())
    return loaded


async def read_input(session: Any, input_id: str, *, head_row: Mapping[str, object] | None = None) -> object:
    """显式加载冻结输入，可复用同一事务内已验证的头行；子行仍完整读取并校验。"""
    return decode_inputs(await _load_rows(session, input_id, head_row=head_row))


def _same_payload(left: object, right: object) -> bool:
    return left == canonical_input(right)


async def _existing_input(session: Any, account_id: int, execution_id: str, payload: object) -> str | None:
    row = (
        (
            await session.execute(
                sa.select(execution_input.c.id, execution_input.c.account_id).where(
                    execution_input.c.execution_id == execution_id
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row["account_id"] != account_id:
        raise ValueError("Execution input belongs to another account")
    if not _same_payload(await read_input(session, row["id"]), payload):
        raise ValueError("Execution input replay differs from frozen input")
    return row["id"]


async def persist_input(
    session: Any,
    *,
    account_id: int,
    execution_id: str | None,
    payload: object,
    created_at: str,
    owner_execution_id: str | None = None,
) -> str:
    """冻结输入但不提交事务，相同执行重放必须与已冻结内容一致。"""
    payload = sanitize_input_payload(payload)
    owner_execution_id = _resolve_owner_execution_id(execution_id, owner_execution_id)
    if execution_id is not None:
        existing = await _existing_input(session, account_id, execution_id, payload)
        if existing is not None:
            return existing
    input_id = str(uuid.uuid4())
    facts = encode_inputs(
        input_id,
        payload,
        account_id=account_id,
        execution_id=execution_id,
        created_at=created_at,
        owner_execution_id=owner_execution_id,
    )
    try:
        async with session.begin_nested():
            for table, batch in facts.items():
                if batch:
                    await session.execute(table.insert(), batch)
    except IntegrityError:
        if execution_id is None:
            raise
        existing = await _existing_input(session, account_id, execution_id, payload)
        if existing is None:
            raise
        return existing
    return input_id


async def persist_owned_input_variant(
    session: Any,
    *,
    account_id: int,
    owner_execution_id: str,
    payload: object,
    created_at: str,
    variant_key: str = "pre_execute_audit",
) -> str:
    """以明确执行归属和变体名称冻结独立输入，重放必须保持原文一致。"""
    payload = sanitize_input_payload(payload)
    identity = f"axile.input-evidence:{account_id}:{owner_execution_id}:{variant_key}"
    input_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    async def existing() -> bool:
        row = (
            (
                await session.execute(
                    sa.select(
                        execution_input.c.id,
                        execution_input.c.account_id,
                        execution_input.c.execution_id,
                        execution_input.c.owner_execution_id,
                    ).where(execution_input.c.id == input_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        if (
            row["account_id"] != account_id
            or row["execution_id"] is not None
            or row["owner_execution_id"] != owner_execution_id
        ):
            raise ValueError("Execution input variant identity has a different owner")
        if not _same_payload(await read_input(session, input_id), payload):
            raise ValueError("Execution input variant replay differs from frozen input")
        return True

    if await existing():
        return input_id
    facts = encode_inputs(
        input_id,
        payload,
        account_id=account_id,
        execution_id=None,
        owner_execution_id=owner_execution_id,
        created_at=created_at,
    )
    try:
        async with session.begin_nested():
            for table, batch in facts.items():
                if batch:
                    await session.execute(table.insert(), batch)
    except IntegrityError:
        if not await existing():
            raise
    return input_id


async def load_input_target(session: Any, input_id: str, *, role: str = "curr_target") -> tuple[bool, object]:
    """只读取一种业务目标；不从当前配置反推历史缺值。"""
    role = _TARGET_ALIASES.get(role, role)
    if role not in TARGET_ROLES:
        raise ValueError("Unknown execution input target role")
    row = (
        await session.execute(
            sa.select(execution_input.c[role + "_state"], execution_input.c[role + "_count"]).where(
                execution_input.c.id == input_id
            )
        )
    ).first()
    if row is None:
        raise ValueError("Execution input does not exist")
    if row[0] == "absent":
        return False, None
    if row[0] in {"null", "invalid"}:
        return True, None
    values = (
        await session.execute(
            sa.select(execution_input_target.c.map_key, execution_input_target.c.value)
            .where(execution_input_target.c.input_id == input_id, execution_input_target.c.role == role)
            .order_by(execution_input_target.c.map_key)
        )
    ).all()
    if len(values) != row[1]:
        raise ValueError("Incomplete frozen target facts")
    return True, dict(values)


async def find_input_id(session: Any, *, account_id: int, execution_id: str) -> str | None:
    """终态尚未存在时定位已提交的冻结输入。"""
    return await session.scalar(
        sa.select(execution_input.c.id).where(
            execution_input.c.account_id == account_id, execution_input.c.execution_id == execution_id
        )
    )


async def load_sizing_context(session: Any, input_id: str) -> tuple[bool, object]:
    """只读取冻结杠杆和精度，不使用当前账户配置补填。"""
    head = (
        (
            await session.execute(
                sa.select(
                    execution_input.c.sizing_context_state, *[execution_input.c[name] for name in _SIZING_FIELDS]
                ).where(execution_input.c.id == input_id)
            )
        )
        .mappings()
        .first()
    )
    if head is None:
        raise ValueError("Execution input does not exist")
    state = head["sizing_context_state"]
    return (
        (False, None)
        if state == "absent"
        else (True, read_columns(head, None, _SIZING_FIELDS) if state == "value" else None)
    )
