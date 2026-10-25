"""真实路由调用链的事务释放、账户删除与盘前准备交错测试。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Response

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server import account_assets as assets_service
from axile.server.api.routes import account_assets, account_crud, account_execution, account_feishu, portfolio
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models import Account, AccountUpdate
from axile.server.db.models.account import AccountSnapshot
from axile.server.db.models.portfolio import ValidateCustomCalcRequest
from axile.server.execution import account_runtime_sync as runtime
from axile.server.execution import ctp_channels
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.worker_backend import manager as worker_manager
from axile.server.native_store import native_operation
from tests.unit.server._runtime_db_support import runtime_database, runtime_rows


def _worker_can_write_and_checkpoint(path: Path) -> None:
    """写事务会阻挡提交，WAL 中泄漏的读事务会阻挡 checkpoint。"""
    runtime_rows(path, "UPDATE account SET remark='worker wrote' WHERE id=2")
    assert runtime_rows(path, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0


@pytest.mark.parametrize(
    "entry", ["assets", "feishu", "account_target", "portfolio_target", "validate", "retry", "update"]
)
@pytest.mark.parametrize("wal", [False, True])
def test_routes_release_transactions_before_worker(tmp_path, monkeypatch, entry, wal):
    """worker 以短超时提交并截断 WAL，路由不能遗留读写事务。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as path:
            code = "def calculate_portfolio(context):\n    return {}"
            now = runtime.now_str()
            runtime_rows(
                path,
                "INSERT INTO portfolio(id,name,market,custom_calc_py_code,updated_at,created_at) "
                "VALUES (1,'test','期货',?,?,?)",
                (code, now, now),
            )
            runtime_rows(
                path, "INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES (1,1,?)", (now,)
            )
            called = False

            async def write_from_worker(account):
                nonlocal called
                assert isinstance(account, (Account, AccountSnapshot))
                _worker_can_write_and_checkpoint(path)
                called = True

            class Manager:
                configure_catalog_account = MagicMock()

                async def get_account_assets(self, account):
                    await write_from_worker(account)
                    return UnifiedAccountAssets(available_cash=1000, total_asset=1000, market_value=0, positions=[])

                async def calculate_portfolio(self, account, *_args, **_kwargs):
                    await write_from_worker(account)
                    return {}

                async def prepare_account(self, account):
                    await write_from_worker(account)
                    return {}

            monkeypatch.setattr(assets_service, "get_worker_backend_manager", Manager)
            monkeypatch.setattr(worker_manager, "get_worker_backend_manager", Manager)
            monkeypatch.setattr(ctp_channels, "get_worker_backend_manager", Manager)
            if entry == "assets":
                await account_assets.refresh_account_assets(1)
            elif entry == "feishu":
                account = await _get_account_or_404(1)
                await account_feishu._build_test_card(AccountSnapshot.model_validate(account.model_dump()))
            elif entry == "account_target":
                await account_execution.refresh_account_target_snapshot(1)
            elif entry == "portfolio_target":
                await portfolio.refresh_portfolio_target_snapshot(1)
            elif entry == "validate":
                await portfolio.validate_custom_calc(ValidateCustomCalcRequest(account_id=1, custom_calc_py_code=code))
            elif entry == "retry":
                await account_crud.retry_account_runtime_sync(MagicMock(), 1, Response())
            else:
                await account_crud.update_account(MagicMock(), 1, AccountUpdate(name="new"), Response())
            assert called

    asyncio.run(scenario())


def test_delete_waits_for_reconcile_and_cannot_resurrect_worker(tmp_path, monkeypatch):
    """删除等待对齐，等待期间不占事务；删除后重试不能重建 worker。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def prepare(*_args, **_kwargs):
                calls.append("prepare")
                entered.set()
                await release.wait()

            async def drop(_account_id):
                _worker_can_write_and_checkpoint(path)
                calls.append("drop")

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            monkeypatch.setattr(account_crud, "drop_account_worker", drop)
            running = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock()))
            await asyncio.wait_for(entered.wait(), 5)
            deleting = asyncio.create_task(account_crud.delete_account(MagicMock(), 1))
            await asyncio.sleep(0)
            assert not deleting.done()
            _worker_can_write_and_checkpoint(path)
            release.set()
            await asyncio.wait_for(asyncio.gather(running, deleting), 5)
            assert calls == ["prepare", "drop"]
            with pytest.raises(LookupError):
                await runtime.reconcile_account_runtime(1, MagicMock())
            assert calls == ["prepare", "drop"]

    asyncio.run(scenario())


def test_retry_returns_404_when_account_is_deleted_while_waiting(tmp_path, monkeypatch):
    """检查账户后到实际对齐前被删除，仍遵守接口的不存在语义。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db"):

            async def drop(_account_id):
                return None

            monkeypatch.setattr(account_crud, "drop_account_worker", drop)
            async with account_runtime_lock(1):
                deletion = asyncio.create_task(account_crud.delete_account(MagicMock(), 1))
                await asyncio.sleep(0)
                assert await _get_account_or_404(1) is not None
                retry = asyncio.create_task(account_crud._reconcile_committed_runtime(MagicMock(), 1))
            await asyncio.wait_for(deletion, 5)
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(retry, 5)
            assert error.value.status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["disabled", "deleted", "renamed", "channel_changed"])
def test_session_prepare_reloads_after_waiting_for_runtime_lock(tmp_path, monkeypatch, change):
    """批量候选过期时，以锁内重新读取的配置决定跳过或准备。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            prepared = []

            class Manager:
                configure_catalog_account = MagicMock()

                async def prepare_account(self, account):
                    _worker_can_write_and_checkpoint(path)
                    prepared.append(account.name)
                    return {}

            monkeypatch.setattr(ctp_channels, "get_worker_backend_manager", Manager)
            candidates = await ctp_channels._started_china_channel_accounts()
            assert 1 in candidates
            async with account_runtime_lock(1):
                pending = asyncio.create_task(ctp_channels._prepare_accounts([1], "day"))
                await asyncio.sleep(0)
                if change == "deleted":
                    await native_operation("account_delete", {"account_id": 1})
                elif change == "channel_changed":
                    runtime_rows(path, "UPDATE account SET trade_channel='gm' WHERE id=1")
                elif change == "disabled":
                    runtime_rows(
                        path,
                        "UPDATE account_settings SET schedule=json_set(schedule,'$.is_started',json('false')) WHERE account_id=1",
                    )
                else:
                    runtime_rows(path, "UPDATE account SET name='latest' WHERE id=1")
            await asyncio.wait_for(pending, 5)
            assert prepared == (["latest"] if change == "renamed" else [])
            if change != "renamed":
                assert not runtime_rows(path, "SELECT id FROM accountruntimesyncattempt WHERE account_id=1")

    asyncio.run(scenario())
