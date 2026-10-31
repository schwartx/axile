"""补发状态机经真实原生事务运行；SQLite 只用于测试夹具与结果核对。"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta

import pytest

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server.cron import SCHEDULER_TIMEZONE
from axile.server.db.models.account_settings import SupplementSettings
from axile.server.execution import (
    dispatcher,
    intents,
    lifecycle,
    notification_outbox,
    records,
    registry,
    supplement_notifications,
    supplements,
)
from axile.server.execution.registry import clear_queued_execution, clear_running_execution
from axile.server.supplement_plan import plan_supplements
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus
from tests.fixtures.rust_store.supplement_rows import SupplementGroup, SupplementStep
from tests.sqlite_oracle import connect as oracle_connect
from tests.sqlite_oracle import to_us
from tests.unit.server._native_read_views import persist_record_fixture

BASE = datetime(2026, 10, 9, 9, 30, tzinfo=SCHEDULER_TIMEZONE)


def call(path, operation, request):
    from tests.conftest import native_store_call

    return native_store_call(path, operation, request)


def query(path, sql, args=()):
    with closing(oracle_connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        result = [dict(row) for row in connection.execute(sql, args)]
    for row in result:
        if "context" in row:
            row["context"] = json.loads(row["context"])
    return result


def execute(path, sql, args=()):
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(sql, args)


def open_moment(channel, moment):
    return CalendarDayDecision(channel=str(channel), day=moment.date(), status=CalendarDecisionStatus.NOT_REQUIRED)


@pytest.fixture
def database(monkeypatch, native_database):
    from axile.server.core import db
    from tests.unit.server._execution_test_support import build_account, insert_account

    path = native_database
    monkeypatch.setattr(db, "database_path", lambda: str(path))
    monkeypatch.setattr(intents, "_wake", lambda _id: None)
    monkeypatch.setattr(notification_outbox, "wake_notifications", lambda _id: None)
    monkeypatch.setattr(supplements, "schedule_step", lambda _step: None)
    monkeypatch.setattr(supplements, "schedule_expiration", lambda _group: None)
    monkeypatch.setattr(supplements, "evaluate_channel_calendar_moment", open_moment)
    now = [BASE]
    monkeypatch.setattr(supplements, "clock_now", lambda **_kwargs: now[0])
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at_us,updated_at_us) VALUES (1,?,?,?,?,?)",
            (
                "test",
                "期货",
                "def calculate_portfolio(context): return {}",
                to_us(BASE.isoformat()),
                to_us(BASE.isoformat()),
            ),
        )
        insert_account(
            connection, build_account(cron_expr="*/15 * * * *", supplement={"count": 2, "interval_minutes": 1})
        )
    yield path, now
    from axile.server.execution.registry import get_queued_execution_id, get_running_execution_id

    clear_queued_execution(1, get_queued_execution_id(1) or "")
    clear_running_execution(1, get_running_execution_id(1) or "")


async def seed(path, *, count=2, base=BASE):
    account = await supplements.get_account_snapshot(1)
    group = SupplementGroup(
        account_id=1,
        fingerprint=supplements.fingerprint(account),
        base_scheduled_at=base.isoformat(),
        expires_at=(base + timedelta(minutes=15)).isoformat(),
        configured_count=count,
        effective_count=count,
    )
    steps = [
        SupplementStep(group_id=group.id, index=index, scheduled_at=(base + timedelta(minutes=index)).isoformat())
        for index in range(count + 1)
    ]
    saved = call(
        path,
        "supplement_create_group",
        {
            "account_id": 1,
            "group": group.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in steps],
        },
    )
    assert saved is not None
    return group, steps


def step(path, step_id):
    return query(path, "SELECT * FROM supplement_step WHERE id=?", (step_id,))[0]


def group(path, group_id):
    return query(path, "SELECT * FROM supplement_group WHERE id=?", (group_id,))[0]


def events(path):
    return query(path, "SELECT * FROM notification_event ORDER BY sequence")


def running(path, execution_id):
    execute(
        path,
        "UPDATE execution_intent SET status='RUNNING',started_at_us=? WHERE execution_id=?",
        (to_us(BASE.isoformat()), execution_id),
    )
    clear_queued_execution(1, execution_id)


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL", "TERMINATED"])
def test_result_commit_survives_interruption_replay_and_restart(database, status):
    from axile.server._native import _core

    path, _now = database

    async def run():
        planned, steps = await seed(path)
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        running(path, execution_id)
        request = {
            "account_id": 1,
            "execution_id": execution_id,
            "created_at": BASE.isoformat(),
            "is_success": int(status == "SUCCEEDED"),
            "raw_result": {"task_status": status, "status": status},
        }
        execute(
            path,
            "CREATE TRIGGER reject_finish BEFORE INSERT ON notification_event BEGIN SELECT RAISE(ABORT,'before-commit'); END",
        )
        with pytest.raises(_core.NativeError, match="before-commit"):
            persist_record_fixture(path, request)
        assert not query(path, "SELECT * FROM executerecord")
        assert (await intents.get_intent(execution_id)).status == ExecutionTaskStatus.RUNNING
        assert step(path, steps[0].id)["status"] == "queued"
        assert not events(path)
        execute(path, "DROP TRIGGER reject_finish")
        _core.close_database(str(path))
        interrupted = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from axile.server._native import _core; _core.persist_execution_state(sys.argv[1],sys.argv[2]); os._exit(73)",
                str(path),
                json.dumps(request),
            ],
            timeout=10,
            capture_output=True,
        )
        assert interrupted.returncode == 73, interrupted.stderr.decode()
        saved = query(path, "SELECT * FROM executerecord")
        assert len(saved) == 1 and saved[0]["task_status"] == status
        assert (await intents.get_intent(execution_id)).status.value == status
        assert step(path, steps[0].id)["status"] == "finished"
        replay = persist_record_fixture(
            path, {**request, "is_success": 0, "raw_result": {"status": "FAILED", "error": "late retry"}}
        )
        assert replay["id"] == saved[0]["id"]
        assert query(path, "SELECT * FROM executerecord") == saved
        await dispatcher.recover_intents_on_startup()
        assert (await intents.get_intent(execution_id)).status.value == status
        assert len(events(path)) == 1
        assert events(path)[0]["context"]["execution"]["status"] == status
        assert group(path, planned.id)["status"] == "active"
        assert step(path, steps[-1].id)["status"] == "pending"

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["receipt", "notification"])
def test_postcommit_failure_keeps_execution_success_and_future_supplements(database, monkeypatch, failure):
    from axile.common.notification_function import NotificationFunctionResult

    path, _now = database
    operation = records.native_operation
    lost = False

    async def lose_receipt(name, request, **kwargs):
        nonlocal lost
        result = await operation(name, request, **kwargs)
        if name == "persist_execution_state" and not lost:
            lost = True
            raise RuntimeError("receipt lost after commit")
        return result

    if failure == "receipt":
        monkeypatch.setattr(records, "native_operation", lose_receipt)
    else:
        execute(
            path,
            "UPDATE account_settings SET notification=json_set(notification,'$.execution_notification_code',?)",
            ("def notify(context):\n    pass",),
        )
        monkeypatch.setattr(
            notification_outbox,
            "run_notification_function",
            lambda *_a, **_k: NotificationFunctionResult(False, "delivery failed"),
        )
    monkeypatch.setattr(lifecycle, "sync_account_live", lambda _id: None)

    async def run():
        planned, steps = await seed(path)
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        running(path, execution_id)
        state = registry.ExecutionTaskState(
            execution_id=execution_id,
            account_id=1,
            execution_kind=ExecutionKind.REBALANCE,
            status=ExecutionTaskStatus.RUNNING,
            created_at=BASE.isoformat(),
        )
        registry.set_execution_task_state(execution_id, state)

        async def runner(_id):
            return await records._persist_execute_record(
                account_id=1, execution_id=execution_id, raw_input={}, raw_result={"status": "SUCCEEDED"}, is_success=1
            )

        try:
            await lifecycle._run_execution_task(
                account_id=1, execution_id=execution_id, execution_kind=ExecutionKind.REBALANCE, runner=runner
            )
            if failure == "notification":
                await notification_outbox._dispatch(1)
                assert events(path)[0]["status"] == "failed"
            assert (await intents.get_intent(execution_id)).status == ExecutionTaskStatus.SUCCEEDED
            assert state.status == ExecutionTaskStatus.SUCCEEDED and state.error is None
            assert len(query(path, "SELECT * FROM executerecord")) == len(events(path)) == 1
            assert query(path, "SELECT task_status FROM executerecord")[0]["task_status"] == "SUCCEEDED"
            assert group(path, planned.id)["status"] == "active"
            assert step(path, steps[-1].id)["status"] == "pending"
        finally:
            registry._execution_tasks.pop(execution_id, None)

    asyncio.run(run())


async def save_closed_record(execution_id, *, scope="all"):
    raw = {
        "status": "BLOCKED",
        "reason_code": "COMMON.SESSION.CLOSED",
        "error": "非交易时段",
        "success": False,
        "symbol_results": {
            "m2701": {
                "status": "BLOCKED",
                "reason_code": "COMMON.SESSION.CLOSED",
                "target_volume": -38,
                "final_volume": None,
                "orders": [],
                "trades": [],
            }
        },
    }
    if scope == "partial":
        raw["status"] = "PARTIAL"
        raw["symbol_results"]["ag2612"] = {"status": "NOOP", "target_volume": -6, "final_volume": -6}
    elif scope == "error":
        raw["symbol_results"]["ag2612"] = {"status": "FAILED", "reason_code": "CHANNEL.QUERY_FAILED"}
    return await records._persist_execute_record(
        account_id=1, execution_id=execution_id, raw_input={}, raw_result=raw, is_success=0
    )


def test_coalescing_preserves_all_steps_and_last_marker(database):
    path, now = database

    async def run():
        planned, steps = await seed(path)
        for index, current in enumerate(steps):
            now[0] = BASE + timedelta(minutes=index)
            await supplements.fire_step(current.id)
        rows = query(path, 'SELECT * FROM supplement_step ORDER BY "index"')
        assert len({row["execution_id"] for row in rows}) == 1
        assert all(row["status"] == "queued" for row in rows)

    asyncio.run(run())


def test_cancel_pending_is_idempotent_and_blocks_stale_trigger(database):
    path, _now = database

    async def run():
        planned, steps = await seed(path)
        assert await supplements.cancel_groups(1, "terminated")
        assert not await supplements.cancel_groups(1, "terminated")
        await supplements.fire_step(steps[-1].id)
        assert group(path, planned.id)["status"] == "cancelled"
        assert len(events(path)) == 1
        assert not query(path, "SELECT * FROM execution_intent")

    asyncio.run(run())


def test_native_create_deduplicates_and_rejects_a_stale_configuration(database):
    path, _now = database

    async def run():
        planned, steps = await seed(path)
        duplicate = planned.model_copy(update={"id": "duplicate"})
        request = {
            "account_id": 1,
            "group": duplicate.model_dump(mode="json"),
            "steps": [
                current.model_copy(update={"id": f"duplicate-{index}", "group_id": duplicate.id}).model_dump()
                for index, current in enumerate(steps)
            ],
        }
        assert call(path, "supplement_create_group", request) is None
        execute(path, "UPDATE account_settings SET schedule=json_set(schedule,'$.cron_expr','45 9 * * *')")
        request["group"]["base_scheduled_at"] = (BASE + timedelta(minutes=15)).isoformat()
        assert call(path, "supplement_create_group", request) is None
        assert len(query(path, "SELECT * FROM supplement_group")) == 1
        assert len(query(path, "SELECT * FROM supplement_step")) == 3

    asyncio.run(run())


@pytest.mark.parametrize("manual", [False, True])
def test_cancel_preserves_only_shared_manual_requests(database, manual):
    path, _now = database

    async def run():
        _planned, steps = await seed(path)
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        if manual:
            shared = await intents.submit_intent(1, ExecutionKind.REBALANCE, "manual")
            assert shared.execution_id == execution_id
        await supplements.cancel_groups(1, "configuration_changed")
        assert await supplement_notifications.validate_execution_requests(execution_id) is manual
        row = query(path, "SELECT status FROM execution_intent WHERE execution_id=?", (execution_id,))[0]
        assert row["status"] == ("QUEUED" if manual else "TERMINATED")

    asyncio.run(run())


def test_running_cancel_waits_then_orders_execution_before_cancel(database):
    path, _now = database

    async def run():
        planned, steps = await seed(path)
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        running(path, execution_id)
        await supplements.cancel_groups(1, "terminated")
        assert group(path, planned.id)["status"] == "cancelling"
        assert not events(path)
        await intents.mark_intent_finished(execution_id, ExecutionTaskStatus.TERMINATED)
        await intents.mark_intent_finished(execution_id, ExecutionTaskStatus.TERMINATED)
        rows = events(path)
        assert [row["event_type"] for row in rows] == ["execution.finished", "supplement.cancelled"]
        assert rows[0]["context"]["execution"]["supplements"][0]["group_status"] == "cancelling"
        assert rows[1]["context"]["execution"] is None
        assert rows[1]["context"]["last_execution"]["execution"]["id"] == execution_id

    asyncio.run(run())


def test_expiration_cancels_missed_last_step(database):
    path, now = database

    async def run():
        planned, _steps = await seed(path)
        now[0] = BASE + timedelta(minutes=16)
        await supplements.expire_group(planned.id)
        assert events(path)[0]["context"]["supplement"]["reason"] == "schedule_interrupted"

    asyncio.run(run())


def test_old_account_creates_no_group_or_cancel_event(database):
    path, _now = database
    execute(path, "UPDATE account_settings SET schedule=json_set(schedule,'$.supplement',json('null'))")

    async def run():
        await supplements.create_group(1, BASE)
        assert not await supplements.cancel_groups(1, "terminated")
        assert not query(path, "SELECT * FROM supplement_group")
        assert not events(path)

    asyncio.run(run())


def test_failed_execution_keeps_future_supplements(database):
    path, _now = database

    async def run():
        planned, steps = await seed(path)
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        await intents.mark_intent_finished(execution_id, ExecutionTaskStatus.FAILED, error="target calculation failed")
        assert group(path, planned.id)["status"] == "active"
        assert step(path, steps[-1].id)["status"] == "pending"
        context = events(path)[0]["context"]
        assert context["execution"]["status"] == "FAILED"
        assert not context["execution"]["supplements"][0]["is_last"]

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["all", "partial", "error"])
def test_session_closed_completion_cancels_only_its_group(database, scope):
    path, now = database

    async def run():
        planned, steps = await seed(path)
        next_group, next_steps = await seed(path, base=BASE + timedelta(minutes=15))
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        running(path, execution_id)
        await save_closed_record(execution_id, scope=scope)
        terminal = {
            "all": ExecutionTaskStatus.SKIPPED,
            "partial": ExecutionTaskStatus.PARTIAL,
            "error": ExecutionTaskStatus.FAILED,
        }[scope]
        await intents.mark_intent_finished(execution_id, terminal)
        await intents.mark_intent_finished(execution_id, terminal)
        assert group(path, planned.id)["status"] == ("cancelled" if scope == "all" else "active")
        assert step(path, steps[0].id)["status"] == "finished"
        assert step(path, steps[-1].id)["status"] == ("cancelled" if scope == "all" else "pending")
        assert group(path, next_group.id)["status"] == "active"
        assert [row["event_type"] for row in events(path)] == (
            ["execution.finished", "supplement.cancelled"] if scope == "all" else ["execution.finished"]
        )
        if scope == "all":
            await supplements.fire_step(steps[-1].id)
            now[0] = BASE + timedelta(minutes=15)
            await supplements.fire_step(next_steps[0].id)
            assert step(path, next_steps[0].id)["status"] == "queued"
            assert len(query(path, "SELECT * FROM execution_intent")) == 2

    asyncio.run(run())


def test_session_skip_preserves_manual_request_sharing_a_future_step(database):
    path, now = database

    async def run():
        planned, steps = await seed(path)
        await supplements.fire_step(steps[0].id)
        execution_id = step(path, steps[0].id)["execution_id"]
        running(path, execution_id)
        await save_closed_record(execution_id)
        manual = await intents.submit_intent(1, ExecutionKind.REBALANCE, "manual")
        now[0] = BASE + timedelta(minutes=1)
        await supplements.fire_step(steps[1].id)
        await intents.mark_intent_finished(execution_id, ExecutionTaskStatus.SKIPPED)
        assert group(path, planned.id)["status"] == "cancelled"
        assert step(path, steps[1].id)["status"] == "cancelled"
        assert (await intents.get_intent(manual.execution_id)).status == ExecutionTaskStatus.QUEUED
        assert await supplement_notifications.validate_execution_requests(manual.execution_id)

    asyncio.run(run())


@pytest.mark.parametrize("seconds, expected", [(30, [1, 2]), (90, [2])])
def test_recovery_skips_missed_steps_but_keeps_future_last(monkeypatch, database, seconds, expected):
    path, now = database

    async def run():
        planned, steps = await seed(path)
        execute(path, "UPDATE supplement_step SET status='finished' WHERE id=?", (steps[0].id,))
        now[0] = BASE + timedelta(seconds=seconds)
        scheduled = []
        monkeypatch.setattr(supplements, "schedule_step", lambda current: scheduled.append(current["id"]))
        await supplements.recover_supplements()
        assert scheduled == [steps[index].id for index in expected]
        assert group(path, planned.id)["status"] == "active"
        assert not events(path)
        now[0] = BASE + timedelta(minutes=3)
        scheduled.clear()
        await supplements.recover_supplements()
        assert not scheduled
        assert group(path, planned.id)["status"] == "cancelled"

    asyncio.run(run())


def test_cancel_races_with_trigger_without_starting_invalid_request(database):
    path, _now = database

    async def run():
        _planned, steps = await seed(path)
        await asyncio.gather(supplements.fire_step(steps[0].id), supplements.cancel_groups(1, "terminated"))
        assert not query(path, "SELECT * FROM execution_intent WHERE status='QUEUED'")
        assert len(events(path)) == 1

    asyncio.run(run())


def test_terminate_between_executions_uses_existing_route(database):
    from fastapi import HTTPException, Response

    from axile.server.api.routes import account_execution
    from axile.server.db.models.execution import ExecutionTerminateRequest

    path, _now = database

    async def run():
        await seed(path)
        response = Response()
        result = await account_execution.terminate_account_execution(1, ExecutionTerminateRequest(), response)
        assert response.status_code == 200
        assert result.execution_id is None
        assert result.status is None
        assert result.message == "已取消剩余补发"
        with pytest.raises(HTTPException) as caught:
            await account_execution.terminate_account_execution(1, ExecutionTerminateRequest(), Response())
        assert caught.value.status_code == 409

    asyncio.run(run())


def test_outbox_silent_success_failure_and_unknown_are_not_retried(monkeypatch, database):
    from axile.common.notification_function import NotificationFunctionResult

    path, _now = database
    execute(
        path,
        "UPDATE account_settings SET notification=json_set(notification,'$.execution_notification_code',?)",
        ("def notify(context):\n    pass",),
    )
    with closing(oracle_connect(path)) as connection, connection:
        for index, status in enumerate(["pending", "pending", "calling"]):
            connection.execute(
                "INSERT INTO notification_event(id,account_id,created_at_us,event_type,status,context) VALUES (?,1,?,?,?,?)",
                (
                    f"test:{index}",
                    to_us(BASE.isoformat()),
                    "supplement.cancelled",
                    status,
                    json.dumps({"execution": None}),
                ),
            )
    calls = []

    def invoke(_code, context, **_kwargs):
        calls.append(context)
        return NotificationFunctionResult(len(calls) == 1, None if len(calls) == 1 else "test failure")

    monkeypatch.setattr(notification_outbox, "run_notification_function", invoke)

    async def run():
        await notification_outbox.recover_notifications()
        await notification_outbox._dispatch(1)
        await notification_outbox._dispatch(1)
        assert len(calls) == 2
        assert [row["status"] for row in events(path)] == ["succeeded", "failed", "unknown"]

    asyncio.run(run())


def test_stale_base_callback_cannot_use_changed_cron(database):
    path, _now = database
    execute(path, "UPDATE account_settings SET schedule=json_set(schedule,'$.cron_expr','45 9 * * *')")
    asyncio.run(supplements.create_group(1, BASE))
    assert not query(path, "SELECT * FROM supplement_group")


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
