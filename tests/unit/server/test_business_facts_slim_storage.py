"""以业务事实验证精简存储，不要求恢复旧 JSON 表示。"""

import asyncio
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts import assets, head, inputs, orders, repository, trades
from axile.server.business_facts.columns import read_columns, store_columns
from axile.server.business_facts.summary_refs import compact_summary_content, hydrate_summary_content
from axile.server.execution import input_persistence


def test_columns_store_facts_without_a_second_json_description():
    first, evidence = store_columns({"price": 2, "size": 0.0}, {"price": "number", "size": "number"})
    other, other_evidence = store_columns({"size": 0, "price": 2.0}, {"price": "number", "size": "number"})
    assert first == other == {"price": 2, "size": 0}
    assert evidence == other_evidence == {}
    assert read_columns(first, evidence, {"price": "number", "size": "number"}) == {"price": 2, "size": 0}


@pytest.mark.parametrize("bad", ["bad", float("nan"), float("inf"), 2**1000 + 1])
def test_invalid_amount_is_auditable_and_never_becomes_zero(bad):
    columns, evidence = store_columns({"amount": bad}, {"amount": "number"})
    assert columns["amount"] is None
    assert "amount" in evidence["invalid_fields"]
    json.dumps(evidence, allow_nan=False)


def test_target_aliases_do_not_create_duplicate_facts():
    batch = inputs.encode_inputs(
        "one",
        {
            "curr_target": {"A": 0},
            "account_target": {"A": 0.0},
            "normalized_weights": {"A": 0},
            "strategy_target": {"A": 0.3},
            "raw_weights": {"A": 0.3},
            "last_target": {"A": -0.2},
        },
        account_id=1,
        execution_id="execution",
        created_at="2026-10-20",
    )
    rows = batch[inputs.execution_input_target]
    assert len(rows) == 3
    assert {(row["role"], row["map_key"], row["value"]) for row in rows} == {
        ("curr_target", "A", 0),
        ("strategy_target", "A", 0.3),
        ("last_target", "A", -0.2),
    }
    assert "ordinal" not in inputs.execution_input_target.c
    assert not inputs.execution_input_target.indexes
    assert len([c for c in inputs.execution_input_target.constraints if isinstance(c, sa.UniqueConstraint)]) == 0


@pytest.mark.parametrize(
    "target,state,present",
    [("missing", "absent", False), (None, "null", True), ({}, "value", True), ({"A": 0}, "value", True)],
)
def test_target_missing_null_empty_and_zero_have_distinct_meaning(target, state, present):
    payload = {} if target == "missing" else {"curr_target": target}
    batch = inputs.encode_inputs("one", payload, account_id=1, execution_id=None, created_at="now")
    assert batch[inputs.execution_input][0]["curr_target_state"] == state
    view = inputs.decode_inputs({table.name: values for table, values in batch.items()})
    assert ("curr_target" in view) is present
    if present:
        assert view["curr_target"] == target


def test_missing_target_children_raise_instead_of_becoming_empty_target():
    batch = inputs.encode_inputs("one", {"curr_target": {"A": 1}}, account_id=1, execution_id=None, created_at="now")
    batch[inputs.execution_input_target].clear()
    with pytest.raises(ValueError, match="Incomplete frozen target"):
        inputs.decode_inputs({table.name: values for table, values in batch.items()})


def test_quantity_and_contract_units_are_separate_frozen_business_facts():
    batch = inputs.encode_inputs(
        "one",
        {"target_quantities": {"A": 0, "B": None}, "target_units": {"A": 2}},
        account_id=1,
        execution_id=None,
        created_at="now",
    )
    view = inputs.decode_inputs({table.name: rows for table, rows in batch.items()})
    assert view["plan"]["quantities"] == {"A": 0, "B": None}
    assert view["plan"]["units"] == {"A": 2}
    assert view["plan"]["availability"]["B"] == {"quantity": "null", "units": "absent"}
    assert len(batch[inputs.execution_input_plan]) == 2
    batch[inputs.execution_input_plan].clear()
    with pytest.raises(ValueError, match="Incomplete frozen quantity plan"):
        inputs.decode_inputs({table.name: rows for table, rows in batch.items()})


@pytest.mark.parametrize("native_id", [0, "0", "", None])
def test_native_order_identity_is_not_coerced(native_id):
    batch = orders.encode_orders(1, {"orders": [{"order_id": native_id}]})
    restored = orders.decode_orders({table.name: rows for table, rows in batch.items()})["orders"][0]
    assert restored["order_id"] == native_id
    assert type(restored["order_id"]) is type(native_id)


