"""真实 SQLite 中执行前输入、审计变体与观测的保形和原子提交。"""

import asyncio
import uuid

import pytest

from axile.server._native import _core
from axile.server.business_facts import repository
from scripts.business_facts_contract import business_evidence
from tests.unit.server._business_facts_test_support import freeze_input, rows, seed_account, sql


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    seed_account(native_database)
    return native_database


def test_pre_execute_bundle_preserves_primary_and_independent_audit_variant(database):
    async def check():
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
        audit_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "axile.input-evidence:1:bundle:pre_execute_audit"))
        receipt = freeze_input(
            database,
            primary,
            execution_id="bundle",
            audit_payload=audit,
            audit_input_id=audit_id,
            before_assets=before,
        )
        primary_id = receipt["input_id"]
        assert receipt["audit_id"] == audit_id
        assert primary_id != audit_id
        assert (
            freeze_input(
                database,
                primary,
                execution_id="bundle",
                audit_payload=audit,
                audit_input_id=audit_id,
                before_assets=before,
            )
            == receipt
        )
        assert await repository.read_execution_input(primary_id) == {"curr_target": {"A": 0}}
        restored = await repository.read_execution_input(audit_id)
        assert business_evidence(restored) == business_evidence(
            {key: value for key, value in audit.items() if key != "execution_notification_code"}
        )
        assert restored["curr_target"]["A"] == 0
        assert (await repository.read_execution_input(primary_id))["curr_target"]["A"] == 0
        variants = rows(database, "execution_input")
        assert len(variants) == 2
        owner = next(row for row in variants if row["id"] == audit_id)
        assert owner["execution_id"] is None
        assert owner["owner_execution_id"] == "bundle"
        snapshots = rows(database, "business_asset_snapshot")
        assert len(snapshots) == 1
        assert snapshots[0]["input_id"] == primary_id

    asyncio.run(check())


def test_variant_replay_with_different_content_is_rejected(database):
    """同一审计变体身份重放时，内容必须与首次冻结一致。"""

    async def check():
        payload = {"curr_target": {"A": 0}}
        freeze_input(
            database,
            payload,
            execution_id="bundle-replay",
            audit_payload={"curr_target": {"A": 1}},
            audit_input_id="audit-replay",
        )
        with pytest.raises(_core.NativeError, match="Execution input variant replay differs from frozen input"):
            freeze_input(
                database,
                payload,
                execution_id="bundle-replay",
                audit_payload={"curr_target": {"A": 2}},
                audit_input_id="audit-replay",
            )

    asyncio.run(check())


def test_variant_identity_conflict_rolls_back_primary_and_before_assets(database):
    """审计变体身份被他人占用时，同事务内已写入的主输入与观测一并回滚。"""

    async def check():
        execution_id = "bundle-failed"
        variant_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"axile.input-evidence:1:{execution_id}:pre_execute_audit"))
        freeze_input(database, {"algorithm": "x"}, input_id=variant_id, execution_id="other-execution")
        sql(database, "UPDATE execution_input SET execution_id=NULL WHERE id=?", (variant_id,))
        with pytest.raises(_core.NativeError, match="Execution input variant identity has a different owner"):
            freeze_input(
                database,
                {"curr_target": {}},
                execution_id=execution_id,
                audit_payload={"account_target": {"A": 0.3}},
                audit_input_id=variant_id,
                before_assets={"total_asset": 0.0, "positions": []},
            )
        assert [row["id"] for row in rows(database, "execution_input")] == [variant_id]
        assert rows(database, "business_asset_snapshot") == []

    asyncio.run(check())
