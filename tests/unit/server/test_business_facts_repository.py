"""业务事实事务保留下单前输入及统一资产观测引用。"""

import asyncio
import sqlite3
from contextlib import closing

import pytest

from axile.server._native import _core
from axile.server.execution import input_persistence
from scripts.business_facts_contract import business_evidence
from tests.fixtures.rust_store.oracle_tables import table
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server import _native_read_views as repository
from tests.unit.server._business_facts_test_support import freeze_input, persist_result, rows, seed_account, sql
from tests.unit.server._native_read_views import read_view


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    for account_id in (2, 3):
        seed_account(native_database, account_id)
    return native_database


def test_domains_cannot_replace_malformed_symbol_container(native_database):
    record = persist_result(native_database, {"symbol_results": {"A": None}})
    sql(
        native_database,
        "INSERT INTO execution_order_collection(record_id,path,ordinal,map_key,kind,item_count,audit_evidence) "
        "VALUES (?,'/symbol_results/A/orders',0,'A','array',0,'{}')",
        (record["id"],),
    )
    with pytest.raises(_core.NativeError, match="conflicting fact field"):
        read_view(native_database, "read_execution_result", {"account_id": 1, "record_id": record["id"]})


def test_nonempty_result_root_array_is_rejected_before_writing(native_database):
    with pytest.raises(_core.NativeError, match="Unsupported historical result root"):
        persist_result(native_database, [{"orders": []}])
    assert rows(native_database, "executerecord") == []
    assert rows(native_database, "execution_extension") == []


@pytest.mark.parametrize("foreign_after_asset", [False, True])
@pytest.mark.parametrize("attached_input", [False, True])
@pytest.mark.parametrize("result_inputs", [False, True])
def test_final_result_reuses_durable_normalized_input_and_after_asset(
    database, foreign_after_asset, attached_input, result_inputs
):
    async def check():
        frozen = {"curr_target": {"normalized": 0}, "channel_type": "tq"}
        observed = {"total_asset": 0.0, "positions": [], "extra": {}}
        output = {"status": "NOOP", "account_assets": observed, "symbol_results": {}}
        if result_inputs:
            output["inputs"] = frozen
        receipt = freeze_input(
            database,
            frozen,
            account_id=2,
            execution_id="frozen",
            input_id="normalized-input",
            created_at="2026-10-20T09:00:00",
        )
        input_id = receipt["input_id"]
        after_ref = await asyncio.to_thread(
            input_persistence.persist_post_execute_assets_sync, account_id=2, execution_id="frozen", payload=output
        )
        record = persist_result(
            database,
            output,
            raw_input={"curr_target": {"stale": 1}},
            account_id=2,
            execution_id="frozen",
            is_success=1,
            input_id=input_id if attached_input else "unused-input",
        )
        canonical_input = await repository.read_execution_input(record["input_id"])
        assert business_evidence(canonical_input) == business_evidence(frozen)
        detail = await repository.read_execution_detail(record)
        assert list(detail) == ["id", "account_id", "execution_id", "created_at", "is_success", "input", "result"]
        assert business_evidence(detail["input"]) == business_evidence(frozen)
        assert business_evidence(detail["result"]) == business_evidence(output)
        identity = {"record_id": record["id"], "account_id": 2}
        assert business_evidence(read_view(database, "read_execution_result", identity)) == business_evidence(output)
        assert business_evidence(
            read_view(database, "read_execution_input", {"input_id": input_id})
        ) == business_evidence(frozen)
        assert record["input_id"] == input_id
        assert record["result_input_id"] == (input_id if result_inputs else None)
        assert business_evidence(after_ref) == business_evidence((input_id, f"input:{input_id}:/after_account_assets"))
        assert len(rows(database, "execution_input")) == 1
        assert len(rows(database, "business_asset_snapshot")) == 1
        assert rows(database, "business_asset_binding")[0]["asset_id"] == after_ref[1]
        assert rows(database, "business_asset_position") == []
        if foreign_after_asset:
            other_input = freeze_input(database, {}, account_id=2, execution_id="other", input_id="other-input")
            await asyncio.to_thread(
                input_persistence.persist_post_execute_assets_sync,
                account_id=2,
                execution_id="other",
                payload={"account_assets": {"total_asset": 99}},
            )
            sql(
                database,
                "UPDATE business_asset_binding SET asset_id=? WHERE record_id=?",
                (f"input:{other_input['input_id']}:/after_account_assets", record["id"]),
            )
            with pytest.raises(ValueError):
                await repository.read_execution_result(record)
            with pytest.raises(_core.NativeError):
                read_view(database, "read_execution_result", identity)

    asyncio.run(check())


