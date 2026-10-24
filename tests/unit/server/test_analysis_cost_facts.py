"""Structured analysis facts preserve API semantics without decoding historical JSON."""

from collections import defaultdict

import pytest

from tests.unit.server._analysis_cost_facts_oracle import iter_cost_fact_batches
from tests.unit.server._business_analysis_support import record
from tests.unit.server._performance_costs_oracle import project_execution, timestamp


def _project_rows(records):
    projected, costs = [], []
    for source in records:
        payload, trades = project_execution(source)
        projected.append(
            {
                "record_id": source.id,
                "time": timestamp(source.created_at),
                "success": source.is_success == 1,
                "noop": payload["noop"],
                "payload": payload,
            }
        )
        for trade in trades:
            costs.append(
                {
                    "id": len(costs),
                    "record_id": source.id,
                    "symbol": trade["symbol"],
                    "time": trade["time"],
                    "day": trade["day"],
                    "value": trade["value"],
                    "cost": trade["cost"],
                    "loss_bp": trade["lossBp"],
                    "fee": trade["fee"],
                    "fee_currency": trade["feeCurrency"],
                    "estimated": trade["timeEstimated"],
                    "payload": trade,
                }
            )
    return projected, costs


def _ranges():
    return {
        "all": {"performance": {"baseline": "2026-01-01T09:00:00", "end": "2026-01-02T09:00:00"}},
        "30": {"performance": {"baseline": None, "end": None}},
        "90": {"performance": {"baseline": "2026-01-01T09:00:00", "end": "2026-01-02T09:00:00"}},
    }


def _rich_projection():
    source = record()
    raw = source.result
    raw.update(status="PARTIAL", outcome="not_reached", error="未全部成交", reason_code=None, msg="")
    raw["account_assets"]["positions"] = [
        {"symbol": "A", "volume": -2, "direction": "SHORT"},
        {"symbol": "FLAT", "volume": 0, "direction": "LONG"},
    ]
    raw["symbol_results"]["A"].update(status="PARTIAL", outcome="not_reached", error="执行不到位", reason_code=None)
    raw["symbol_results"]["A"]["sizing"].update(current_quantity=0, target_quantity=3)
    raw["symbol_results"]["A"]["orders"][0]["filled_volume"] = 2
    raw["symbol_results"]["B"] = {
        "status": "SUCCEEDED",
        "outcome": "completed",
        "first_tick": {"last_price": 50},
        "orders": [{"order_id": "sell", "direction": "SELL"}],
        "trades": [{"order_id": "sell", "trade_price": 51, "trade_volume": 1}],
    }
    return _project_rows([source])


def _group_facts(projected, costs, batch_size=500):
    result = defaultdict(list)
    for table, rows in iter_cost_fact_batches("snapshot", projected, costs, _ranges(), batch_size=batch_size):
        result[table.name].extend(rows)
    return result


def test_cost_facts_reconstruct_status_presence_positions_and_attempts():
    """Null, absent and empty evidence remain distinct in scalar and child facts."""
    projected, costs = _rich_projection()
    facts = _group_facts(projected, costs)
    original = projected[0]["payload"]
    execution = facts["performance_cost_execution"][0]
    raw = original["record"]["raw_result"]
    fields = ("status", "task_status", "error", "outcome", "outcome_reason", "reason_code", "msg", "execution_kind")
    rebuilt_raw = {
        name: execution["raw_" + name] for index, name in enumerate(fields) if execution["raw_present"] & (1 << index)
    }
    assert rebuilt_raw == {name: raw[name] for name in fields if name in raw}
    assert rebuilt_raw["reason_code"] is None
    assert rebuilt_raw["msg"] == ""
    assert "execution_kind" not in rebuilt_raw
    assert execution["positions_present"] is True
    assert execution["position_count"] == 1
    assert [
        {key: value for key, value in row.items() if key not in {"snapshot_id", "record_id", "idx"}}
        for row in facts["performance_cost_position"]
    ] == original["positions"]
    assert [
        {key: value for key, value in row.items() if key not in {"snapshot_id", "record_id", "idx"}}
        for row in facts["performance_cost_attempt"]
    ] == original["attempts"]
    assert execution["payload"] == original
    symbols = {row["symbol"]: row for row in facts["performance_cost_execution_symbol"]}
    assert symbols["A"]["reason_code"] is None
    assert symbols["A"]["present"] & (1 << 4)
    assert not symbols["B"]["present"] & (1 << 4)


def test_cost_facts_preserve_unknown_money_and_zero_fees():
    """Unpriced trades stay unknown while an explicit zero fee stays covered."""
    projected, costs = _rich_projection()
    facts = _group_facts(projected, costs)
    trades = {row["symbol"]: row for row in facts["performance_cost_trade"]}
    assert trades["A"]["value"] == 2060
    assert trades["A"]["cost"] == 40
    assert trades["A"]["fee"] == 0
    assert trades["B"]["value"] is None
    assert trades["B"]["cost"] is None
    assert trades["B"]["fee"] is None
    assert trades["B"]["side"] == "sell"
    assert trades["B"]["reference_source"] == "last"
    transactions = {row["symbol"]: row for row in facts["performance_cost_transaction"]}
    assert transactions["A"]["amount_complete"] is True
    assert transactions["A"]["fee_covered"] == 1
    assert transactions["B"]["amount_complete"] is False
    assert transactions["B"]["fee_covered"] == 0
    assert facts["performance_cost_transaction_fee"] == [
        {"snapshot_id": "snapshot", "record_id": 1, "transaction_idx": 0, "currency": "CNY", "value": 0}
    ]


def test_cost_facts_distinguish_unknown_positions_from_explicit_flat():
    """No child positions can mean unavailable or flat; the scalar presence bit disambiguates."""
    unavailable = record(1)
    flat = record(2, account_assets={"total_asset": 102, "positions": []})
    projected, costs = _project_rows([unavailable, flat])
    facts = _group_facts(projected, costs)
    first, second = facts["performance_cost_execution"]
    assert first["positions_present"] is False
    assert first["position_count"] is None
    assert second["positions_present"] is True
    assert second["position_count"] == 0
    assert facts["performance_cost_position"] == []


def test_cost_fact_batches_stream_and_limit_each_insert():
    """Range facts can be emitted before consuming history and every insert stays bounded."""
    projected, costs = _project_rows([record(index) for index in range(1, 8)])
    consumed = []

    def history():
        for row in projected:
            consumed.append(row["record_id"])
            yield row

    batches = iter_cost_fact_batches("snapshot", history(), iter(costs), _ranges(), batch_size=2)
    remaining = list(batches)
    assert all(0 < len(rows) <= 2 for _, rows in remaining)
    assert consumed == list(range(1, 8))
    assert sum(len(rows) for table, rows in remaining if table.name == "performance_cost_execution") == 7
    assert sum(len(rows) for table, rows in remaining if table.name == "performance_cost_trade") == len(costs)
    assert not any(table.name == "performance_cost_range" for table, _ in remaining)
    with pytest.raises(ValueError, match="batch_size"):
        list(iter_cost_fact_batches("snapshot", [], [], {}, batch_size=0))
