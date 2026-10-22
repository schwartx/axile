"""Dashboard native inputs preserve business facts, ordering and account isolation."""

import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server._native import _core
from axile.server.business_facts.assets import read_snapshots_assets, write_snapshot_assets
from axile.server.db.models import AccountAssetSnapshot, PortfolioAccount, TargetWeightSnapshot
from axile.server.db.models.target_weight_snapshot import target_weight_snapshot_weight
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from axile.server.target_weight_snapshots import encode_snapshot_weights
from tests.unit.server._business_analysis_support import analysis_record, persist_records
from tests.unit.server._execution_test_support import build_account


@pytest.fixture
def dashboard_database(tmp_path):
    path = tmp_path / "dashboard-inputs.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    expected = {}

    async def seed():
        writer = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with async_sessionmaker(writer, expire_on_commit=False)() as session:
                session.add_all([build_account(id=i) for i in (1, 2, 3)])
                session.add_all(
                    [
                        PortfolioAccount(id=1, account_id=1, portfolio_id=7),
                        PortfolioAccount(id=2, account_id=2, portfolio_id=7),
                        PortfolioAccount(id=3, account_id=2, portfolio_id=None),
                    ]
                )
                await session.flush()
                snapshots = []
                for snapshot_id, account_id, assets in [
                    (1, 1, {"total_asset": 100, "positions": []}),
                    (
                        2,
                        1,
                        {
                            "total_asset": 0,
                            "available_cash": None,
                            "positions": [
                                {"symbol": "Z", "volume": 1, "market_value": "bad"},
                                {"symbol": "A", "volume": None, "market_value": -5},
                            ],
                        },
                    ),
                    (3, 1, {"total_asset": "invalid", "positions": []}),
                    (4, 2, {"total_asset": 22, "positions": []}),
                ]:
                    snapshot = AccountAssetSnapshot(
                        id=snapshot_id,
                        account_id=account_id,
                        source="manual",
                        created_at=f"2026-10-{20 - snapshot_id:02d}T00:00:00",
                    )
                    session.add(snapshot)
                    await session.flush()
                    await write_snapshot_assets(session, snapshot, assets)
                    if snapshot_id in (2, 4):
                        snapshots.append(snapshot)
                expected.update(await read_snapshots_assets(session, snapshots))
                for snapshot_id, portfolio_id, payload in [
                    (1, 7, {"normalized_weights": {"old": 1}}),
                    (2, 7, {"normalized_weights": {}}),
                    (3, 7, {"normalized_weights": None}),
                    (4, 7, {}),
                    (5, 8, {"normalized_weights": {"wrong": 1}}),
                ]:
                    columns, rows = encode_snapshot_weights(snapshot_id, payload)
                    session.add(
                        TargetWeightSnapshot(
                            id=snapshot_id, account_id=1, portfolio_id=portfolio_id, source="manual", **columns
                        )
                    )
                    await session.flush()
                    if rows[target_weight_snapshot_weight]:
                        await session.execute(
                            target_weight_snapshot_weight.insert(), rows[target_weight_snapshot_weight]
                        )
                await persist_records(
                    session,
                    [
                        analysis_record(
                            id=i,
                            account_id=1,
                            execution_id=f"e{i}",
                            input={},
                            result={"status": "SUCCEEDED"},
                            is_success=1,
                            created_at=f"2026-10-{20 - i:02d}T00:00:00",
                        )
                        for i in (1, 2)
                    ],
                )
                await session.commit()
        finally:
            await writer.dispose()

    asyncio.run(seed())
    try:
        yield path, engine, expected
    finally:
        engine.dispose()


def inputs(path, ids=(1, 2, 3)):
    return _core.dashboard_inputs(_core.Store(str(path)), list(ids), LOGIC_VERSION, ENGINE_VERSION)


def test_dashboard_inputs_selection_and_asset_decoder_parity(dashboard_database):
    path, _, expected = dashboard_database
    assert inputs(path, ()) == {}
    result = inputs(path)
    assert set(result) == {"1", "2", "3"}
    assert result["1"]["observation"]["assets"] == expected[2]
    assert result["2"]["observation"]["assets"] == expected[4]
    assert result["1"]["latest"]["created_at"] == "2026-10-18T00:00:00"
    assert result["1"]["target_weights"] == {}
    assert result["1"]["portfolio_id"] == 7
    assert result["2"]["portfolio_id"] is None
    assert result["2"]["target_weights"] is None
    assert result["3"]["observation"] is None
    assert set(inputs(path, (2,))) == {"2"}


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE account_asset_snapshot SET asset_fact_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE id=4) WHERE id=2",
        "DELETE FROM business_asset_position WHERE asset_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE id=2) AND ordinal=0",
        "UPDATE business_asset_position SET ordinal=5 WHERE asset_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE id=2) AND ordinal=0",
    ],
)
def test_dashboard_rejects_corrupt_asset_facts(dashboard_database, mutation):
    path, engine, _ = dashboard_database
    with engine.begin() as connection:
        connection.execute(text(mutation))
    with pytest.raises(_core.NativeError, match="INVALID_DATA"):
        inputs(path)


@pytest.mark.parametrize("positions", [None, [], "invalid", [None, 7, {"volume": "bad", "market_value": None}]])
def test_dashboard_position_container_parity(dashboard_database, positions):
    path, _, _ = dashboard_database

    async def write():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                snapshot = AccountAssetSnapshot(account_id=1, source="manual")
                await write_snapshot_assets(session, snapshot, {"total_asset": 0, "positions": positions})
                expected = (await read_snapshots_assets(session, [snapshot]))[snapshot.id]
                await session.commit()
                return expected
        finally:
            await engine.dispose()

    expected = asyncio.run(write())
    assert inputs(path)["1"]["observation"]["assets"] == expected


@pytest.mark.parametrize("value", [[], "invalid", 0, {"X": "bad"}])
def test_dashboard_invalid_target_recovery(dashboard_database, value):
    path, engine, _ = dashboard_database
    columns, rows = encode_snapshot_weights(6, {"normalized_weights": value})
    from sqlmodel import Session

    with Session(engine) as session:
        session.add(TargetWeightSnapshot(id=6, account_id=1, portfolio_id=7, source="manual", **columns))
        session.flush()
        if rows[target_weight_snapshot_weight]:
            session.execute(target_weight_snapshot_weight.insert(), rows[target_weight_snapshot_weight])
        session.commit()
    assert inputs(path)["1"]["target_weights"] == ({"X": None} if isinstance(value, dict) else value)
