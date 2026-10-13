"""真实路由调用链的事务释放、账户删除与盘前准备交错测试。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import inspect, text

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server import account_assets as assets_service
from axile.server.api.routes import account_assets, account_crud, account_execution, account_feishu, portfolio
from axile.server.db.models import Account, AccountUpdate, Portfolio, PortfolioAccount
from axile.server.db.models.account import AccountSnapshot
from axile.server.db.models.portfolio import ValidateCustomCalcRequest
from axile.server.execution import account_runtime_sync as runtime
from axile.server.execution import ctp_channels
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.worker_backend import manager as worker_manager
from axile.server.portfolio_function import PortfolioFunctionResult
from tests.unit.server._runtime_db_support import runtime_database


@pytest.mark.parametrize(
    "entry", ["assets", "feishu", "account_target", "portfolio_target", "validate", "retry", "update"]
)
@pytest.mark.parametrize("wal", [False, True])
def test_routes_release_transactions_before_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, wal: bool
) -> None:
    """路由自己的读事务也必须释放，worker 才能从另一连接提交。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as (factory, sessions):
            code = "def calculate_portfolio(context):\n    return {}"
            async with factory() as session, session.begin():
                session.add(Portfolio(id=1, name="test", market="期货", custom_calc_py_code=code))
                await session.flush()
                session.add(PortfolioAccount(account_id=1, portfolio_id=1))
            called = False

            async def write_from_worker(account: Account) -> None:
                nonlocal called
                assert isinstance(account, AccountSnapshot) or inspect(account).session is None
                assert all(not session.in_transaction() for session in sessions)
                async with factory() as session, session.begin():
                    await session.execute(text("UPDATE account SET remark='worker wrote' WHERE id=2"))
                called = True

            class Manager:
                configure_catalog_account = MagicMock()

                async def get_account_assets(self, account: Account) -> UnifiedAccountAssets:
                    await write_from_worker(account)
                    return UnifiedAccountAssets(available_cash=1000, total_asset=1000, market_value=0, positions=[])

                async def calculate_portfolio(self, account: Account, *_args: object, **_kwargs: object):
                    await write_from_worker(account)
                    return PortfolioFunctionResult(ok=True, target={})

                async def prepare_account(self, account: Account):
                    await write_from_worker(account)
                    return {}

            monkeypatch.setattr(assets_service, "get_worker_backend_manager", Manager)
            monkeypatch.setattr(worker_manager, "get_worker_backend_manager", Manager)
            monkeypatch.setattr(ctp_channels, "get_worker_backend_manager", Manager)
            monkeypatch.setattr(runtime, "SessionLocal", factory)
            async with factory() as session:
                if entry == "assets":
                    await account_assets.refresh_account_assets(session, 1)
                elif entry == "feishu":
                    account = await session.get(Account, 1)
                    assert account is not None
                    await account_feishu._build_test_card(session, account)
                elif entry == "account_target":
                    await account_execution.refresh_account_target_snapshot(session, 1)
                elif entry == "portfolio_target":
                    await portfolio.refresh_portfolio_target_snapshot(session, 1)
                elif entry == "validate":
                    await portfolio.validate_custom_calc(
                        session, ValidateCustomCalcRequest(account_id=1, custom_calc_py_code=code)
                    )
                elif entry == "retry":
                    await account_crud.retry_account_runtime_sync(session, MagicMock(), 1, Response())
                else:
                    await account_crud.update_account(session, MagicMock(), 1, AccountUpdate(name="new"), Response())
            assert called

    asyncio.run(scenario())


def test_delete_waits_for_reconcile_and_cannot_resurrect_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除等待对齐，但等待期间不占数据库连接；删除后重试不能再准备 worker。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as (factory, sessions):
            entered, release = asyncio.Event(), asyncio.Event()
            calls: list[str] = []

            async def prepare(*_args: object, **_kwargs: object) -> None:
                calls.append("prepare")
                entered.set()
                await release.wait()

            async def drop(_account_id: int) -> None:
                assert all(not session.in_transaction() for session in sessions)
                calls.append("drop")

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            monkeypatch.setattr(account_crud, "drop_account_worker", drop)
            running = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory))
            await asyncio.wait_for(entered.wait(), 5)
            async with factory() as session:
                deleting = asyncio.create_task(account_crud.delete_account(session, MagicMock(), 1))
                # 调度一次删除协程，让它到达锁等待点。
                await asyncio.sleep(0)
                assert not deleting.done() and not session.in_transaction()
                release.set()
                await asyncio.wait_for(asyncio.gather(running, deleting), 5)
            assert calls == ["prepare", "drop"]
            with pytest.raises(LookupError):
                await runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory)
            assert calls == ["prepare", "drop"]

    asyncio.run(scenario())


def test_retry_returns_404_when_account_is_deleted_while_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检查账户后到实际对齐前被删除，也遵守接口的不存在语义。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as (factory, _sessions):
            monkeypatch.setattr(runtime, "SessionLocal", factory)

            async def drop(_account_id: int) -> None:
                return None

            monkeypatch.setattr(account_crud, "drop_account_worker", drop)
            async with factory() as delete_session, factory() as retry_session:
                async with account_runtime_lock(1):
                    deletion = asyncio.create_task(account_crud.delete_account(delete_session, MagicMock(), 1))
                    await asyncio.sleep(0)
                    assert await retry_session.get(Account, 1) is not None
                    retry = asyncio.create_task(
                        account_crud._reconcile_committed_runtime(retry_session, MagicMock(), 1)
                    )
                await asyncio.wait_for(deletion, 5)
                with pytest.raises(HTTPException) as error:
                    await asyncio.wait_for(retry, 5)
                assert error.value.status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["disabled", "deleted", "renamed", "channel_changed"])
def test_session_prepare_reloads_after_waiting_for_runtime_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """盘前候选列表过期时，以锁内重新读取的配置决定跳过或准备。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as (factory, sessions):
            monkeypatch.setattr(ctp_channels, "SessionLocal", factory)
            monkeypatch.setattr(runtime, "SessionLocal", factory)
            prepared: list[str] = []

            class Manager:
                configure_catalog_account = MagicMock()

                async def prepare_account(self, account: Account):
                    assert all(not session.in_transaction() for session in sessions)
                    prepared.append(account.name)
                    return {}

            monkeypatch.setattr(ctp_channels, "get_worker_backend_manager", Manager)
            async with factory() as session:
                old = await session.get(Account, 1)
            assert old is not None
            async with account_runtime_lock(1):
                pending = asyncio.create_task(ctp_channels._prepare_accounts([old], "day"))
                await asyncio.sleep(0)
                async with factory() as session, session.begin():
                    account = await session.get(Account, 1)
                    assert account is not None
                    if change == "deleted":
                        await session.delete(account)
                    elif change == "channel_changed":
                        account.trade_channel = "gm"
                    elif change == "disabled":
                        account.is_started = False
                    else:
                        account.name = "latest"
            await asyncio.wait_for(pending, 5)
            assert prepared == (["latest"] if change == "renamed" else [])

    asyncio.run(scenario())
