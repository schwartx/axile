"""离线对照的业务证据口径；不参与应用读写，也不恢复旧接口对象。"""

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal

_COLLECTIONS = {
    "symbol_results",
    "orders",
    "trades",
    "sizing",
    "first_tick",
    "first_ticks",
    "target_volume",
    "account_assets",
    "positions",
    "inputs",
    "termination",
    "cancel_failed_order_ids",
    "curr_target",
    "last_target",
    "strategy_target",
    "risk_symbols",
    "forbidden_symbols",
    "sizing_context",
}
_NATIVE_JSON = {"extra", "memory", "reference", "algorithm", "symbol_algorithms", "trade_rules", "futures_close_policy"}
_TARGETS = {
    "curr_target",
    "last_target",
    "strategy_target",
    "raw_weights",
    "normalized_weights",
    "strategy_weights",
    "account_weights",
    "account_target",
    "quantities",
    "units",
    "target_volume",
}
_ALIASES = {
    "account_target": "curr_target",
    "account_weights": "curr_target",
    "normalized_weights": "curr_target",
    "strategy_weights": "strategy_target",
    "raw_weights": "strategy_target",
}


def _restore_audit(body: dict) -> None:
    evidence = body.pop("audit_evidence", None)
    if not isinstance(evidence, Mapping):
        return
    for section in ("invalid_fields", "unrecognized_fields"):
        fields = evidence.get(section)
        if isinstance(fields, Mapping):
            body.update(fields)
    _restore_plan_issues(body, evidence)
    for role, weights in evidence.get("invalid_target_values", {}).items():
        if isinstance(body.get(role), dict):
            for symbol, issue in weights.items():
                body[role][symbol] = issue["invalid_fields"]["value"]
    for role, items in evidence.get("invalid_symbol_values", {}).items():
        if isinstance(body.get(role), list):
            for ordinal, original in items.items():
                body[role][int(ordinal)] = original
    if isinstance(body.get("cancel_failed_order_ids"), list):
        for ordinal, original in evidence.get("invalid_cancel_order_ids", {}).items():
            body["cancel_failed_order_ids"][int(ordinal)] = original


def _restore_plan_issues(body: dict, evidence: Mapping) -> None:
    for key, value in evidence.get("invalid_collections", {}).items():
        if key in {"quantities", "target_units"}:
            body.setdefault("plan", {})["quantities" if key == "quantities" else "units"] = value
        else:
            body[key] = value
    for symbol, issue in evidence.get("invalid_plan_values", {}).items():
        for key, value in issue.get("invalid_fields", {}).items():
            role = "quantities" if key == "quantity" else "units"
            body.setdefault("plan", {}).setdefault(role, {})[symbol] = value


def _normalize_input(body: dict) -> None:
    conflicts = {}
    for alias, role in _ALIASES.items():
        if alias not in body:
            continue
        original = body.pop(alias)
        if role not in body:
            body[role] = original
        elif business_evidence(body[role], context=role) != business_evidence(original, context=role):
            conflicts[alias] = original
    if conflicts:
        body["target_conflicts"] = conflicts
    _normalize_plan(body)


def _normalize_plan(body: dict) -> None:
    plan = dict(body.get("plan", {})) if isinstance(body.get("plan"), Mapping) else {}
    conflicts = {}
    for alias in ("quantities", "target_quantities", "target_quantity", "target_volume"):
        if alias not in body:
            continue
        quantities = body.pop(alias)
        if "quantities" not in plan:
            plan["quantities"] = quantities
        elif business_evidence(plan["quantities"], context="quantities") != business_evidence(
            quantities, context="quantities"
        ):
            conflicts[alias] = quantities
    if "target_units" in body:
        plan["units"] = body.pop("target_units")
    if plan:
        body["plan"] = plan
    if conflicts:
        body["quantity_conflicts"] = conflicts


def business_evidence(value: object, *, context: str = "", native: bool = False) -> object:
    """对照业务事实及原生审计证据，集合 null/空/缺失及观察顺序参与比较。"""
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ("invalid_number", "nan" if math.isnan(value) else "inf" if value > 0 else "-inf")
        return ("number", str(Decimal(str(value)).normalize()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [business_evidence(item, context=context, native=native) for item in value]
    if not isinstance(value, Mapping):
        return value
    if set(value) == {"invalid_number"}:
        return ("invalid_number", value["invalid_number"])
    body = deepcopy(dict(value))
    if not native:
        _restore_audit(body)
        for key in ("availability", "task_status", "business_facts_ref"):
            body.pop(key, None)
        if context == "input" or any(key in body for key in ("curr_target", "last_target", "strategy_target")):
            _normalize_input(body)
    return {
        key: business_evidence(item, context=key, native=native or key in _NATIVE_JSON)
        for key, item in body.items()
        if item is not None or native or context in _TARGETS or key in _COLLECTIONS
    }
