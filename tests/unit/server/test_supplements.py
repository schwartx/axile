"""独立补发：时钟、交易和通知全用替身，不访问真实渠道。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from shutil import copyfile
from tempfile import TemporaryDirectory

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server.business_facts.repository import store_execution_facts
from axile.server.cron import SCHEDULER_TIMEZONE
from axile.server.db.models import Account, ExecuteRecord
from axile.server.db.models.account_settings import SupplementSettings
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from axile.server.execution import intents, notification_outbox, supplement_notifications, supplements
from axile.server.execution.registry import clear_queued_execution, clear_running_execution
from axile.server.supplement_plan import plan_supplements
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus

BASE = datetime(2026, 10, 9, 9, 30, tzinfo=SCHEDULER_TIMEZONE)


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory):
    """只共享已关闭的空库文件，各用例仍拥有独立数据库和连接。"""
    path = tmp_path_factory.mktemp("supplement-schema") / "template.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        SQLModel.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


def open_moment(channel, moment):
    return CalendarDayDecision(channel=str(channel), day=moment.date(), status=CalendarDecisionStatus.NOT_REQUIRED)


@asynccontextmanager
async def database(monkeypatch, schema_template, *, enabled=True):
    temporary = TemporaryDirectory()
    copyfile(schema_template, f"{temporary.name}/supplement.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{temporary.name}/supplement.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (intents, supplements, supplement_notifications, notification_outbox):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(intents, "_wake", lambda _id: None)
    monkeypatch.setattr(notification_outbox, "wake_notifications", lambda _id: None)
    monkeypatch.setattr(supplements, "schedule_step", lambda _step: None)
    monkeypatch.setattr(supplements, "schedule_expiration", lambda _group: None)
    monkeypatch.setattr(supplements, "evaluate_channel_calendar_moment", open_moment)
    now = [BASE]
    monkeypatch.setattr(supplements, "clock_now", lambda **_kwargs: now[0])
    async with factory() as session:
        session.add(
            Account(
                id=1,
                name="test",
                market="期货",
                trade_channel="ctp",
                brokerage="ctp",
                is_started=True,
                cron_expr="*/15 * * * *",
                portfolio_id=1,
                algorithm={"method": "SINGLE-MAKER"},
                supplement={"count": 2, "interval_minutes": 1} if enabled else None,
            )
        )
        await session.commit()
    try:
        yield factory, now
    finally:
        clear_queued_execution(1, "")
        clear_running_execution(1, "")
        # Remove only test slots; production registry remains the same API.
        from axile.server.execution.registry import get_queued_execution_id, get_running_execution_id

        clear_queued_execution(1, get_queued_execution_id(1) or "")
        clear_running_execution(1, get_running_execution_id(1) or "")
        await engine.dispose()
        temporary.cleanup()


async def seed(factory, *, count=2, base=BASE):
    async with factory() as session:
        group = SupplementGroup(
            account_id=1,
            fingerprint=supplements.fingerprint(await session.get(Account, 1)),
            base_scheduled_at=base.isoformat(),
            expires_at=(base + timedelta(minutes=15)).isoformat(),
            configured_count=count,
            effective_count=count,
        )
        session.add(group)
        await session.flush()
        steps = [
            SupplementStep(group_id=group.id, index=index, scheduled_at=(base + timedelta(minutes=index)).isoformat())
            for index in range(count + 1)
        ]
        session.add_all(steps)
        await session.commit()
        return group, steps


def test_plan_cuts_next_base_and_session(monkeypatch):
    import axile.server.supplement_plan as planner

    monkeypatch.setattr(planner, "evaluate_channel_calendar_moment", open_moment)
    monkeypatch.setattr(planner, "session_end", lambda _channel, _base: BASE + timedelta(minutes=2))
    points, expires = plan_supplements("ctp", "*/15 * * * *", SupplementSettings(count=4, interval_minutes=1), BASE)
    assert points == [BASE, BASE + timedelta(minutes=1)]
    assert expires == BASE + timedelta(minutes=2)
    monkeypatch.setattr(planner, "session_end", lambda *_args: None)
    points, _ = plan_supplements("ctp", "*/2 * * * *", SupplementSettings(count=4, interval_minutes=1), BASE)
    assert points == [BASE, BASE + timedelta(minutes=1)]


def test_coalescing_preserves_all_steps_and_last_marker(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, now):
            group, steps = await seed(factory)
            for index, step in enumerate(steps):
                now[0] = BASE + timedelta(minutes=index)
                await supplements.fire_step(step.id)
            async with factory() as session:
                rows = list((await session.scalars(select(SupplementStep))).all())
                assert len({step.execution_id for step in rows}) == 1
                assert all(step.status == "queued" for step in rows)
                assert [supplements.supplement_context(group, step)["is_last"] for step in rows] == [False, False, True]

    asyncio.run(run())


def test_cancel_pending_is_idempotent_and_blocks_stale_trigger(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            group, steps = await seed(factory)
            assert await supplements.cancel_groups(1, "terminated")
            assert not await supplements.cancel_groups(1, "terminated")
            await supplements.fire_step(steps[-1].id)
            async with factory() as session:
                assert (await session.get(SupplementGroup, group.id)).status == "cancelled"
                assert len(list((await session.scalars(select(NotificationEvent))).all())) == 1
                assert not list((await session.scalars(select(ExecutionIntent))).all())

    asyncio.run(run())


def test_cancel_preserves_shared_manual_request(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            _group, steps = await seed(factory)
            await supplements.fire_step(steps[0].id)
            result = await intents.submit_intent(1, ExecutionKind.REBALANCE, "manual")
            await supplements.cancel_groups(1, "configuration_changed")
            assert await supplement_notifications.validate_execution_requests(result.execution_id)

    asyncio.run(run())


def test_cancel_revokes_pure_supplement_queue(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            _group, steps = await seed(factory)
            await supplements.fire_step(steps[0].id)
            async with factory() as session:
                execution = (await session.scalars(select(ExecutionIntent))).one()
            await supplements.cancel_groups(1, "account_stopped")
            assert not await supplement_notifications.validate_execution_requests(execution.execution_id)

    asyncio.run(run())


def test_running_cancel_waits_then_orders_execution_before_cancel(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            group, steps = await seed(factory)
            await supplements.fire_step(steps[0].id)
            async with factory() as session:
                execution = (await session.scalars(select(ExecutionIntent))).one()
                execution.status = ExecutionTaskStatus.RUNNING
                execution.started_at = BASE.isoformat()
                session.add(execution)
                await session.commit()
            await supplements.cancel_groups(1, "terminated")
            async with factory() as session:
                assert (await session.get(SupplementGroup, group.id)).status == "cancelling"
                assert not list((await session.scalars(select(NotificationEvent))).all())
            await intents.mark_intent_finished(execution.execution_id, ExecutionTaskStatus.TERMINATED)
            await intents.mark_intent_finished(execution.execution_id, ExecutionTaskStatus.TERMINATED)
            async with factory() as session:
                events = list(
                    (await session.scalars(select(NotificationEvent).order_by(NotificationEvent.sequence))).all()
                )
                assert [event.event_type for event in events] == ["execution.finished", "supplement.cancelled"]
                assert events[0].context["execution"]["supplements"][0]["group_status"] == "cancelling"
                assert events[1].context["execution"] is None
                assert events[1].context["last_execution"]["execution"]["id"] == execution.execution_id

    asyncio.run(run())


def test_expiration_cancels_missed_last_step(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, now):
            group, _steps = await seed(factory)
            now[0] = BASE + timedelta(minutes=16)
            await supplements.expire_group(group.id)
            async with factory() as session:
                event = (await session.scalars(select(NotificationEvent))).one()
                assert event.context["supplement"]["reason"] == "schedule_interrupted"

    asyncio.run(run())


def test_old_account_creates_no_group_or_cancel_event(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template, enabled=False) as (factory, _now):
            await supplements.create_group(1, BASE)
            assert not await supplements.cancel_groups(1, "terminated")
            async with factory() as session:
                assert not list((await session.scalars(select(SupplementGroup))).all())
                assert not list((await session.scalars(select(NotificationEvent))).all())

    asyncio.run(run())


def test_failed_execution_keeps_future_supplements(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            group, steps = await seed(factory)
            await supplements.fire_step(steps[0].id)
            async with factory() as session:
                execution = (await session.scalars(select(ExecutionIntent))).one()
            await intents.mark_intent_finished(
                execution.execution_id, ExecutionTaskStatus.FAILED, error="target calculation failed"
            )
            async with factory() as session:
                assert (await session.get(SupplementGroup, group.id)).status == "active"
                assert (await session.get(SupplementStep, steps[-1].id)).status == "pending"
                event = (await session.scalars(select(NotificationEvent))).one()
                assert event.context["execution"]["status"] == "FAILED"
                assert not event.context["execution"]["supplements"][0]["is_last"]

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["all", "partial", "error"])
def test_session_closed_completion_cancels_only_its_group(monkeypatch, schema_template, scope):
    from tests.unit.server.test_session_closed_execution import closed_result

    async def run():
        async with database(monkeypatch, schema_template) as (factory, now):
            group, steps = await seed(factory)
            next_group, next_steps = await seed(factory, base=BASE + timedelta(minutes=15))
            await supplements.fire_step(steps[0].id)
            async with factory() as session:
                execution = (await session.scalars(select(ExecutionIntent))).one()
                execution.status = ExecutionTaskStatus.RUNNING
                execution.started_at = BASE.isoformat()
                session.add(execution)
                raw = closed_result()
                if scope == "partial":
                    raw["status"] = "PARTIAL"
                    raw["symbol_results"]["ag2612"] = {"status": "NOOP", "target_volume": -6, "final_volume": -6}
                elif scope == "error":
                    raw["symbol_results"]["ag2612"] = {"status": "FAILED", "reason_code": "CHANNEL.QUERY_FAILED"}
                record = ExecuteRecord(account_id=1, execution_id=execution.execution_id, is_success=0)
                session.add(record)
                await store_execution_facts(session, record, {}, raw)
                await session.commit()
            terminal = {
                "all": ExecutionTaskStatus.SKIPPED,
                "partial": ExecutionTaskStatus.PARTIAL,
                "error": ExecutionTaskStatus.FAILED,
            }[scope]
            from axile.server.execution import lifecycle, registry

            async def runner(_execution_id):
                return record

            registry.update_execution_task_state(execution.execution_id, status=ExecutionTaskStatus.QUEUED)
            monkeypatch.setattr(lifecycle, "sync_account_live", lambda *_args: None)
            await lifecycle._run_execution_task(
                account_id=1,
                execution_id=execution.execution_id,
                execution_kind=ExecutionKind.REBALANCE,
                runner=runner,
            )
            state = registry.get_execution_task_state(execution.execution_id)
            assert state.status == terminal
            assert state.output_status == raw["status"]
            assert state.is_success == 0
            assert state.error == "非交易时段"
            # 幂等收尾不能重复取消/通知，也不能影响下一组。
            await intents.mark_intent_finished(execution.execution_id, terminal)
            async with factory() as session:
                finished = (await session.scalars(select(ExecutionIntent))).one()
                assert finished.status == terminal
                assert finished.finished_at is not None
                actual_group = await session.get(SupplementGroup, group.id)
                assert actual_group.status == ("cancelled" if scope == "all" else "active")
                assert (await session.get(SupplementStep, steps[0].id)).status == "finished"
                assert (await session.get(SupplementStep, steps[-1].id)).status == (
                    "cancelled" if scope == "all" else "pending"
                )
                assert (await session.get(SupplementGroup, next_group.id)).status == "active"
                events = list(
                    (await session.scalars(select(NotificationEvent).order_by(NotificationEvent.sequence))).all()
                )
                assert [event.event_type for event in events] == (
                    ["execution.finished", "supplement.cancelled"] if scope == "all" else ["execution.finished"]
                )
            if scope == "all":
                await supplements.fire_step(steps[-1].id)
                now[0] = BASE + timedelta(minutes=15)
                clear_queued_execution(1, execution.execution_id)
                await supplements.fire_step(next_steps[0].id)
                async with factory() as session:
                    assert (await session.get(SupplementStep, next_steps[0].id)).status == "queued"
                    assert len(list((await session.scalars(select(ExecutionIntent))).all())) == 2

    asyncio.run(run())


def test_session_skip_preserves_manual_request_sharing_a_future_step(monkeypatch, schema_template):
    from tests.unit.server.test_session_closed_execution import closed_result

    async def run():
        async with database(monkeypatch, schema_template) as (factory, now):
            group, steps = await seed(factory)
            await supplements.fire_step(steps[0].id)
            async with factory() as session:
                running = (await session.scalars(select(ExecutionIntent))).one()
                running.status = ExecutionTaskStatus.RUNNING
                running.started_at = BASE.isoformat()
                session.add(running)
                record = ExecuteRecord(account_id=1, execution_id=running.execution_id, is_success=0)
                session.add(record)
                await store_execution_facts(session, record, {}, closed_result())
                await session.commit()
            clear_queued_execution(1, running.execution_id)
            manual = await intents.submit_intent(1, ExecutionKind.REBALANCE, "manual")
            now[0] = BASE + timedelta(minutes=1)
            await supplements.fire_step(steps[1].id)
            await intents.mark_intent_finished(running.execution_id, ExecutionTaskStatus.SKIPPED)
            async with factory() as session:
                queued = await session.scalar(
                    select(ExecutionIntent).where(ExecutionIntent.execution_id == manual.execution_id)
                )
                assert queued.status == ExecutionTaskStatus.QUEUED
                assert queued.payload["independent_request"]
                assert (await session.get(SupplementStep, steps[1].id)).status == "cancelled"
                assert (await session.get(SupplementGroup, group.id)).status == "cancelled"
            assert await supplement_notifications.validate_execution_requests(manual.execution_id)

    asyncio.run(run())


def test_recovery_only_restores_future_steps(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, now):
            group, steps = await seed(factory)
            async with factory() as session:
                step = await session.get(SupplementStep, steps[0].id)
                step.status = "finished"
                session.add(step)
                await session.commit()
            now[0] = BASE + timedelta(seconds=30)
            scheduled = []
            monkeypatch.setattr(supplements, "schedule_step", lambda step: scheduled.append(step.id))
            await supplements.recover_supplements()
            assert scheduled == [steps[1].id, steps[2].id]
            now[0] = BASE + timedelta(minutes=3)
            scheduled.clear()
            await supplements.recover_supplements()
            assert scheduled == []
            async with factory() as session:
                assert (await session.get(SupplementGroup, group.id)).status == "cancelled"

    asyncio.run(run())


def test_cancel_races_with_trigger_without_starting_invalid_request(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            _group, steps = await seed(factory)
            await asyncio.gather(supplements.fire_step(steps[0].id), supplements.cancel_groups(1, "terminated"))
            async with factory() as session:
                queued = list(
                    (
                        await session.scalars(
                            select(ExecutionIntent).where(ExecutionIntent.status == ExecutionTaskStatus.QUEUED)
                        )
                    ).all()
                )
                assert queued == []
                assert len(list((await session.scalars(select(NotificationEvent))).all())) == 1

    asyncio.run(run())


def test_terminate_between_executions_uses_existing_route(monkeypatch, schema_template):
    from fastapi import HTTPException, Response

    from axile.server.api.routes import account_execution
    from axile.server.db.models.execution import ExecutionTerminateRequest

    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            await seed(factory)
            async with factory() as session:
                response = Response()
                result = await account_execution.terminate_account_execution(
                    session, 1, ExecutionTerminateRequest(), response
                )
                assert response.status_code == 200
                assert result.execution_id is None
                assert result.status is None
                assert result.message == "已取消剩余补发"
        async with database(monkeypatch, schema_template, enabled=False) as (factory, _now):
            async with factory() as session:
                with pytest.raises(HTTPException) as caught:
                    await account_execution.terminate_account_execution(
                        session, 1, ExecutionTerminateRequest(), Response()
                    )
                assert caught.value.status_code == 409

    asyncio.run(run())


@pytest.mark.parametrize(("interval", "expected_count"), [(1, 2), (8, 1)])
def test_preview_recalculates_selected_bases_without_moving_timeline(monkeypatch, interval, expected_count):
    import axile.server.supplement_plan as planner
    from axile.server.api.routes import account_schedule

    monkeypatch.setattr(account_schedule, "evaluate_channel_calendar_moment", open_moment)
    monkeypatch.setattr(planner, "evaluate_channel_calendar_moment", open_moment)
    monkeypatch.setattr(planner, "session_end", lambda *_args: None)
    monkeypatch.setattr(account_schedule, "clock_now", lambda **_kwargs: BASE + timedelta(days=1))
    bases = [BASE, BASE + timedelta(minutes=15)]
    payload = account_schedule.SchedulePreviewRequest(
        trade_channel="ctp",
        cron_expr="*/15 * * * *",
        supplement=SupplementSettings(count=2, interval_minutes=interval),
        scheduled_ats=bases,
        limit=1,
    )
    response = asyncio.run(account_schedule.schedule_preview(payload))
    assert [item.scheduled_at for item in response.items] == bases
    assert [item.base_scheduled_at for item in response.items] == bases
    assert all(item.index == 0 and item.effective_count == expected_count for item in response.items)
    assert response.next_cursor is None
    assert not response.has_more


def test_preview_paginates_supplements_from_same_base(monkeypatch):
    import axile.server.supplement_plan as planner
    from axile.server.api.routes import account_schedule

    monkeypatch.setattr(account_schedule, "evaluate_channel_calendar_moment", open_moment)
    monkeypatch.setattr(planner, "evaluate_channel_calendar_moment", open_moment)
    monkeypatch.setattr(planner, "session_end", lambda *_args: None)
    monkeypatch.setattr(
        account_schedule,
        "_calendar_summary",
        lambda *_args: account_schedule.SchedulePreviewCalendar(
            requirement="not_required", availability="not_required"
        ),
    )

    async def run():
        payload = account_schedule.SchedulePreviewRequest(
            trade_channel="ctp",
            cron_expr="*/15 * * * *",
            supplement=SupplementSettings(count=2, interval_minutes=1),
            after=BASE + timedelta(seconds=30),
            limit=1,
        )
        first = await account_schedule.schedule_preview(payload)
        assert first.items[0].scheduled_at == BASE + timedelta(minutes=1)
        assert first.items[0].base_scheduled_at == BASE
        assert not first.items[0].is_last
        second = await account_schedule.schedule_preview(payload.model_copy(update={"after": first.next_cursor}))
        assert second.items[0].scheduled_at == BASE + timedelta(minutes=2)
        assert second.items[0].is_last

    asyncio.run(run())


def test_outbox_silent_success_failure_and_unknown_are_not_retried(monkeypatch, schema_template):
    from axile.common.notification_function import NotificationFunctionResult

    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            async with factory() as session:
                account = await session.get(Account, 1)
                account.execution_notification_code = "def notify(context):\n    pass"
                session.add(account)
                for index, status in enumerate(["pending", "pending", "calling"]):
                    session.add(
                        NotificationEvent(
                            id=f"test:{index}",
                            account_id=1,
                            event_type="supplement.cancelled",
                            status=status,
                            context={"execution": None},
                        )
                    )
                await session.commit()
            calls = []

            def invoke(_code, context, **_kwargs):
                calls.append(context)
                return NotificationFunctionResult(len(calls) == 1, None if len(calls) == 1 else "test failure")

            monkeypatch.setattr(notification_outbox, "run_notification_function", invoke)
            monkeypatch.setattr(notification_outbox, "record_notification_result", lambda *_args: None)
            await notification_outbox.recover_notifications()
            await notification_outbox._dispatch(1)
            await notification_outbox._dispatch(1)
            assert len(calls) == 2
            async with factory() as session:
                rows = list((await session.scalars(select(NotificationEvent).order_by(NotificationEvent.id))).all())
                assert [row.status for row in rows] == ["succeeded", "failed", "unknown"]

    asyncio.run(run())


def test_recovery_skips_intermediate_miss_but_keeps_future_last(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, now):
            group, steps = await seed(factory)
            async with factory() as session:
                base = await session.get(SupplementStep, steps[0].id)
                base.status = "finished"
                session.add(base)
                await session.commit()
            now[0] = BASE + timedelta(minutes=1, seconds=30)
            scheduled = []
            monkeypatch.setattr(supplements, "schedule_step", lambda step: scheduled.append(step.id))
            await supplements.recover_supplements()
            assert scheduled == [steps[2].id]
            async with factory() as session:
                assert (await session.get(SupplementGroup, group.id)).status == "active"
                assert (await session.get(SupplementStep, steps[1].id)).status == "cancelled"
                assert not list((await session.scalars(select(NotificationEvent))).all())

    asyncio.run(run())


def test_migration_only_upgrades_exact_old_default():
    import importlib.util
    import json
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE

    path = Path(__file__).parents[3] / "axile/server/alembic/versions/0023_supplements.py"
    spec = importlib.util.spec_from_file_location("supplement_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE account (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE account_settings (account_id INTEGER PRIMARY KEY, notification JSON, schedule JSON)"
        )
        connection.exec_driver_sql("CREATE TABLE account_notification_state (account_id INTEGER PRIMARY KEY)")
        for index, code in enumerate([migration.OLD_DEFAULT, migration.OLD_DEFAULT + "# custom\n"], start=1):
            connection.execute(sa.text("INSERT INTO account VALUES (:id)"), {"id": index})
            connection.execute(
                sa.text("INSERT INTO account_settings VALUES (:id, :notification, :schedule)"),
                {
                    "id": index,
                    "notification": json.dumps({"execution_notification_code": code, "feishu_key": "unchanged"}),
                    "schedule": json.dumps({"cron_expr": "0,1,2 * * * *", "is_started": False}),
                },
            )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            rows = list(
                connection.execute(sa.text("SELECT notification, schedule FROM account_settings ORDER BY account_id"))
            )
            assert json.loads(rows[0][0])["execution_notification_code"] == DEFAULT_ACCOUNT_NOTIFICATION_CODE
            assert json.loads(rows[1][0])["execution_notification_code"] == migration.OLD_DEFAULT + "# custom\n"
            assert all(json.loads(row[0])["feishu_key"] == "unchanged" for row in rows)
            assert all("supplement" not in json.loads(row[1]) for row in rows)
            migration.downgrade()
    engine.dispose()


def test_stale_base_callback_cannot_use_changed_cron(monkeypatch, schema_template):
    async def run():
        async with database(monkeypatch, schema_template) as (factory, _now):
            async with factory() as session:
                account = await session.get(Account, 1)
                account.cron_expr = "45 9 * * *"
                session.add(account)
                await session.commit()
            await supplements.create_group(1, BASE)
            async with factory() as session:
                assert not list((await session.scalars(select(SupplementGroup))).all())

    asyncio.run(run())