@pytest.mark.parametrize("is_success", [0, 1])
@pytest.mark.parametrize("result_assets", ["absent", "matching", "different"])
def test_input_reuse_preserves_failure_finalization_after_assets(database, is_success, result_assets):
    async def check():
        frozen = {"curr_target": {"A": 0}, "last_target": None}
        observed = {"total_asset": 0, "positions": []}
        output = {"status": "SUCCEEDED" if is_success else "FAILED"}
        if result_assets != "absent":
            output["account_assets"] = observed if result_assets == "matching" else {"total_asset": 1}
        freeze_input(database, frozen, account_id=2, execution_id="after-assets")
        await asyncio.to_thread(
            input_persistence.persist_post_execute_assets_sync,
            account_id=2,
            execution_id="after-assets",
            payload={"account_assets": observed},
        )
        if result_assets == "different" or (is_success and result_assets == "absent"):
            with pytest.raises(_core.NativeError, match="终态结果"):
                persist_result(database, output, account_id=2, execution_id="after-assets", is_success=is_success)
            assert rows(database, "executerecord") == []
            assert len(rows(database, "execution_input")) == 1
            assert len(rows(database, "business_asset_snapshot")) == 1
            assert rows(database, "business_asset_binding") == []
            return
        record = persist_result(database, output, account_id=2, execution_id="after-assets", is_success=is_success)
        canonical_input = await repository.read_execution_input(record["input_id"])
        assert canonical_input == frozen
        result = await repository.read_execution_result(record)
        assert business_evidence(result) == business_evidence(output)
        assert len(rows(database, "business_asset_snapshot")) == 1
        assert len(rows(database, "business_asset_binding")) == (1 if result_assets == "matching" else 0)

    asyncio.run(check())


@pytest.mark.parametrize("reference", ["input_id", "result_input_id"])
@pytest.mark.parametrize("input_account", [2, 3])
def test_foreign_input_is_rejected_on_read_and_write(database, reference, input_account):
    unrelated = freeze_input(database, {}, account_id=input_account, execution_id="other", input_id="unrelated")
    record = persist_result(database, {"inputs": {}}, account_id=2, execution_id="own", raw_input={}, is_success=1)
    if input_account == 3:
        with pytest.raises(sqlite3.IntegrityError, match="Execution input owner mismatch"):
            sql(database, f"UPDATE executerecord SET {reference}=? WHERE id=?", (unrelated["input_id"], record["id"]))
        # 模拟历史损坏需临时移开写保护；读取仍使用完整原生校验。
        with closing(oracle_connect(database)) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='business_facts_0034_record_input_update'"
            ).fetchone()[0]
        sql(database, "DROP TRIGGER business_facts_0034_record_input_update")
    sql(database, f"UPDATE executerecord SET {reference}=? WHERE id=?", (unrelated["input_id"], record["id"]))
    if input_account == 3:
        sql(database, trigger)
    with pytest.raises(ValueError, match="input belongs to another account or execution"):
        asyncio.run(repository.read_execution_detail(record))
    with pytest.raises(_core.NativeError, match="input belongs to another account or execution"):
        read_view(database, "read_execution_result", {"account_id": 2, "record_id": record["id"]})

    # 终态只能首次提交；在新终态中验证发现冻结输入时的相同归属边界。
    sql(database, "UPDATE execution_input SET execution_id='write-attempt' WHERE id=?", (unrelated["input_id"],))
    message = "其他执行" if input_account == 2 else "another account"
    with pytest.raises(_core.NativeError, match=message):
        persist_result(database, {}, account_id=2, execution_id="write-attempt")
    assert len(rows(database, "executerecord")) == 1
    sql(
        database,
        "UPDATE execution_input SET execution_id='other',owner_execution_id='another-execution' WHERE id=?",
        (unrelated["input_id"],),
    )
    with pytest.raises(_core.NativeError, match=message):
        freeze_input(database, {}, account_id=2, execution_id="other", input_id="new-input")


def test_null_execution_ids_cannot_share_one_legacy_input(database):
    first = persist_result(database, {}, account_id=2, raw_input={}, input_id="legacy-input", is_success=1)
    with pytest.raises(_core.NativeError, match="UNIQUE constraint failed: execution_input.id"):
        persist_result(database, {}, account_id=2, raw_input={}, input_id="legacy-input", is_success=1)
    assert len(rows(database, "executerecord")) == 1
    assert len(rows(database, "execution_input")) == 1

    # 人为损坏历史数据后，完整读取同样禁止匿名执行共享输入。
    columns = ",".join(column.name for column in table("executerecord").c if column.name != "id")
    sql(
        database,
        f"INSERT INTO executerecord ({columns}) SELECT {columns} FROM executerecord WHERE id=?",
        (first["id"],),
    )
    for record in rows(database, "executerecord"):
        with pytest.raises(ValueError, match="anonymous execution input is shared"):
            asyncio.run(repository.read_execution_detail(record))
