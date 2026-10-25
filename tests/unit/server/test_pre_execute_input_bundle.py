"""真实 SQLite 中单输入与前观测的原子提交，以及旧双审计协议的拒绝边界。"""

import asyncio

import pytest

from axile.server._native import _core
from scripts.business_facts_contract import business_evidence
from tests.unit.server import _native_read_views as repository
from tests.unit.server._business_facts_test_support import freeze_input, rows, seed_account, sql


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    seed_account(native_database)
    return native_database


def test_pre_execute_bundle_preserves_single_audit_input_and_before_assets(database):
    payload = {
        "curr_target": {"A": 0.0},
        "last_target": {},
        "strategy_target": {},
        "account_target": {"A": 0.0},
        "sizing_context": {},
        "trade_rules": {},
        "futures_close_policy": {},
        "account_config": {"password": "synthetic"},
        "execution_notification_code": "synthetic",
    }
    before = {"total_asset": 0.0, "positions": [], "source": "real"}
    receipt = freeze_input(database, payload, execution_id="bundle", before_assets=before)
    assert receipt == {"input_id": "input-1"}
    assert freeze_input(database, payload, execution_id="bundle", before_assets=before) == receipt
    restored = asyncio.run(repository.read_execution_input(receipt["input_id"]))
    assert business_evidence(restored) == business_evidence(
        {key: value for key, value in payload.items() if key not in {"account_config", "execution_notification_code"}}
    )
    (owner,) = rows(database, "execution_input")
    assert owner["id"] == receipt["input_id"]
    assert owner["execution_id"] == owner["owner_execution_id"] == "bundle"
    (snapshot,) = rows(database, "business_asset_snapshot")
    assert snapshot["input_id"] == receipt["input_id"]
    assert snapshot["total_asset"] == 0.0


@pytest.mark.parametrize("keys", [("audit_payload",), ("audit_input_id",), ("audit_payload", "audit_input_id")])
@pytest.mark.parametrize("value", [None, {}, [], "", False, "retired"])
def test_retired_audit_keys_are_rejected_without_writing_facts(database, keys, value):
    with pytest.raises(_core.NativeError, match="执行前双审计输入写入协议已停用") as caught:
        freeze_input(
            database,
            {"curr_target": {"A": 0}},
            before_assets={"total_asset": 0.0, "positions": []},
            **dict.fromkeys(keys, value),
        )
    assert caught.value.args[0] == "BUSINESS_FACTS"
    for name in (
        "execution_input",
        "execution_input_target",
        "execution_input_symbol",
        "execution_input_plan",
        "business_asset_snapshot",
        "business_asset_position",
    ):
        assert rows(database, name) == []


def test_before_asset_write_failure_rolls_back_primary_input(database):
    sql(
        database,
        "CREATE TRIGGER reject_before_assets BEFORE INSERT ON business_asset_snapshot "
        "BEGIN SELECT RAISE(ABORT,'synthetic before asset failure'); END",
    )
    with pytest.raises(_core.NativeError, match="synthetic before asset failure"):
        freeze_input(
            database,
            {"curr_target": {"A": 0}},
            before_assets={"total_asset": 0.0, "positions": []},
        )
    assert rows(database, "execution_input") == []
    assert rows(database, "execution_input_target") == []
    assert rows(database, "business_asset_snapshot") == []
