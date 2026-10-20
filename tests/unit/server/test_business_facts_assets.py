"""资产事实的身份、空值、可信度及选择性读取回归。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.business_facts.assets import (
    TABLES,
    asset_binding,
    asset_fact_id,
    asset_position,
    asset_snapshot,
    decode_assets,
    encode_assets,
    encode_input_after_assets,
    encode_input_assets,
    encode_snapshot_assets,
    ensure_execution_asset_snapshot,
    find_matching_after_asset,
    input_after_asset_fact_id,
    load_result_asset_rows,
    read_input_asset_observations,
    read_result_asset_totals,
    read_snapshots_assets,
    snapshot_asset_fact_id,
    validate_result_asset_ownership,
)
from scripts.business_facts_contract import business_evidence


def _metadata():
    metadata = sa.MetaData()
    sa.Table(
        "executerecord",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, server_default="1"),
        sa.Column("execution_id", sa.Text),
        sa.Column("input_id", sa.Text),
        sa.Column("result_input_id", sa.Text),
    )
    sa.Table(
        "execution_input",
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("account_id", sa.Integer, server_default="1"),
        sa.Column("execution_id", sa.Text),
        sa.Column("owner_execution_id", sa.Text),
    )
    sa.Table(
        "account_asset_snapshot",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer),
        sa.Column("asset_fact_id", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("execution_id", sa.Text, unique=True),
        sa.Column("created_at", sa.Text),
    )
    for table in TABLES:
        table.to_metadata(metadata)
    return metadata


def _roundtrip(payload):
    metadata = _metadata()
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    batch = encode_assets(17, payload)
    with engine.begin() as connection:
        connection.execute(metadata.tables["executerecord"].insert(), {"id": 17})
        for table, rows in batch.items():
            if rows:
                connection.execute(table.insert(), rows)
        loaded = {table.name: list(connection.execute(table.select()).mappings()) for table in TABLES}
    engine.dispose()
    return decode_assets(loaded), loaded


@pytest.mark.parametrize(
    "value", [None, 0, "missing", [], {}, {"positions": None}, {"positions": {}}, {"positions": []}]
)
def test_asset_container_and_positions_presence_roundtrip(value):
    payload = {"account_assets": value}
    restored, _ = _roundtrip(payload)
    assert business_evidence(restored) == business_evidence(payload)


def test_position_identity_and_native_channel_fields_are_not_merged():
    payload = {
        "account_assets": {
            "positions": [
                {
                    "symbol": "SHFE.rb2610",
                    "direction": "多头",
                    "volume": 2,
                    "available_volume": 0.0,
                    "market_value": None,
                    "avg_price": None,
                    "extra": {"position_attributes": {"today_volume": 2, "unknown_native": {"v": 0}}},
                },
                {"symbol": "SHFE.rb2610", "direction": "空头", "volume": 1.0},
                None,
                "legacy-position-error",
            ],
            "available_cash": 0,
            "market_value": None,
            "total_asset": 0,
            "source": "ctp-native",
            "update_time": "2026-10-19T21:00:00.123456789+08:00",
        }
    }
    restored, loaded = _roundtrip(payload)
    assert business_evidence(restored) == business_evidence(payload)
    assert restored["account_assets"]["positions"][0]["volume"] == payload["account_assets"]["positions"][0]["volume"]
    assert type(restored["account_assets"]["positions"][1]["volume"]) is float
    assert business_evidence([row["ordinal"] for row in loaded[asset_position.name]]) == business_evidence([0, 1, 2, 3])
    assert loaded[asset_snapshot.name][0]["observation_valid"]
    assert "realized_profit" not in restored["account_assets"]["positions"][0]["extra"]["position_attributes"]


@pytest.mark.parametrize(
    "source,valid", [("real", True), ("gm", True), ("assumed", False), ("error", False), ("unavailable", False)]
)
def test_real_zero_is_distinct_from_unavailable(source, valid):
    restored, loaded = _roundtrip({"account_assets": {"total_asset": 0, "source": source}})
    assert business_evidence(restored["account_assets"]["total_asset"]) == business_evidence(0)
    assert loaded[asset_snapshot.name][0]["observation_valid"] is valid


def test_exact_legacy_placeholder_is_frozen_without_dropping_evidence():
    payload = {
        "account_assets": {
            "available_cash": 0,
            "total_asset": 0,
            "market_value": 0,
            "positions": [],
            "extra": {},
            "source": "real",
        },
        "status": "FAILED",
        "extra": {"worker_error": {"type": "ValueError"}},
        "inputs": None,
        "execution_time": 0,
        "symbol_results": {},
    }
    _, loaded = _roundtrip(payload)
    assert loaded[asset_snapshot.name][0]["observation_valid"] is False
    assert business_evidence(decode_assets(loaded)["account_assets"]) == business_evidence(payload["account_assets"])
    changed = deepcopy(payload)
    changed["extra"] = {}
    _, real = _roundtrip(changed)
    assert real[asset_snapshot.name][0]["observation_valid"] is True


def test_symbol_map_key_path_and_embedded_position_symbol_are_separate():
    payload = {
        "account_assets": {"total_asset": 10},
        "symbol_results": {
            "BTC/USDT~hedge": {
                "account_assets": {"total_asset": 9, "positions": [{"symbol": "native-symbol", "volume": 1}]}
            },
            "": {"account_assets": None},
        },
    }
    restored, loaded = _roundtrip(payload)
    assert business_evidence(restored) == business_evidence(payload)
    paths = [row["path"] for row in loaded[asset_snapshot.name]]
    assert business_evidence(paths) == business_evidence(
        [
            "/account_assets",
            "/symbol_results/BTC~1USDT~0hedge/account_assets",
            "/symbol_results//account_assets",
        ]
    )
    assert business_evidence(len(set(row["asset_id"] for row in loaded[asset_snapshot.name]))) == business_evidence(3)


def test_worker_after_observation_binds_without_duplicate_snapshot(tmp_path):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'after.db'}")
        metadata = _metadata()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(metadata.tables["execution_input"].insert(), {"id": "pre-order-input"})
            await connection.execute(
                metadata.tables["executerecord"].insert(), {"id": 17, "input_id": "pre-order-input"}
            )
        sessions = async_sessionmaker(engine)
        before = {"account_assets": {"total_asset": 100, "update_time": "before", "positions": []}}
        after = {
            "account_assets": {"total_asset": 0, "update_time": "after", "positions": [{"symbol": "A", "volume": 0}]}
        }
        async with sessions() as session:
            for batch in (
                encode_input_assets("pre-order-input", before),
                encode_input_after_assets("pre-order-input", after),
            ):
                for table, rows in batch.items():
                    if rows:
                        await session.execute(table.insert(), rows)
            await session.commit()
            occurrences = await read_input_asset_observations(session, "pre-order-input")
            assert business_evidence({row["path"]: row["account_assets"] for row in occurrences}) == business_evidence(
                {
                    "/before_account_assets": before["account_assets"],
                    "/after_account_assets": after["account_assets"],
                }
            )
            linked_id = await find_matching_after_asset(session, "pre-order-input", after)
            assert business_evidence(linked_id) == business_evidence(input_after_asset_fact_id("pre-order-input"))
            batch = encode_assets(17, after, root_asset_id=linked_id)
            assert not batch[asset_snapshot]
            assert not batch[asset_position]
            assert business_evidence(batch[asset_binding][0]["asset_id"]) == business_evidence(linked_id)
            await session.execute(asset_binding.insert(), batch[asset_binding])
            await session.commit()
            loaded = await load_result_asset_rows(session, 17)
            assert business_evidence(decode_assets(loaded)) == business_evidence(after)
            statements = []
            sa.event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                lambda _, _cursor, statement, *_args: statements.append(statement),
            )
            await validate_result_asset_ownership(session, 17)
            assert not any("field_metadata" in statement or "payload_value" in statement for statement in statements)
            assert business_evidence(await read_result_asset_totals(session, [17])) == business_evidence({17: 0})
            assert business_evidence(
                await session.scalar(sa.select(sa.func.count()).select_from(asset_snapshot))
            ) == business_evidence(2)
            with pytest.raises(ValueError, match="不一致"):
                await find_matching_after_asset(session, "pre-order-input", before)
            changed_representation = deepcopy(after)
            changed_representation["account_assets"]["total_asset"] = 0.0
            assert await find_matching_after_asset(session, "pre-order-input", changed_representation) == linked_id
        await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["other_account", "other_execution", "before"])
def test_result_binding_and_scalar_read_reject_foreign_observation(tmp_path, mode):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'foreign.db'}")
        metadata = _metadata()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                metadata.tables["execution_input"].insert(),
                [
                    {"id": "local", "account_id": 1, "execution_id": "exec-local"},
                    {
                        "id": "foreign",
                        "account_id": 2 if mode == "other_account" else 1,
                        "execution_id": "exec-foreign",
                    },
                ],
            )
            await connection.execute(
                metadata.tables["executerecord"].insert(),
                {"id": 17, "account_id": 1, "execution_id": "exec-local", "input_id": "local"},
            )
        sessions = async_sessionmaker(engine)
        payload = {"account_assets": {"total_asset": 42}}
        batch = (
            encode_input_assets("local", payload) if mode == "before" else encode_input_after_assets("foreign", payload)
        )
        actual_id = batch[asset_snapshot][0]["asset_id"]
        async with sessions() as session:
            for table, rows in batch.items():
                if rows:
                    await session.execute(table.insert(), rows)
            bindings = encode_assets(17, payload, root_asset_id=actual_id)[asset_binding]
            await session.execute(asset_binding.insert(), bindings)
            await session.commit()
            with pytest.raises(ValueError):
                await load_result_asset_rows(session, 17)
            with pytest.raises(ValueError):
                await read_result_asset_totals(session, [17])
        await engine.dispose()

    asyncio.run(check())


def test_account_snapshot_reference_rejects_another_manual_snapshot(tmp_path):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'foreign-manual.db'}")
        metadata = _metadata()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(metadata.tables["account_asset_snapshot"].insert(), [{"id": 1}, {"id": 2}])
        sessions = async_sessionmaker(engine)
        async with sessions() as session:
            for table, rows in encode_snapshot_assets(2, {"account_assets": {"total_asset": 99}}).items():
                if rows:
                    await session.execute(table.insert(), rows)
            await session.commit()
            aliased = SimpleNamespace(id=1, asset_fact_id=snapshot_asset_fact_id(2))
            with pytest.raises(ValueError, match="其他快照"):
                await read_snapshots_assets(session, [aliased])
            with pytest.raises(ValueError, match="其他快照"):
                await read_snapshots_assets(session, [aliased], include_positions=False)
        await engine.dispose()

    asyncio.run(check())


def test_after_snapshot_matching_preserves_nonfinite_diagnostic_and_negative_zero(tmp_path):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nonfinite.db'}")
        metadata = _metadata()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(metadata.tables["execution_input"].insert(), {"id": "input-nonfinite"})
        sessions = async_sessionmaker(engine)
        after = {"account_assets": {"total_asset": 0, "extra": {"diagnostic": float("nan"), "native": -0.0}}}
        async with sessions() as session:
            for table, rows in encode_input_after_assets("input-nonfinite", after).items():
                if rows:
                    await session.execute(table.insert(), rows)
            await session.commit()
            assert await find_matching_after_asset(session, "input-nonfinite", deepcopy(after))
            changed = deepcopy(after)
            changed["account_assets"]["extra"]["native"] = 0.0
            assert await find_matching_after_asset(session, "input-nonfinite", changed) == input_after_asset_fact_id(
                "input-nonfinite"
            )
            changed["account_assets"]["extra"]["native"] = 1
            with pytest.raises(ValueError, match="不一致"):
                await find_matching_after_asset(session, "input-nonfinite", changed)
        await engine.dispose()

    asyncio.run(check())


def test_manual_snapshot_batch_reader_and_scalar_query_are_selective(tmp_path):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'manual.db'}")
        metadata = _metadata()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(metadata.tables["account_asset_snapshot"].insert(), [{"id": 5}, {"id": 6}])
        sessions = async_sessionmaker(engine)
        snapshots = [SimpleNamespace(id=key, asset_fact_id=snapshot_asset_fact_id(key)) for key in (5, 6)]
        payloads = {key: {"total_asset": key, "positions": [{"symbol": "A", "volume": key}]} for key in (5, 6)}
        async with sessions() as session:
            for key, payload in payloads.items():
                for table, rows in encode_snapshot_assets(key, {"account_assets": payload}).items():
                    if rows:
                        await session.execute(table.insert(), rows)
            await session.commit()
            statements = []
            sa.event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                lambda _, _cursor, statement, *_args: statements.append(statement),
            )
            scalar = await read_snapshots_assets(session, snapshots, include_positions=False)
            assert business_evidence(scalar) == business_evidence({5: {"total_asset": 5}, 6: {"total_asset": 6}})
            assert business_evidence(len(statements)) == business_evidence(1)
            statements.clear()
            assert business_evidence(await read_snapshots_assets(session, snapshots)) == business_evidence(payloads)
            assert business_evidence(len(statements)) == business_evidence(2)
            snapshots[0].asset_fact_id = asset_fact_id(999)
            with pytest.raises(ValueError, match="缺失"):
                await read_snapshots_assets(session, snapshots)
        await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("input_owned", [False, True])
@pytest.mark.parametrize("execution_id", [None, "execution-history-17"])
@pytest.mark.parametrize("source", ["real", "unavailable"])
def test_execution_asset_history_reuses_valid_canonical_root(tmp_path, input_owned, execution_id, source):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
        metadata = _metadata()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                metadata.tables["execution_input"].insert(),
                {
                    "id": "history-input",
                    "account_id": 1,
                    "execution_id": execution_id,
                },
            )
            await connection.execute(
                metadata.tables["executerecord"].insert(),
                {
                    "id": 17,
                    "account_id": 1,
                    "execution_id": execution_id,
                    "input_id": "history-input",
                },
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        payload = {
            "account_assets": {
                "available_cash": 0,
                "total_asset": 0,
                "market_value": None,
                "source": source,
                "positions": [],
            }
        }
        record = SimpleNamespace(
            id=17, account_id=1, execution_id=execution_id, created_at="2026-10-20T01:02:03.123456+08:00"
        )
        async with sessions() as session:
            if input_owned:
                for table, rows in encode_input_after_assets("history-input", payload).items():
                    if rows:
                        await session.execute(table.insert(), rows)
            reference = input_after_asset_fact_id("history-input") if input_owned else asset_fact_id(17)
            for table, rows in encode_assets(17, payload, root_asset_id=reference if input_owned else None).items():
                if rows:
                    await session.execute(table.insert(), rows)
            first = await ensure_execution_asset_snapshot(session, record)
            second = await ensure_execution_asset_snapshot(session, record)
            if source == "unavailable":
                assert first is second is None
                expected = 0
            else:
                assert first is second
                assert business_evidence(first.asset_fact_id) == business_evidence(reference)
                assert business_evidence(first.source) == business_evidence("execution")
                assert business_evidence(first.execution_id) == business_evidence(execution_id)
                assert business_evidence(first.created_at) == business_evidence(record.created_at)
                assert business_evidence(await read_snapshots_assets(session, [first])) == business_evidence(
                    {first.id: payload["account_assets"]}
                )
                expected = 1
            assert business_evidence(
                await session.scalar(sa.select(sa.func.count()).select_from(metadata.tables["account_asset_snapshot"]))
            ) == business_evidence(expected)
            assert business_evidence(
                await session.scalar(sa.select(sa.func.count()).select_from(asset_snapshot))
            ) == business_evidence(1)
            await session.rollback()
        async with sessions() as session:
            assert business_evidence(
                await session.scalar(sa.select(sa.func.count()).select_from(metadata.tables["account_asset_snapshot"]))
            ) == business_evidence(0)
        await engine.dispose()

    asyncio.run(check())
