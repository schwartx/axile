"""执行记录与账户资产快照原子持久化测试。"""

from __future__ import annotations

import asyncio

import pytest

from axile.server._native import _core
from axile.server.business_facts.assets import latest_asset_snapshot
from axile.server.db.models import ExecuteRecord
from axile.server.execution.records import append_success_execute_record
from tests.unit.server._business_facts_test_support import rows, seed_account, sql
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import read_execution_result


def test_success_record_persists_matching_asset_snapshot(native_database, monkeypatch) -> None:
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    seed_account(native_database, 51)
    account = build_account(id=51)
    result = {
        "account_assets": {
            "available_cash": 700.0,
            "total_asset": 1000.0,
            "market_value": 300.0,
            "positions": [],
        }
    }

    async def check() -> None:
        saved = await append_success_execute_record(account, raw_input={}, result=result, execution_id="exec-assets-51")
        records = rows(native_database, "executerecord")
        assert len(records) == 1
        record = ExecuteRecord.model_validate(records[0])
        assert record.id == saved.id
        snapshots = rows(native_database, "account_asset_snapshot")
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot["account_id"] == 51
        assert snapshot["execution_id"] == "exec-assets-51"
        assert snapshot["source"] == "execution"
        assert snapshot["created_at"] == record.created_at
        bindings = rows(native_database, "business_asset_binding")
        assert len(bindings) == 1
        assert bindings[0]["record_id"] == record.id
        assert bindings[0]["path"] == "/account_assets"
        assert snapshot["asset_fact_id"] == bindings[0]["asset_id"]
        assert len(rows(native_database, "business_asset_snapshot")) == 1
        latest = await latest_asset_snapshot(51)
        assert latest is not None and latest["id"] == snapshot["id"]
        assets = latest["assets"]
        canonical_result = await read_execution_result(record)
        assert assets == canonical_result["account_assets"]
        assert assets == {
            **result["account_assets"],
            "availability": {"cash": "value", "equity": "value", "market_value": "value"},
        }

        # 在最后的历史快照步骤失败，已写终态、输入和资产事实必须全部回滚。
        tables = (
            "executerecord",
            "execution_input",
            "execution_extension",
            "business_asset_binding",
            "business_asset_snapshot",
            "business_asset_position",
            "account_asset_snapshot",
        )
        committed = {name: rows(native_database, name) for name in tables}
        sql(
            native_database,
            "CREATE TRIGGER reject_asset_history BEFORE INSERT ON account_asset_snapshot "
            "BEGIN SELECT RAISE(ABORT,'asset history failed'); END",
        )
        with pytest.raises(_core.NativeError, match="asset history failed"):
            await append_success_execute_record(
                account, raw_input={}, result=result, execution_id="exec-assets-rollback"
            )
        assert {name: rows(native_database, name) for name in tables} == committed

    asyncio.run(check())
