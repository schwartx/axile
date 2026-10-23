"""账户控制原生阶段的原子性、幂等性和读取口径。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlCounterDeltaWrite,
    AccountControlEventWrite,
)
from axile.server._native import _core
from axile.server.account_control import providers
from axile.server.account_control import store as store_module
from axile.server.account_control.store import AccountControlStore
from axile.server.native_store import native_operation_sync


@pytest.fixture
def control_database(monkeypatch, tmp_path):
    """从留存 schema 提取控制表，使用文件库跨 FFI 验证。"""
    path = tmp_path / "controls.sqlite"
    schema = (Path(__file__).parents[2] / "fixtures/rust_store/v0.22.0.sql").read_text()
    with sqlite3.connect(path) as connection:
        for table in (
            "portfolio",
            "account",
            "account_control_counter_delta",
            "account_control_event",
            "account_control_session",
            "notification_event",
        ):
            start = schema.index(f"CREATE TABLE {table} (")
            connection.executescript(schema[start : schema.index(";", start) + 1])
        connection.execute(
            "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
            "VALUES (1,'测试账户','期货','ctp','ctp','2026-03-22','2026-03-22')"
        )
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    for name in ("_guards", "_final", "_updates"):
        monkeypatch.setattr(providers, name, {})
    return path


def _delta(uid="delta-1", **changes):
    return AccountControlCounterDeltaWrite.model_validate(
        {
            "account_id": 1,
            "execution_id": "exec-1",
            "context_kind": "execution",
            "source": "algorithm",
            "control_date": "2026-03-22",
            "bucket_type": "day",
            "bucket_start": "2026-03-22T00:00:00",
            "scope_type": "account",
            "operation": "place_order",
            "delta_count": 2,
            "delta_uid": uid,
            **changes,
        }
    )


def _event(uid="event-1", **changes):
    return AccountControlEventWrite.model_validate(
        {
            "account_id": 1,
            "execution_id": "exec-1",
            "context_kind": "execution",
            "source": "algorithm",
            "control_date": "2026-03-22",
            "seq": 1,
            "channel": "ctp",
            "operation": "place_order",
            "symbol": "rb2610",
            "decision": "allowed",
            "counted": True,
            "outcome": "submitted",
            "metadata": {"note": "原始事实"},
            "event_uid": uid,
            "created_at": "2026-03-22T10:00:00",
            "occurred_at_ms": 1000,
            **changes,
        }
    )


async def _register(store, **changes):
    await store.register_session(
        **{
            "instance_id": "guard-1",
            "account_id": 1,
            "execution_id": "exec-1",
            "context_kind": "execution",
            "source": "algorithm",
            "timezone": "Asia/Shanghai",
            "started_at_ms": 1000,
            **changes,
        }
    )


@pytest.mark.parametrize("close_session", [None, {"instance_id": "guard-1", "finished_at_ms": 2000}])
def test_empty_flush_skips_native_only_without_session_close(monkeypatch, close_session):
    calls = []

    async def operation(name, request):
        calls.append((name, request))
        return {"warning_error": None}

    monkeypatch.setattr(store_module, "native_operation", operation)
    asyncio.run(
        AccountControlStore().flush_execution_records(counter_deltas=[], events=[], close_session=close_session)
    )

    if close_session is None:
        assert calls == []
    else:
        assert len(calls) == 1
        name, request = calls[0]
        assert name == "control_flush_records"
        assert request["counter_deltas"] == request["events"] == []
        assert request["close_session"] == close_session


def test_baseline_aggregates_scopes_and_recent_allowed_history(control_database):
    async def scenario():
        store = AccountControlStore()
        await store.flush_execution_records(
            counter_deltas=[
                _delta(),
                _delta("delta-2", delta_count=3),
                _delta("symbol", scope_type="symbol", symbol="rb2610", delta_count=5),
                _delta("minute", bucket_type="minute", bucket_start="2026-03-22T10:00:00", delta_count=4),
                _delta("other-day", control_date="2026-03-21", delta_count=99),
            ],
            events=[
                _event(),
                _event("past", control_date="2026-03-21", occurred_at_ms=2000),
                _event("blocked", decision="blocked", counted=False, occurred_at_ms=9000),
                _event("uncounted", counted=False, occurred_at_ms=10000),
            ],
        )
        baseline, recent, incomplete = await store.load_baseline(1, "2026-03-22", "Asia/Shanghai")
        assert not incomplete
        assert (
            baseline.get_count(
                bucket_type=AccountControlBucketType.DAY, bucket_start="2026-03-22T00:00:00", operation="place_order"
            )
            == 5
        )
        assert (
            baseline.get_count(
                bucket_type=AccountControlBucketType.DAY,
                bucket_start="2026-03-22T00:00:00",
                operation="place_order",
                symbol="rb2610",
            )
            == 5
        )
        assert (
            baseline.get_count(
                bucket_type=AccountControlBucketType.MINUTE, bucket_start="2026-03-22T10:00:00", operation="place_order"
            )
            == 4
        )
        assert recent.get_last_allowed_at_ms("place_order") == 2000
        assert recent.get_last_allowed_at_ms("place_order", symbol="rb2610") == 2000
        assert recent.get_last_allowed_at_ms("place_order", symbol="ag2612") is None
        with sqlite3.connect(control_database) as connection:
            assert set(connection.execute("SELECT bucket_type,scope_type FROM account_control_counter_delta")) == {
                ("DAY", "ACCOUNT"),
                ("DAY", "SYMBOL"),
                ("MINUTE", "ACCOUNT"),
            }
            assert set(connection.execute("SELECT decision FROM account_control_event")) == {("ALLOWED",), ("BLOCKED",)}

    asyncio.run(scenario())


def test_flush_rolls_back_all_facts_and_close_then_retries_idempotently(control_database):
    async def scenario():
        store = AccountControlStore()
        await _register(store)
        with sqlite3.connect(control_database) as connection:
            connection.executescript(
                "CREATE TRIGGER reject_event BEFORE INSERT ON account_control_event "
                "WHEN NEW.event_uid='rejected' BEGIN SELECT RAISE(ABORT,'forced event failure'); END;"
            )
        deltas, events = [_delta()], [_event(), _event("rejected", seq=2)]
        close = {"instance_id": "guard-1", "finished_at_ms": 2000}
        with pytest.raises(Exception, match="forced event failure"):
            await store.flush_execution_records(counter_deltas=deltas, events=events, close_session=close)
        with sqlite3.connect(control_database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM account_control_counter_delta").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM account_control_event").fetchone() == (0,)
            assert connection.execute("SELECT status,finished_at_ms FROM account_control_session").fetchone() == (
                "active",
                None,
            )
            connection.execute("DROP TRIGGER reject_event")
        for _ in range(2):
            await store.flush_execution_records(counter_deltas=deltas, events=events, close_session=close)
        with sqlite3.connect(control_database) as connection:
            assert connection.execute(
                "SELECT COUNT(*),SUM(delta_count) FROM account_control_counter_delta"
            ).fetchone() == (1, 2)
            assert connection.execute("SELECT COUNT(*) FROM account_control_event").fetchone() == (2,)
            assert connection.execute("SELECT status,finished_at_ms FROM account_control_session").fetchone() == (
                "closed",
                2000,
            )
            raw = connection.execute("SELECT metadata FROM account_control_event LIMIT 1").fetchone()[0]
            assert json.loads(raw) == {"note": "原始事实"}

    asyncio.run(scenario())


def test_warning_failure_does_not_rollback_committed_control_facts(control_database):
    async def scenario():
        store = AccountControlStore()
        await _register(store)
        warning = {
            "account_id": 1,
            "execution_id": "exec-1",
            "operation": "place_order",
            "control_date": "2026-03-22",
            "timezone": "Asia/Shanghai",
            "occurred_at_ms": 1000,
            "used": 8,
            "limit": 10,
            "warning_percent": 80,
            "threshold_count": 8,
            "remaining": 2,
            "source": "axile_local_allowed",
        }
        with sqlite3.connect(control_database) as connection:
            connection.executescript(
                "CREATE TRIGGER reject_warning BEFORE INSERT ON notification_event "
                "BEGIN SELECT RAISE(ABORT,'forced warning failure'); END;"
            )
        await store.flush_execution_records(
            counter_deltas=[_delta()],
            events=[_event(metadata={"quota_warning": warning})],
            close_session={"instance_id": "guard-1", "finished_at_ms": 2000},
        )
        with sqlite3.connect(control_database) as connection:
            assert connection.execute("SELECT SUM(delta_count) FROM account_control_counter_delta").fetchone() == (2,)
            assert connection.execute("SELECT COUNT(*) FROM account_control_event").fetchone() == (1,)
            assert connection.execute("SELECT status FROM account_control_session").fetchone() == ("closed",)
            assert connection.execute("SELECT COUNT(*) FROM notification_event").fetchone() == (0,)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("context_kind", "source", "execution_id", "status", "active", "finished", "expected"),
    [
        ("execution", "algorithm", "exec-1", "active", None, None, True),
        ("execution", "algorithm", "exec-1", "active", "guard-1", None, False),
        ("execution", "algorithm", "exec-1", "incomplete", "guard-1", None, True),
        ("execution", "algorithm", "exec-1", "active", None, 999, False),
        ("execution", "algorithm", "exec-1", "active", None, 1000, True),
        ("account_operation", "instrument_catalog", "catalog-x", "active", None, None, False),
        ("account_operation", "account_assets", "assets-x", "active", None, None, False),
        ("account_operation", "connection_prepare", "prepare-x", "active", None, None, False),
        ("account_operation", "manual_cancel", "orders-x", "active", None, None, True),
        ("unknown", "instrument_catalog", "catalog-" + "a" * 32, "active", None, None, False),
        ("unknown", "instrument_catalog", "catalog-" + "A" * 32, "active", None, None, True),
    ],
)
def test_incomplete_history_preserves_window_and_risk_exclusions(
    control_database, context_kind, source, execution_id, status, active, finished, expected
):
    async def scenario():
        await _register(AccountControlStore(), context_kind=context_kind, source=source, execution_id=execution_id)
        with sqlite3.connect(control_database) as connection:
            connection.execute("UPDATE account_control_session SET status=?,finished_at_ms=?", (status, finished))
        raw = native_operation_sync(
            "control_load_baseline",
            {
                "account_id": 1,
                "control_date": "2026-03-22",
                "start_ms": 1000,
                "end_ms": 2000,
                "active_instance_id": active,
            },
        )
        assert raw["incomplete"] is expected

    asyncio.run(scenario())


def test_outcome_updates_are_atomic_and_account_scoped(control_database):
    async def scenario():
        store = AccountControlStore()
        await store.flush_execution_records(counter_deltas=[], events=[_event()])
        request = {"events": [{"account_id": 2, "event_uid": "event-1", "outcome": "broker_rejected", "metadata": {}}]}
        assert native_operation_sync("control_update_event_outcomes", request)["updated_uids"] == []
        request["events"][0]["account_id"] = 1
        assert native_operation_sync("control_update_event_outcomes", request)["updated_uids"] == ["event-1"]
        with pytest.raises(Exception, match="metadata"):
            await store.update_event_outcomes(
                [
                    {"event_uid": "event-1", "outcome": "wrong", "metadata": {}},
                    {"event_uid": "event-1", "outcome": "invalid", "metadata": None},
                ]
            )
        with sqlite3.connect(control_database) as connection:
            assert connection.execute("SELECT outcome FROM account_control_event").fetchone() == ("broker_rejected",)

    asyncio.run(scenario())


def test_missing_database_is_not_created(monkeypatch, tmp_path):
    path = tmp_path / "missing.sqlite"
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    for name, request in (
        ("control_load_baseline", {}),
        ("control_register_session", {}),
        ("control_flush_records", {}),
    ):
        assert callable(getattr(_core, name))
        with pytest.raises(Exception):
            native_operation_sync(name, request)
        assert not path.exists()


def test_load_baseline_keeps_dst_window_in_python_and_uses_one_native_call(monkeypatch):
    calls = []

    async def operation(name, request):
        calls.append((name, request))
        return {"counters": [], "recent_allowed": [], "incomplete": True}

    monkeypatch.setattr(store_module, "native_operation", operation)
    baseline, recent, incomplete = asyncio.run(
        AccountControlStore().load_baseline(7, "2026-03-08", "America/New_York", active_instance_id="current")
    )
    assert not baseline.account_counts and not recent.account_timestamps and incomplete
    assert len(calls) == 1
    name, request = calls[0]
    assert name == "control_load_baseline" and request["active_instance_id"] == "current"
    assert request["end_ms"] - request["start_ms"] == 23 * 3600 * 1000
