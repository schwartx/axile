"""主动核对记录联动持仓落库，普通读取不查询渠道。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.server import account_assets as assets
from axile.server import account_orders as orders
from axile.server.business_facts.assets import latest_asset_snapshot, save_manual_asset_snapshot
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._runtime_db_support import runtime_database


@pytest.fixture(autouse=True)
def clear_state():
    orders._states.clear()
    assets._refreshes.clear()
    yield
    orders._states.clear()
    assets._refreshes.clear()


def asset(volume):
    return UnifiedAccountAssets(
        total_asset=1000,
        available_cash=600,
        market_value=400,
        positions=[
            Position(symbol="rb2610", direction="多头", volume=volume, available_volume=volume, market_value=400)
        ],
    )


def activity():
    return dict(
        channel="",
        ended_orders=[],
        queried_at=None,
        errors=[],
        truncated=False,
        asset_refresh=None,
        orders=[],
        trades=[],
        failures=[],
        scope="本次连接",
        started_at="now",
        sampled_at="now",
        symbols=[],
        limit=100,
    )


@pytest.mark.parametrize("entry", ["activity", "orders"])
def test_explicit_query_commits_position_before_return_and_reloading(monkeypatch, tmp_path, entry):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            previous = await save_manual_asset_snapshot(1, asset(1).model_dump(mode="json"))
            command = AsyncMock(return_value=activity() if entry == "activity" else dict(data=[], sampled_at="now"))

            async def after_records(_):
                assert command.await_count == 1
                return asset(3)

            query = AsyncMock(side_effect=after_records)
            monkeypatch.setattr(orders, "_command", command)
            monkeypatch.setattr(assets, "query_account_assets", query)
            call = orders.query_account_activity if entry == "activity" else orders.query_account_orders
            result = await call(build_account(id=1), refresh=True)
            assert result["asset_refresh"]["status"] == "updated"
            assert result["asset_refresh"]["snapshot_id"] != previous["id"]
            saved = await latest_asset_snapshot(1)
            assert saved["id"] == result["asset_refresh"]["snapshot_id"]
            assert saved["assets"]["positions"][0]["volume"] == 3
            assert saved["assets"]["positions"][0]["available_volume"] == 3
            # 后续普通读取仍读快照/镜像，不重复查询资产。
            await call(build_account(id=1), refresh=False)
            query.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("busy", [False, True])
def test_partial_failure_keeps_records_and_previous_position(monkeypatch, tmp_path, busy):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            previous = await save_manual_asset_snapshot(1, asset(1).model_dump(mode="json"))
            monkeypatch.setattr(orders, "_command", AsyncMock(return_value=activity()))
            query = AsyncMock(side_effect=RuntimeError("password=secret"))
            monkeypatch.setattr(assets, "query_account_assets", query)
            if busy:
                monkeypatch.setattr(assets, "try_register_account_asset_refresh", lambda _: False)
                monkeypatch.setattr(orders, "get_running_execution_id", lambda _: "active-execution")
            result = await orders.query_account_activity(build_account(id=1), refresh=True)
            assert result["scope"] == "本次连接"
            assert result["asset_refresh"]["status"] == ("pending" if busy else "failed")
            assert result["asset_refresh"].get("snapshot_id") is None
            assert "secret" not in json.dumps(result)
            assert (await latest_asset_snapshot(1))["id"] == previous["id"]
            if busy:
                query.assert_not_awaited()

    asyncio.run(scenario())


def test_refresh_coalesces_and_survives_a_cancelled_http_waiter(monkeypatch, tmp_path):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            started, release = asyncio.Event(), asyncio.Event()

            async def query(_):
                started.set()
                await release.wait()
                return asset(3)

            mock = AsyncMock(side_effect=query)
            monkeypatch.setattr(assets, "query_account_assets", mock)
            first = asyncio.create_task(assets.refresh_account_assets(build_account(id=1)))
            await started.wait()
            second = asyncio.create_task(assets.refresh_account_assets(build_account(id=1)))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            result = await second
            mock.assert_awaited_once()
            assert (await latest_asset_snapshot(1))["id"] == result["id"]

    asyncio.run(scenario())


def test_asset_wait_does_not_hold_order_lock_and_reads_remain_available(monkeypatch, tmp_path):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            started, release = asyncio.Event(), asyncio.Event()

            async def query(_):
                started.set()
                await release.wait()
                return asset(3)

            command = AsyncMock(return_value=activity())
            monkeypatch.setattr(assets, "query_account_assets", query)
            monkeypatch.setattr(orders, "_command", command)
            account = build_account(id=1)
            pending = asyncio.create_task(orders.query_account_activity(account, refresh=True))
            await started.wait()
            observed = await asyncio.wait_for(orders.query_account_activity(account), timeout=1)
            assert observed["asset_refresh"] is None
            assert not pending.done()
            release.set()
            await pending

    asyncio.run(scenario())


def test_exhausted_record_budget_returns_pending_and_finishes_saving(monkeypatch, tmp_path):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            started, release = asyncio.Event(), asyncio.Event()

            async def query(_):
                started.set()
                await release.wait()
                return asset(3)

            monkeypatch.setattr(assets, "query_account_assets", query)
            account = build_account(id=1)
            result = await orders._reconcile_assets(account, asyncio.get_running_loop().time())
            assert result["status"] == "pending"
            await started.wait()
            release.set()
            saved = await assets.refresh_account_assets(account)
            assert (await latest_asset_snapshot(1))["id"] == saved["id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["activity", "orders"])
def test_closing_the_dialog_during_record_query_still_updates_positions(monkeypatch, tmp_path, entry):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            started, release = asyncio.Event(), asyncio.Event()

            async def command(*_):
                started.set()
                await release.wait()
                return activity() if entry == "activity" else dict(data=[], sampled_at="now")

            monkeypatch.setattr(orders, "_command", command)
            monkeypatch.setattr(assets, "query_account_assets", AsyncMock(return_value=asset(3)))
            call = orders.query_account_activity if entry == "activity" else orders.query_account_orders
            request = asyncio.create_task(call(build_account(id=1), refresh=True))
            await started.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            release.set()
            await asyncio.gather(*tuple(orders._tasks))
            assert (await latest_asset_snapshot(1))["assets"]["positions"][0]["volume"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["activity", "orders"])
def test_record_timeout_still_updates_positions_after_the_query_finishes(monkeypatch, tmp_path, entry):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            release = asyncio.Event()
            wait_for = asyncio.wait_for

            async def short_wait(awaitable, timeout):
                return await wait_for(awaitable, 0.01 if timeout == 35 else timeout)

            async def command(*_):
                await release.wait()
                return activity() if entry == "activity" else dict(data=[], sampled_at="now")

            monkeypatch.setattr(asyncio, "wait_for", short_wait)
            monkeypatch.setattr(orders, "_command", command)
            query = AsyncMock(return_value=asset(3))
            monkeypatch.setattr(assets, "query_account_assets", query)
            call = orders.query_account_activity if entry == "activity" else orders.query_account_orders
            with pytest.raises(TimeoutError):
                await call(build_account(id=1), refresh=True)
            query.assert_not_awaited()
            release.set()
            while orders._tasks:
                await asyncio.gather(*tuple(orders._tasks))
            query.assert_awaited_once()
            assert (await latest_asset_snapshot(1))["assets"]["positions"][0]["volume"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["activity", "orders"])
@pytest.mark.parametrize("previous_fails", [False, True])
def test_record_reconciliation_waits_for_older_query_then_reads_new_positions(
    monkeypatch, tmp_path, entry, previous_fails
):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            started, release, reconciling = asyncio.Event(), asyncio.Event(), asyncio.Event()
            current_volume = 1
            calls = 0

            async def query(_):
                nonlocal calls
                calls += 1
                captured = current_volume
                if calls == 1:
                    started.set()
                    await release.wait()
                    if previous_fails:
                        raise RuntimeError("previous query failed")
                return asset(captured)

            async def command(*_):
                nonlocal current_volume
                current_volume = 3
                return activity() if entry == "activity" else dict(data=[], sampled_at="now")

            async def reconcile(account):
                reconciling.set()
                return await assets.reconcile_account_assets(account)

            monkeypatch.setattr(assets, "query_account_assets", query)
            monkeypatch.setattr(orders, "_command", command)
            monkeypatch.setattr(orders, "reconcile_account_assets", reconcile)
            account = build_account(id=1)
            previous = asyncio.create_task(assets.refresh_account_assets(account))
            await started.wait()
            call = orders.query_account_activity if entry == "activity" else orders.query_account_orders
            request = asyncio.create_task(call(account, refresh=True))
            await reconciling.wait()
            release.set()
            result = await request
            if previous_fails:
                with pytest.raises(RuntimeError, match="previous query failed"):
                    await previous
            else:
                assert (await previous)["assets"]["positions"][0]["volume"] == 1
            assert calls == 2
            assert result["asset_refresh"]["status"] == "updated"
            saved = await latest_asset_snapshot(1)
            assert saved["id"] == result["asset_refresh"]["snapshot_id"]
            assert saved["assets"]["positions"][0]["volume"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_status_read_reports_its_own_background_result_not_an_unrelated_snapshot(monkeypatch, tmp_path, fails):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            release, started = asyncio.Event(), asyncio.Event()

            async def query(_):
                started.set()
                await release.wait()
                if fails:
                    raise RuntimeError("query failed")
                return asset(3)

            monkeypatch.setattr(assets, "query_account_assets", query)
            account = build_account(id=1)
            pending = await orders._reconcile_assets(account, asyncio.get_running_loop().time())
            identity = pending["reconciliation_id"]
            assert identity and pending["status"] == "pending"
            await started.wait()
            # 任意更大的快照编号都不能完成这次仍在运行的任务。
            await save_manual_asset_snapshot(1, asset(1).model_dump(mode="json"))
            status = await orders.query_account_orders(account, reconciliation_id=identity)
            assert status["asset_refresh"]["status"] == "pending"
            release.set()
            await asyncio.gather(*tuple(orders._tasks))
            status = await orders.query_account_orders(account, reconciliation_id=identity)
            assert status["asset_refresh"]["reconciliation_id"] == identity
            assert status["asset_refresh"]["status"] == ("failed" if fails else "updated")
            if not fails:
                assert status["asset_refresh"]["snapshot_id"] == (await latest_asset_snapshot(1))["id"]
            unknown = await orders.query_account_orders(account, reconciliation_id="0" * 32)
            assert unknown["asset_refresh"]["status"] == "failed"
            foreign = await orders.query_account_orders(build_account(id=2), reconciliation_id=identity)
            assert foreign["asset_refresh"]["status"] == "failed"
            assert foreign["asset_refresh"].get("snapshot_id") is None

    asyncio.run(scenario())


def test_deferred_reconciliation_waits_for_execution_without_querying_channel(monkeypatch, tmp_path):
    async def scenario():
        async with runtime_database(tmp_path / "positions.db"):
            release, waiting = asyncio.Event(), asyncio.Event()
            busy = True
            register = assets.try_register_account_asset_refresh

            async def wait(_):
                waiting.set()
                await release.wait()

            monkeypatch.setattr(assets, "clock_async_sleep", wait)
            monkeypatch.setattr(
                assets, "try_register_account_asset_refresh", lambda account_id: not busy and register(account_id)
            )
            monkeypatch.setattr(orders, "get_running_execution_id", lambda _: "execution" if busy else None)
            query = AsyncMock(return_value=asset(3))
            monkeypatch.setattr(assets, "query_account_assets", query)
            account = build_account(id=1)
            result = await orders._reconcile_assets(account, asyncio.get_running_loop().time() + 35)
            assert result["status"] == "pending"
            await waiting.wait()
            query.assert_not_awaited()
            busy = False
            release.set()
            await asyncio.gather(*tuple(orders._tasks))
            status = await orders.query_account_orders(account, reconciliation_id=result["reconciliation_id"])
            assert status["asset_refresh"]["status"] == "updated"
            query.assert_awaited_once()

    asyncio.run(scenario())
