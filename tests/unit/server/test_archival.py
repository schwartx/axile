"""归档保留历史，执行准入和绑定约束由真实 SQLite 事务保证。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import account_connection, account_crud, account_execution, portfolio
from axile.server.core import db
from axile.server.db.models import Portfolio, ValidateCustomCalcRequest
from tests.conftest import native_store_call
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._runtime_db_support import runtime_rows

NOW = "2026-11-09T01:00:00Z"
CODE = "def calculate_portfolio(context):\n    return {'rb2610': 1.0}\n"


def call(path, operation, **request):
    return native_store_call(path, operation, request)


def create_portfolio(path):
    return call(
        path,
        "portfolio_operation",
        op="create",
        portfolio=Portfolio(name="组合", market="期货", custom_calc_py_code=CODE).model_dump(),
    )


def create_account(path, portfolio_id=None, *, started=False):
    return call(
        path,
        "account_create",
        account=build_account(portfolio_id=portfolio_id, is_started=started).model_dump(mode="json"),
        now=NOW,
    )["account"]


def archive_account(path, account_id, archived=True):
    return call(path, "account_archive", account_id=account_id, archived=archived, now=NOW)


def intent(path, account_id):
    return call(
        path,
        "intent_insert",
        execution_id="queued",
        account_id=account_id,
        kind="rebalance",
        trigger_source="manual",
        channel="ctp",
        algorithm="SINGLE-MAKER",
        payload={},
        created_at=NOW,
    )


def test_archive_retains_configuration_bindings_and_snapshots(native_database):
    path = native_database
    pf = create_portfolio(path)
    account = create_account(path, pf["id"])
    call(
        path,
        "target_operation",
        op="append",
        portfolio_id=pf["id"],
        account_id=account["id"],
        raw_weights={"rb2610": 1.0},
        normalized_weights=None,
        target_mode="weight",
        source="manual",
        execution_id=None,
        calculated_at=NOW,
    )
    archived = archive_account(path, account["id"])
    assert archived["archived_at"] is not None
    assert archived["account_config"] == account["account_config"]
    assert archived["portfolio_id"] == pf["id"]
    assert archive_account(path, account["id"])["archived_at"] == archived["archived_at"]
    assert len(runtime_rows(path, "SELECT * FROM portfolioaccount")) == 1
    assert len(runtime_rows(path, "SELECT * FROM target_weight_snapshot")) == 1
    assert call(path, "account_dashboard_context")["accounts"] == []
    assert call(path, "connection_accounts", live_sessions={}) == []
    assert len(call(path, "account_dashboard_context", archived=True)["accounts"]) == 1
    restored = archive_account(path, account["id"], False)
    assert restored["archived_at"] is None
    assert restored["is_started"] is False


@pytest.mark.parametrize("state", ["started", "QUEUED", "RUNNING", "TERMINATING", "supplement"])
def test_busy_accounts_cannot_be_archived(native_database, state):
    path = native_database
    account = create_account(path, started=state == "started")
    if state in {"QUEUED", "RUNNING", "TERMINATING"}:
        intent(path, account["id"])
        runtime_rows(path, "UPDATE execution_intent SET status=?", (state,))
    elif state == "supplement":
        runtime_rows(
            path,
            "INSERT INTO supplement_group(id,account_id,fingerprint,base_scheduled_at_us,expires_at_us,configured_count,effective_count,status) VALUES('pending',?,'test',0,1,1,1,'active')",
            (account["id"],),
        )
    with pytest.raises(_core.NativeError, match="请先暂停"):
        archive_account(path, account["id"])
    assert call(path, "account_get", account_id=account["id"])["archived_at"] is None


def test_archived_accounts_reject_mutations_and_new_intents(native_database):
    account = create_account(native_database)
    archive_account(native_database, account["id"])
    with pytest.raises(_core.NativeError, match="账户已归档"):
        intent(native_database, account["id"])
    with pytest.raises(_core.NativeError, match="账户已归档"):
        call(native_database, "account_update", account_id=account["id"], patch={"is_started": True}, now=NOW)


def test_portfolio_binding_and_restore_rules(native_database):
    path = native_database
    pf = create_portfolio(path)
    account = create_account(path, pf["id"])
    with pytest.raises(_core.NativeError, match="未归档账户"):
        call(path, "portfolio_operation", op="archive", portfolio_id=pf["id"], updated_at=NOW)
    archive_account(path, account["id"])
    call(path, "portfolio_operation", op="archive", portfolio_id=pf["id"], updated_at=NOW)
    assert json.loads(call(path, "portfolio_query_json", op="list")) == {"data": []}
    assert len(json.loads(call(path, "portfolio_query_json", op="list", archived=True))["data"]) == 1
    assert call(path, "portfolio_query_json", op="get", portfolio_id=pf["id"])
    for op in ("context", "update"):
        with pytest.raises(_core.NativeError, match="组合已归档"):
            call(path, "portfolio_operation", op=op, portfolio_id=pf["id"], changes={"name": "修改"}, updated_at=NOW)
    with pytest.raises(_core.NativeError, match="组合已归档"):
        create_account(path, pf["id"])
    restored = archive_account(path, account["id"], False)
    assert restored["is_started"] is False
    with pytest.raises(_core.NativeError, match="组合已归档"):
        call(path, "account_update", account_id=account["id"], patch={"is_started": True}, now=NOW)
    call(path, "portfolio_operation", op="restore", portfolio_id=pf["id"], updated_at=NOW)
    assert (
        call(path, "account_update", account_id=account["id"], patch={"is_started": True}, now=NOW)["account"][
            "is_started"
        ]
        is True
    )


@pytest.mark.parametrize("race", ["execution", "binding"])
def test_archive_is_atomic_against_admission(native_database, race):
    path = native_database
    account = create_account(path)
    pf = create_portfolio(path)
    gate = Barrier(2)

    def run(action):
        gate.wait(timeout=5)
        try:
            action()
            return True
        except _core.NativeError:
            return False

    archive = (
        (lambda: archive_account(path, account["id"]))
        if race == "execution"
        else (lambda: call(path, "portfolio_operation", op="archive", portfolio_id=pf["id"], updated_at=NOW))
    )
    admission = (
        (lambda: intent(path, account["id"]))
        if race == "execution"
        else (lambda: call(path, "account_update", account_id=account["id"], patch={"portfolio_id": pf["id"]}, now=NOW))
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, archive)
        second = pool.submit(run, admission)
        assert sorted([first.result(timeout=5), second.result(timeout=5)]) == [False, True]


def test_0035_upgrade_preserves_data_and_archives_block_downgrade(tmp_path):
    path = tmp_path / "old.db"
    call(path, "migration_upgrade", public="0035")
    runtime_rows(
        path,
        "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at_us,updated_at_us) VALUES(7,'旧组合','期货',?,0,0)",
        (CODE,),
    )
    assert call(path, "migration_upgrade")["public"] == "0037"
    assert runtime_rows(path, "SELECT name,archived_at_us FROM portfolio")[0][:] == ("旧组合", None)
    call(path, "portfolio_operation", op="archive", portfolio_id=7, updated_at=NOW)
    with pytest.raises(_core.NativeError, match="无法无损降级"):
        call(path, "migration_downgrade", public="0035")
    assert call(path, "migration_current")["public"] == "0037"
    call(path, "portfolio_operation", op="restore", portfolio_id=7, updated_at=NOW)
    assert call(path, "migration_downgrade", public="0035")["public"] == "0035"


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_archive_http_blocks_execution_and_connection_without_touching_workers(
    native_database, monkeypatch, cleanup_fails
):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    account = create_account(native_database)
    manager = Mock(configure_catalog_account=Mock(), drop_account=AsyncMock(), prepare_account=AsyncMock())
    if cleanup_fails:
        manager.drop_account.side_effect = [RuntimeError("模拟回收失败"), None, None]
    monkeypatch.setattr("axile.server.execution.worker_backend.manager.get_worker_backend_manager", lambda: manager)
    app = FastAPI()
    for router in (account_crud.router, account_execution.router, account_connection.router):
        app.include_router(router, prefix="/account")
    app.include_router(portfolio.router)
    with TestClient(app) as client:
        response = client.post(f"/account/{account['id']}/archive")
        if cleanup_fails:
            assert response.status_code == 202, response.text
            assert response.json()["runtime_sync"]["status"] == "failed"
            assert call(native_database, "account_get", account_id=account["id"])["archived_at"] is not None
            assert client.post(f"/account/execute/{account['id']}").status_code == 409
            response = client.post(f"/account/{account['id']}/archive")
        assert response.status_code == 200, response.text
        assert response.json()["runtime_sync"]["status"] == "synchronized"
        for url in (f"/account/execute/{account['id']}", f"/account/{account['id']}/connection/reconnect"):
            response = client.post(url)
            assert response.status_code == 409, response.text
        assert client.get(f"/account/{account['id']}").status_code == 200
        response = client.post(f"/account/{account['id']}/restore")
        assert response.status_code == 200, response.text
        assert response.json()["is_started"] is False
    manager.prepare_account.assert_not_called()


@pytest.mark.parametrize("force_reconnect", [False, True])
def test_stale_worker_requests_cannot_reconnect_archived_accounts(native_database, monkeypatch, force_reconnect):
    from axile.server.execution.resident_connection import AccountConnectionUnavailable
    from axile.server.execution.worker_backend.manager import WorkerBackendManager
    from axile.server.execution.worker_backend.protocol import WorkerBackendRequest

    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    account = create_account(native_database)
    archive_account(native_database, account["id"])
    request = WorkerBackendRequest(
        request_id="stale",
        command="prepare",
        account_payload=account,
        execution_id=None,
        payload={"force_reconnect": force_reconnect},
    )
    manager = object.__new__(WorkerBackendManager)
    with pytest.raises(AccountConnectionUnavailable, match="账户已归档"):
        manager._check_worker_creation(request)


def test_startup_recovery_keeps_archived_accounts_disconnected(native_database, monkeypatch):
    from axile.server.execution.account_runtime_sync import recover_account_runtime_on_startup

    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    account = create_account(native_database)
    archive_account(native_database, account["id"])
    manager = Mock(configure_catalog_account=Mock(), drop_account=AsyncMock(), prepare_account=AsyncMock())
    monkeypatch.setattr("axile.server.execution.worker_backend.manager.get_worker_backend_manager", lambda: manager)
    asyncio.run(recover_account_runtime_on_startup(Mock()))
    manager.drop_account.assert_awaited_once_with(account["id"])
    manager.prepare_account.assert_not_called()


def test_account_script_trial_blocks_archival_until_finished(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    account = create_account(native_database)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def trial(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {
                "valid": True,
                "target": {},
                "target_mode": "weight",
                "error": None,
                "traceback": None,
                "error_line": None,
                "error_offset": None,
                "error_type": None,
                "error_message": None,
            }

        monkeypatch.setattr(portfolio, "_run_custom_calc_validation", trial)
        task = asyncio.create_task(
            portfolio.validate_custom_calc(
                ValidateCustomCalcRequest(account_id=account["id"], custom_calc_py_code=CODE)
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            with pytest.raises(HTTPException) as error:
                await account_crud.archive_account(Mock(), account["id"], Response())
            assert error.value.status_code == 409
        finally:
            release.set()
            await task

    asyncio.run(scenario())
    assert archive_account(native_database, account["id"])["archived_at"] is not None