def test_missing_positions_and_cancellation_children_expose_incomplete_facts():
    batch = assets.encode_assets(1, {"account_assets": {"positions": [{"symbol": "A", "volume": 0}]}})
    batch[assets.asset_position].clear()
    with pytest.raises(ValueError, match="Incomplete position facts"):
        assets.decode_assets({table.name: rows for table, rows in batch.items()})
    batch = head.encode_head(1, {"termination": {"cancel_failed_order_ids": [0, None]}})
    batch[head.termination_cancel_failure].clear()
    with pytest.raises(ValueError, match="Incomplete cancellation failure facts"):
        head.decode_head({table.name: rows for table, rows in batch.items()})


@pytest.mark.parametrize(
    "payload,state",
    [
        ({}, "absent"),
        ({"available_cash": None}, "null"),
        ({"available_cash": 0}, "value"),
        ({"available_cash": "bad"}, "invalid"),
    ],
)
def test_cash_observation_preserves_missing_null_zero_and_invalid_evidence(payload, state):
    batch = assets.encode_assets(1, {"account_assets": payload})
    row = batch[assets.asset_snapshot][0]
    view = assets.decode_assets({table.name: values for table, values in batch.items()})["account_assets"]
    assert row["cash_state"] == view["availability"]["cash"] == state
    if state == "value":
        assert view["available_cash"] == 0
    elif state == "null":
        assert view["available_cash"] is None
    elif state == "invalid":
        assert "available_cash" not in view
        assert view["audit_evidence"]["invalid_fields"]["available_cash"] == "bad"
    else:
        assert "available_cash" not in view


@pytest.mark.parametrize(
    "extra,state,fee",
    [
        ({}, "absent", None),
        ({"commission": None}, "null", None),
        ({"commission": 0}, "value", 0),
        ({"commission": -1}, "value", -1),
    ],
)
def test_fee_availability_and_actual_zero_are_not_guessed(extra, state, fee):
    batch = trades.encode_trades(1, {"trades": [{"trade_id": "0", "extra": extra}]})
    row = batch[trades.trade][0]
    assert row["fee_state"] == state
    assert row["fee"] == fee
    assert row["extra"] == extra


def test_summary_reference_preserves_distinct_evidence_without_field_projection_metadata():
    fact = {"status": "SUCCEEDED", "orders": [{"order_id": "0", "price": 2}], "sizing": {"target_quantity": 0}}
    result = {"symbol_results": {"A": fact}}
    content = {
        "symbol_results": {"A": {**fact, "sizing": {"target_quantity": 3}}},
        "independent": {"null": None, "zero": 0},
    }
    compact = compact_summary_content(content, result, record_id=1)
    assert compact["business_facts_ref"] == {"version": 1, "view": "result", "record_id": 1, "projection_version": 2}
    assert "orders" not in compact["symbol_results"]["A"]
    assert compact["symbol_results"]["A"]["sizing"] == {"target_quantity": 3}
    assert hydrate_summary_content(compact, result, include_reference=False) == content


def test_all_business_tables_have_no_generic_field_metadata():
    for table in repository.execution_fact_tables():
        assert "field_metadata" not in table.c
        assert "numeric_kinds" not in table.c


def test_equal_audit_targets_share_one_frozen_input(monkeypatch):
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(input_persistence, "SessionLocal", sessions)
            payload = {"curr_target": {"A": 0}, "last_target": {}, "strategy_target": {"A": 0.3}, "trade_rules": {}}
            primary, audit = await input_persistence._persist_pre_execute_bundle(
                account_id=1,
                execution_id="one",
                payload=payload,
                audit_payload={**payload, "account_target": {"A": 0.0}},
            )
            assert primary == audit
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(inputs.execution_input)) == 1
                assert await session.scalar(sa.select(sa.func.count()).select_from(inputs.execution_input_target)) == 2
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_explicit_invalid_input_state_survives_business_view_reencoding():
    first = inputs.canonical_input({"curr_target": ["invalid"]})
    assert first["curr_target"] is None
    batch = inputs.encode_inputs("one", first, account_id=1, execution_id=None, created_at="now")
    assert batch[inputs.execution_input][0]["curr_target_state"] == "invalid"
    assert batch[inputs.execution_input][0]["audit_evidence"]["invalid_collections"]["curr_target"] == ["invalid"]


def test_invalid_cash_state_survives_adoption_roundtrip():
    result = repository.business_result({"account_assets": {"available_cash": "not-observed"}})
    second = repository.business_result(result)
    assert second["account_assets"]["availability"]["cash"] == "invalid"
    assert second["account_assets"]["audit_evidence"]["invalid_fields"]["available_cash"] == "not-observed"
