"""冻结输入与历史目标的存储契约。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from axile.server.business_facts.inputs import (
    TABLES,
    decode_inputs,
    encode_inputs,
    execution_input,
    load_input_target,
    load_sizing_context,
    persist_input,
    persist_owned_input_variant,
    read_input,
    sanitize_input_payload,
)
from axile.server.target_weight_snapshots import decode_snapshot_weights, encode_snapshot_weights
from scripts.business_facts_contract import business_evidence


def _roundtrip(payload):
    facts = encode_inputs("input-1", payload, account_id=1, execution_id=None, created_at="2026-10-20T12:00:00")
    return decode_inputs({table.name: rows for table, rows in facts.items()}), facts


def test_inputs_keep_native_identity_targets_and_scoped_configuration():
    payload = {
        "strategy_target": {"CF2701": 0, "CF701": -0.05},
        "curr_target": {"CF2701": 0.0, "CF701": -0.15},
        "account_target": {"CF2701": None},
        "last_target": {},
        "quantities": {"CF701": -2},
        "risk_symbols": ["CF2701", "CF2701", None],
        "forbidden_symbols": [],
        "sizing_context": {"long_leverage": 0, "short_leverage": 3.0, "weight_precision": None, "native": "kept"},
        "algorithm": {"method": "TARGET-POS-TASK", "params": {"offset_priority": "今昨,开", "limit": 0}},
        "extra": {"audit": {"execution_id": "execution-1", "is_test": False}},
        "channel_type": "tq",
    }
    decoded, facts = _roundtrip(payload)
    assert business_evidence(decoded) == business_evidence(payload)
    assert business_evidence(list(decoded["curr_target"])) == business_evidence(["CF2701", "CF701"])
    assert business_evidence(decoded["strategy_target"]["CF2701"]) == business_evidence(0)
    assert business_evidence(decoded["curr_target"]["CF2701"]) == business_evidence(0)
    assert business_evidence(facts[execution_input][0]["audit_evidence"].get("extensions", {})) == business_evidence({})
    assert facts[execution_input][0]["audit_evidence"]["unrecognized_fields"]["target_conflicts"] == {
        "account_target": {"CF2701": None}
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"curr_target": None},
        {"curr_target": {}},
        {"curr_target": []},
        {"curr_target": {"rb2610": "0"}},
        None,
        False,
        0,
        "legacy",
        [],
    ],
)
def test_input_missing_null_empty_malformed_and_root_evidence(payload):
    assert business_evidence(_roundtrip(payload)[0]) == business_evidence(payload)


@pytest.mark.parametrize("payload", [{}, {"curr_target": None}, {"curr_target": {}}, {"curr_target": {"A": 0}}])
def test_read_input_reuses_header_without_changing_collection_states(payload):
    expected, facts = _roundtrip(payload)
    results = [
        SimpleNamespace(mappings=lambda rows=rows: rows)
        for table, rows in facts.items()
        if table is not execution_input
    ]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results))
    decoded = asyncio.run(read_input(session, "input-1", head_row=facts[execution_input][0]))
    assert decoded == expected
    assert session.execute.await_count == len(TABLES) - 1
    assert [call.args[0].get_final_froms()[0] for call in session.execute.await_args_list] == list(TABLES[1:])


def test_read_input_rejects_mismatched_header_before_querying():
    _, facts = _roundtrip({})
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(ValueError, match="header identity differs"):
        asyncio.run(read_input(session, "another-input", head_row=facts[execution_input][0]))
    session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "payload, missing_table, message",
    [
        ({"curr_target": {"A": 0}}, TABLES[1], "Incomplete frozen target facts"),
        ({"risk_symbols": ["A"]}, TABLES[2], "Incomplete frozen risk symbol facts"),
        ({"quantities": {"A": 0}}, TABLES[3], "Incomplete frozen quantity plan"),
    ],
)
def test_read_input_with_header_still_rejects_incomplete_children(payload, missing_table, message):
    _, facts = _roundtrip(payload)
    facts[missing_table] = []
    results = [
        SimpleNamespace(mappings=lambda rows=rows: rows)
        for table, rows in facts.items()
        if table is not execution_input
    ]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results))
    with pytest.raises(ValueError, match=message):
        asyncio.run(read_input(session, "input-1", head_row=facts[execution_input][0]))


def test_sanitation_is_identical_for_live_and_migration_and_does_not_mutate_input():
    payload = {
        "account_config": {"password": "synthetic-secret"},
        "feishu_key": "synthetic-key",
        "execution_notification_code": "synthetic-source",
        "feishu_account": {"id": 1},
        "curr_target": {"rb2610": 0},
    }
    decoded, _ = _roundtrip(payload)
    assert business_evidence(decoded) == business_evidence(sanitize_input_payload(payload))
    assert business_evidence(decoded["feishu_key"]) == business_evidence("***")
    assert "account_config" not in decoded
    assert "execution_notification_code" not in decoded
    assert business_evidence(payload["feishu_key"]) == business_evidence("synthetic-key")
    with pytest.raises(ValueError, match="root type"):
        _roundtrip([{"curr_target": {"rb2610": 0}}])


def test_persist_replays_identity_and_selective_target_reads():
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        metadata = sa.MetaData()
        account = sa.Table("account", metadata, sa.Column("id", sa.Integer, primary_key=True))
        for table in TABLES:
            table.to_metadata(metadata)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
                await connection.execute(account.insert(), [{"id": 1}, {"id": 2}])
            async with AsyncSession(engine) as session:
                payload = {"curr_target": {"rb2610": 0}, "sizing_context": {"weight_precision": 0.01}}
                kwargs = dict(account_id=1, execution_id="same-execution", payload=payload, created_at="2026-10-20")
                input_id = await persist_input(session, **kwargs)
                assert business_evidence(await persist_input(session, **kwargs)) == business_evidence(input_id)
                assert business_evidence(await read_input(session, input_id)) == business_evidence(payload)
                assert business_evidence(await load_input_target(session, input_id)) == business_evidence(
                    (True, {"rb2610": 0})
                )
                assert business_evidence(
                    await load_input_target(session, input_id, role="last_target")
                ) == business_evidence((False, None))
                assert business_evidence(await load_sizing_context(session, input_id)) == business_evidence(
                    (True, {"weight_precision": 0.01})
                )
                with pytest.raises(ValueError, match="differs"):
                    await persist_input(session, **{**kwargs, "payload": {"curr_target": {}}})
                with pytest.raises(ValueError, match="another account"):
                    await persist_input(session, **{**kwargs, "account_id": 2})
                first = await persist_input(
                    session, **{**kwargs, "execution_id": None, "payload": {"curr_target": None}}
                )
                second = await persist_input(
                    session, **{**kwargs, "execution_id": None, "payload": {"curr_target": {}}}
                )
                variant_one = await persist_input(
                    session,
                    **{
                        **kwargs,
                        "execution_id": None,
                        "owner_execution_id": "same-execution",
                        "payload": {"curr_target": {"rb2610": 1}},
                    },
                )
                variant_two = await persist_input(
                    session,
                    **{
                        **kwargs,
                        "execution_id": None,
                        "owner_execution_id": "same-execution",
                        "payload": {"curr_target": {"rb2610": 2}},
                    },
                )
                assert variant_one != variant_two
                owners = (
                    (
                        await session.execute(
                            sa.select(execution_input.c.owner_execution_id).where(
                                execution_input.c.id.in_([variant_one, variant_two])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert business_evidence(owners) == business_evidence(["same-execution", "same-execution"])
                assert first != second
                assert business_evidence(await load_input_target(session, first)) == business_evidence((True, None))
                assert business_evidence(await load_input_target(session, second)) == business_evidence((True, {}))
                ordered = {
                    **kwargs,
                    "execution_id": "ordered-target",
                    "payload": {"curr_target": {"rb2610": 0, "ag2612": 1}},
                }
                await persist_input(session, **ordered)
                assert await persist_input(
                    session, **{**ordered, "payload": {"curr_target": {"ag2612": 1, "rb2610": 0}}}
                ) == await persist_input(session, **ordered)
                with pytest.raises(ValueError, match="differs"):
                    await persist_input(
                        session,
                        **{
                            **kwargs,
                            "payload": {"curr_target": {"rb2610": 1.0}, "sizing_context": {"weight_precision": 0.01}},
                        },
                    )
                variant_kwargs = dict(
                    account_id=1,
                    owner_execution_id="same-execution",
                    payload={"curr_target": {"rb2610": 2}},
                    created_at="2026-10-20",
                )
                audit_id = await persist_owned_input_variant(session, **variant_kwargs)
                assert business_evidence(
                    await persist_owned_input_variant(session, **variant_kwargs)
                ) == business_evidence(audit_id)
                other_key_id = await persist_owned_input_variant(session, **variant_kwargs, variant_key="another-view")
                assert other_key_id != audit_id
                other_owner_id = await persist_owned_input_variant(
                    session, **{**variant_kwargs, "owner_execution_id": "another-execution"}
                )
                assert other_owner_id not in (audit_id, other_key_id)
                other_account_id = await persist_owned_input_variant(session, **{**variant_kwargs, "account_id": 2})
                assert other_account_id not in (audit_id, other_key_id, other_owner_id)
                with pytest.raises(ValueError, match="variant replay differs"):
                    await persist_owned_input_variant(
                        session, **{**variant_kwargs, "payload": {"curr_target": {"rb2610": 3}}}
                    )
                variant_row = (
                    (await session.execute(sa.select(execution_input).where(execution_input.c.id == audit_id)))
                    .mappings()
                    .one()
                )
                assert variant_row["execution_id"] is None
                assert business_evidence(variant_row["owner_execution_id"]) == business_evidence("same-execution")
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_weights": {"CF2701": 0, "CF701": -0.05}, "normalized_weights": {"CF2701": 0.0, "CF701": -0.15}},
        {"raw_weights": None, "normalized_weights": {}},
        {},
        {"raw_weights": [], "normalized_weights": {"rb2610": "0"}},
    ],
)
def test_calculated_snapshots_keep_weight_roles_and_container_states(payload):
    head, batches = encode_snapshot_weights(12, payload)
    child_rows = next(iter(batches.values()))
    assert business_evidence(decode_snapshot_weights(head, child_rows)) == business_evidence(payload)
    assert "raw_weights" not in head
    assert "normalized_weights" not in head


def test_target_snapshot_services_explicitly_load_typed_maps():
    from axile.server.db.models.target_weight_snapshot import TargetWeightSnapshot, target_weight_snapshot_weight
    from axile.server.target_weight_snapshots import (
        append_target_weight_snapshot,
        get_latest_account_target_snapshot,
        get_latest_portfolio_target_snapshot,
        read_target_snapshot_weights,
    )

    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        metadata = sa.MetaData()
        account = sa.Table("account", metadata, sa.Column("id", sa.Integer, primary_key=True))
        portfolio = sa.Table("portfolio", metadata, sa.Column("id", sa.Integer, primary_key=True))
        TargetWeightSnapshot.__table__.to_metadata(metadata)
        target_weight_snapshot_weight.to_metadata(metadata)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
                await connection.execute(account.insert(), [{"id": 1}])
                await connection.execute(portfolio.insert(), [{"id": 2}])
            async with AsyncSession(engine) as session:
                snapshot = await append_target_weight_snapshot(
                    session,
                    portfolio_id=2,
                    account_id=1,
                    raw_weights={"CF2701": 0, "CF701": -0.05},
                    normalized_weights={"CF2701": 0.0, "CF701": -0.15},
                    source="manual",
                )
                assert business_evidence(snapshot.raw_weights) == business_evidence({"CF2701": 0, "CF701": -0.05})
                assert business_evidence(snapshot.normalized_weights) == business_evidence(
                    {"CF2701": 0.0, "CF701": -0.15}
                )
                assert business_evidence(await get_latest_account_target_snapshot(session, 1, 2)) == business_evidence(
                    snapshot
                )
                assert business_evidence(await get_latest_portfolio_target_snapshot(session, 2)) == business_evidence(
                    snapshot
                )
                assert await read_target_snapshot_weights(session, account_id=1, execution_id=None) is None
                await session.execute(
                    sa.update(TargetWeightSnapshot)
                    .where(TargetWeightSnapshot.id == snapshot.id)
                    .values(execution_id="snapshot-execution")
                )
                assert business_evidence(
                    await read_target_snapshot_weights(session, account_id=1, execution_id="snapshot-execution")
                ) == business_evidence(
                    {
                        "CF2701": 0.0,
                        "CF701": -0.15,
                    }
                )
                assert "raw_weights" not in TargetWeightSnapshot.__table__.c
                assert "normalized_weights" not in TargetWeightSnapshot.__table__.c
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_input_variants_keep_owner_anchor_without_unique_execution_alias():
    ordinary = encode_inputs("primary", {}, account_id=1, execution_id="execution-1", created_at="2026-10-20")
    variant = encode_inputs(
        "variant", None, account_id=1, execution_id=None, created_at="2026-10-20", owner_execution_id="execution-1"
    )
    anonymous = encode_inputs("anonymous", {}, account_id=1, execution_id=None, created_at="2026-10-20")
    assert business_evidence(ordinary[execution_input][0]["owner_execution_id"]) == business_evidence("execution-1")
    assert business_evidence(variant[execution_input][0]["owner_execution_id"]) == business_evidence("execution-1")
    assert variant[execution_input][0]["execution_id"] is None
    assert anonymous[execution_input][0]["owner_execution_id"] is None
    assert decode_inputs({table.name: rows for table, rows in variant.items()}) is None
    assert "owner_execution_id" not in decode_inputs({table.name: rows for table, rows in ordinary.items()})


def test_input_primary_identity_cannot_point_at_a_different_owner():
    with pytest.raises(ValueError, match="primary identity and owner differ"):
        encode_inputs(
            "contradictory",
            {},
            account_id=1,
            execution_id="primary",
            created_at="2026-10-20",
            owner_execution_id="another",
        )

    async def check():
        with pytest.raises(ValueError, match="primary identity and owner differ"):
            await persist_input(
                None,
                account_id=1,
                execution_id="primary",
                payload={},
                created_at="2026-10-20",
                owner_execution_id="another",
            )

    asyncio.run(check())
