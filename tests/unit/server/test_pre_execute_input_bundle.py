"""真实 SQLite 中执行前输入、审计变体与观测的保形和原子提交。"""

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts import assets, inputs, repository
from axile.server.execution import input_persistence
from scripts.business_facts_contract import business_evidence


async def _database(monkeypatch, tmp_path):
    path = tmp_path / "bundle.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # 原生冻结入口按数据库路径开独立写连接,必须指向同一个测试文件。
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    return engine, sessions


def test_pre_execute_bundle_preserves_primary_and_independent_audit_variant(monkeypatch, tmp_path):
    async def check():
        engine, sessions = await _database(monkeypatch, tmp_path)
        primary = {"curr_target": {"A": 0}, "account_config": {"password": "synthetic"}}
        audit = {
            "curr_target": {"A": 0.0},
            "last_target": {},
            "strategy_target": {},
            "account_target": {"A": 0.0},
            "sizing_context": {},
            "trade_rules": {},
            "futures_close_policy": {},
            "execution_notification_code": "synthetic",
        }
        before = {"total_asset": 0.0, "positions": [], "source": "real"}
        python_statements: list[str] = []

        def track_python_sql(_conn, _cursor, statement, _params, _context, _executemany):
            python_statements.append(statement)

        try:
            sa.event.listen(engine.sync_engine, "before_cursor_execute", track_python_sql)
            primary_id, audit_id = await input_persistence._persist_pre_execute_bundle(
                account_id=1,
                execution_id="bundle",
                payload=primary,
                audit_payload=audit,
                before_account_assets=before,
            )
            sa.event.remove(engine.sync_engine, "before_cursor_execute", track_python_sql)
            # 冻结全权在原生事务内完成:异步引擎上不应再出现任何 Python SQL。
            assert python_statements == []
            assert primary_id != audit_id
            assert await input_persistence._persist_pre_execute_bundle(
                account_id=1,
                execution_id="bundle",
                payload=primary,
                audit_payload=audit,
                before_account_assets=before,
            ) == (primary_id, audit_id)
            assert audit_id == str(uuid.uuid5(uuid.NAMESPACE_URL, "axile.input-evidence:1:bundle:pre_execute_audit"))
            async with sessions() as session:
                assert await inputs.read_input(session, primary_id) == {"curr_target": {"A": 0}}
                restored = await inputs.read_input(session, audit_id)
                assert business_evidence(restored) == business_evidence(
                    {key: value for key, value in audit.items() if key != "execution_notification_code"}
                )
                assert restored["curr_target"]["A"] == 0
                assert (await inputs.read_input(session, primary_id))["curr_target"]["A"] == 0
                variants = (await session.execute(sa.select(inputs.execution_input))).mappings().all()
                assert len(variants) == 2
                owner = next(row for row in variants if row["id"] == audit_id)
                assert owner["execution_id"] is None
                assert owner["owner_execution_id"] == "bundle"
                snapshots = (await session.execute(sa.select(assets.asset_snapshot))).mappings().all()
                assert len(snapshots) == 1
                assert snapshots[0]["input_id"] == primary_id
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_variant_replay_with_different_content_is_rejected(monkeypatch, tmp_path):
    """同一审计变体身份重放时,内容必须与首次冻结一致。"""

    async def check():
        engine, sessions = await _database(monkeypatch, tmp_path)
        payload = {"curr_target": {"A": 0}}
        try:
            await input_persistence._persist_pre_execute_bundle(
                account_id=1,
                execution_id="bundle-replay",
                payload=payload,
                audit_payload={"curr_target": {"A": 1}},
            )
            with pytest.raises(ValueError, match="Execution input variant replay differs from frozen input"):
                await input_persistence._persist_pre_execute_bundle(
                    account_id=1,
                    execution_id="bundle-replay",
                    payload=payload,
                    audit_payload={"curr_target": {"A": 2}},
                )
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_variant_identity_conflict_rolls_back_primary_and_before_assets(monkeypatch, tmp_path):
    """审计变体身份被他人占用时,同事务内已写入的主输入与观测一并回滚。"""

    async def check():
        engine, sessions = await _database(monkeypatch, tmp_path)
        execution_id = "bundle-failed"
        variant_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"axile.input-evidence:1:{execution_id}:pre_execute_audit"))
        try:
            async with sessions() as session:
                # 预置同标识但属主不同的变体,迫使原生事务在变体步骤失败。
                batch = inputs.encode_inputs(
                    variant_id,
                    {"algorithm": "x"},
                    account_id=1,
                    execution_id=None,
                    created_at="t",
                    owner_execution_id="other-execution",
                )
                await repository._write_batch(session, batch)
                await session.commit()
            with pytest.raises(ValueError, match="Execution input variant identity has a different owner"):
                await input_persistence._persist_pre_execute_bundle(
                    account_id=1,
                    execution_id=execution_id,
                    payload={"curr_target": {}},
                    audit_payload={"account_target": {"A": 0.3}},
                    before_account_assets={"total_asset": 0.0, "positions": []},
                )
            async with sessions() as session:
                assert (
                    await session.scalar(sa.select(sa.func.count()).select_from(inputs.execution_input)) == 1
                )  # 只剩预置冲突行
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_snapshot)) == 0
        finally:
            await engine.dispose()

    asyncio.run(check())
