"""目标展示和持仓对账共用冻结的数量换算事实。"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from axile.common.serialization import from_payload, to_payload
from axile.executor.models.value_conversion import PriceValueConversion
from axile.server.native_store import native_operation
from axile.server.target_weight_snapshots import target_snapshot_public


def _canonical_map(values: dict[str, float] | None, canonicalize: Callable[[str], str]) -> dict[str, float]:
    return {canonicalize(key): float(value) for key, value in (values or {}).items()}


def _valid_targets(values: object) -> bool:
    return isinstance(values, dict) and all(
        isinstance(key, str)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for key, value in values.items()
    )


def _same_targets(left: dict[str, Any], right: dict[str, Any], canonicalize: Callable[[str], str]) -> bool:
    if left.get("target_mode", "weight") != right.get("target_mode", "weight") or not _valid_targets(
        left.get("normalized_weights")
    ):
        return False
    a = _canonical_map(left.get("normalized_weights"), canonicalize)
    b = _canonical_map(right.get("normalized_weights"), canonicalize)
    return a.keys() == b.keys() and all(math.isclose(a[key], b[key], rel_tol=1e-9, abs_tol=1e-12) for key in a)


def _sizing_rows(
    evidence: dict[str, Any], snapshot: dict[str, Any], canonicalize: Callable[[str], str]
) -> dict[str, dict[str, Any]]:
    strategy = _canonical_map(snapshot.get("raw_weights"), canonicalize)
    targets = _canonical_map(snapshot.get("normalized_weights"), canonicalize)
    quantity_mode = snapshot.get("target_mode", "weight") == "quantity"
    rows = {}
    for key, raw in evidence["sizings"].items():
        symbol = canonicalize(key)
        if (raw.get("sizing_mode", "weight") == "quantity") != quantity_mode:
            continue
        if not all(
            isinstance(raw.get(name, default), str)
            for name, default in (("reason_code", None), ("sizing_mode", "weight"), ("status", "SIZED"))
        ):
            continue
        try:
            row = {
                "symbol": symbol,
                "sizing_mode": raw.get("sizing_mode", "weight"),
                "status": raw.get("status", "SIZED"),
                "reason_code": raw["reason_code"],
                **{
                    name: None if raw.get(name) is None else float(raw[name])
                    for name in (
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
                    )
                },
                "value_conversion": None
                if raw.get("value_conversion") is None
                else to_payload(from_payload(PriceValueConversion, raw["value_conversion"]), mode="json"),
            }
        except (TypeError, ValueError, OverflowError):
            # 历史证据不完整时不得用当前行情和交易规则补齐。
            continue
        if any(isinstance(value, float) and not math.isfinite(value) for value in row.values()):
            continue
        weight, strategy_weight = targets.get(symbol), strategy.get(symbol)
        row.update(
            strategy_weight=None if quantity_mode else strategy_weight,
            account_weight=None if quantity_mode else targets.get(symbol, raw.get("account_weight")),
            account_multiplier=None
            if quantity_mode or strategy_weight in (None, 0.0) or weight is None
            else weight / strategy_weight,
            weight_precision=None if quantity_mode else evidence["target_context"].get("weight_precision"),
        )
        rows[symbol] = row
    return rows


def _evidence(
    snapshot: dict[str, Any], executions: dict[str, Any], canonicalize: Callable[[str], str]
) -> dict[str, Any]:
    execution_id = snapshot["execution_id"]
    status = "pending_execution" if snapshot["source"] == "manual" or not execution_id else "unavailable"
    rows: dict[str, dict[str, Any]] = {}
    inflight = False
    quantities = None
    incomplete = False
    if execution_id:
        evidence = executions[execution_id]
        if evidence["record_id"] is not None:
            rows = _sizing_rows(evidence, snapshot, canonicalize)
            required = _canonical_map(snapshot.get("normalized_weights"), canonicalize)
            quantities = {
                symbol: row["target_quantity"]
                for symbol, row in rows.items()
                if row["target_quantity"] is not None
                and math.isfinite(row["target_quantity"])
                and row["status"] != "UNAVAILABLE"
            }
            if (
                required.keys() <= quantities.keys()
                and {canonicalize(key) for key in evidence["sizings"]} <= rows.keys()
                and all(
                    row["target_quantity"] is not None
                    and math.isfinite(row["target_quantity"])
                    and row["status"] != "UNAVAILABLE"
                    for row in rows.values()
                )
            ):
                status = "available"
            else:
                quantities = None
                incomplete = any(symbol in rows and rows[symbol]["target_quantity"] is None for symbol in required)
        elif evidence["input_id"] is not None or evidence["artifact_versions"].get("target_snapshot", 0) >= 2:
            status, inflight = "pending_execution", True
        else:
            status = "legacy"
    return {
        "status": status,
        "rows": rows,
        "quantities": quantities,
        "inflight": inflight,
        "incomplete": incomplete,
        "execution_id": execution_id,
        "calculated_at": snapshot["calculated_at"],
    }


def resolve_account_target(bundle: dict[str, Any], canonicalize: Callable[[str], str]) -> dict[str, Any]:
    """从同一读快照的事实中选择一份完整证据，不拼接不同执行的数量。"""
    snapshot = bundle["snapshot"]
    if snapshot is None:
        return target_snapshot_public(None, weight_kind="normalized")
    if not _valid_targets(snapshot.get("normalized_weights")) or (
        snapshot.get("raw_weights") is not None and not _valid_targets(snapshot["raw_weights"])
    ):
        return {
            **target_snapshot_public(None, weight_kind="normalized"),
            "target_mode": snapshot.get("target_mode", "weight"),
            "calculated_at": snapshot["calculated_at"],
            "sizing": {
                "status": "unavailable",
                "rows": {},
                "execution_id": snapshot["execution_id"],
                "calculated_at": snapshot["calculated_at"],
            },
        }
    evidence = _evidence(snapshot, bundle["executions"], canonicalize)
    if evidence["status"] != "available" and not evidence["inflight"]:
        for previous in bundle["previous"]:
            if not _same_targets(previous, snapshot, canonicalize):
                continue
            candidate = _evidence(previous, bundle["executions"], canonicalize)
            if candidate["status"] == "available":
                # 展示权重属于当前目标，数量与换算上下文仍属于历史执行。
                historical = bundle["executions"][previous["execution_id"]]
                candidate["rows"] = _sizing_rows(historical, snapshot, canonicalize)
                evidence = candidate
                break
        else:
            if evidence["status"] == "unavailable" and not evidence["incomplete"]:
                evidence["status"] = "pending_execution"
    return target_snapshot_public(
        snapshot,
        weight_kind="normalized",
        weights=_canonical_map(snapshot.get("normalized_weights"), canonicalize),
        quantities=evidence["quantities"],
        strategy_weights=_canonical_map(snapshot.get("raw_weights"), canonicalize),
        account_weights=_canonical_map(snapshot.get("normalized_weights"), canonicalize),
        sizing={key: evidence[key] for key in ("status", "rows", "execution_id", "calculated_at")},
    )


async def read_account_target(account: object, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """按确切目标快照读取事实，避免刷新响应混入后来的目标。"""
    if snapshot is None:
        return target_snapshot_public(None, weight_kind="normalized")
    from axile.channels import get_channel

    bundles = await native_operation(
        "target_operation",
        {
            "op": "account_evidence",
            "pairs": [
                {
                    "account_id": snapshot["account_id"],
                    "portfolio_id": snapshot["portfolio_id"],
                    "snapshot_id": snapshot["id"],
                }
            ],
        },
    )
    return resolve_account_target(
        bundles[str(snapshot["account_id"])], get_channel(getattr(account, "trade_channel")).canonicalize_symbol
    )
