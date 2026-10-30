"""原生控制审计的分组、分页、上下文和事件响应契约。"""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from axile.server import account_control_audit
from axile.server._native import _core
from tests.unit.server._runtime_db_support import to_us, utc_time
from tests.unit.server.test_account_control_monitor import database


def seed_event(path, **changes):
    event = {
        "account_id": 1,
        "control_date": "2026-03-22",
        "execution_id": "exec-newer",
        "context_kind": "execution",
        "source": "algorithm",
        "seq": 1,
        "channel": "ctp",
        "operation": "place_order",
        "symbol": "rb2610",
        "metadata": '{"order_id":"oid-1","null":null}',
        "decision": "ALLOWED",
        "counted": True,
        "outcome": "submitted",
        "event_uid": "event-1",
        "created_at": "2026-03-22T09:34:00",
        "occurred_at_ms": 1763226660123,
        **changes,
    }
    event["created_at_us"] = to_us(event.pop("created_at"))
    event["occurred_at_us"] = event.pop("occurred_at_ms") * 1_000
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            f"INSERT INTO account_control_event({','.join(event)}) VALUES ({','.join('?' for _ in event)})",
            list(event.values()),
        )


def seed_events(path):
    seed_event(
        path,
        event_uid="event-2",
        seq=2,
        operation="cancel_order",
        symbol="ag2612",
        outcome="downstream_error",
        created_at="2026-03-22T09:35:00",
    )
    seed_event(path)
    seed_event(
        path,
        event_uid="event-3",
        seq=3,
        decision="BLOCKED",
        counted=False,
        outcome="policy_blocked",
        created_at="2026-03-22T09:36:00",
    )
    seed_event(
        path,
        event_uid="event-old",
        execution_id="exec-older",
        operation="query_order",
        outcome="filled",
        created_at="2026-03-22T09:32:00",
    )
    seed_event(
        path,
        event_uid="other-day",
        execution_id="exec-other-date",
        control_date="2026-03-21",
        created_at="2026-03-21T09:32:00",
    )


def test_summary_groups_operation_decision_and_outcome(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, _, _):
            seed_events(path)
            rows = json.loads(
                await account_control_audit.query_account_control_audit_summary(account_id=1, control_date="2026-03-22")
            )
            assert [(row["operation"], row["decision"], row["outcome"], row["count"]) for row in rows] == [
                ("cancel_order", "allowed", "downstream_error", 1),
                ("place_order", "allowed", "submitted", 1),
                ("place_order", "blocked", "policy_blocked", 1),
                ("query_order", "allowed", "filled", 1),
            ]

    asyncio.run(scenario())


def test_executions_orders_recent_first_and_preserves_page_count(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, _, _):
            seed_events(path)
            page = json.loads(
                await account_control_audit.query_account_control_audit_executions(
                    account_id=1, control_date="2026-03-22", limit=1
                )
            )
            assert page == {
                "data": [
                    {
                        "execution_id": "exec-newer",
                        "event_count": 3,
                        "first_event_at": utc_time("2026-03-22T09:34:00"),
                        "last_event_at": utc_time("2026-03-22T09:36:00"),
                        "context_kind": "execution",
                        "sources": ["algorithm"],
                    }
                ],
                "count": 2,
            }
            page = json.loads(
                await account_control_audit.query_account_control_audit_executions(
                    account_id=1, control_date="2026-03-22", skip=1, limit=1
                )
            )
            assert page["data"][0]["execution_id"] == "exec-older" and page["count"] == 2

    asyncio.run(scenario())


def test_events_filter_and_order_by_seq_then_id(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, _, _):
            seed_events(path)
            seed_event(path, event_uid="same-seq", created_at="2026-03-22T09:31:00")
            page = json.loads(
                await account_control_audit.query_account_control_execution_events(
                    execution_id="exec-newer", symbol="rb2610", operation="place_order", limit=2
                )
            )
            assert page["count"] == 3
            assert [event["event_uid"] for event in page["data"]] == ["event-1", "same-seq"]
            event = page["data"][0]
            assert event["metadata"] == {"order_id": "oid-1", "null": None}
            assert event["decision"] == "allowed" and event["counted"] is True
            assert event["created_at"] == utc_time("2026-03-22T09:34:00")
            assert event["occurred_at_ms"] == 1763226660123

    asyncio.run(scenario())


def test_context_descriptions_keep_exact_legacy_ids_and_sort_sources(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, _, _):
            legacy = "catalog-" + "a" * 32
            seed_event(path, execution_id=legacy, context_kind="unknown", source="instrument_catalog")
            seed_event(
                path, event_uid="second", execution_id=legacy, context_kind="account_operation", source="account_assets"
            )
            seed_event(path, event_uid="invalid", execution_id="catalog-short", context_kind="unknown")
            page = json.loads(
                await account_control_audit.query_account_control_audit_executions(
                    account_id=1, control_date="2026-03-22"
                )
            )
            descriptions = {row["execution_id"]: row for row in page["data"]}
            assert descriptions[legacy]["context_kind"] == "account_operation"
            assert descriptions[legacy]["sources"] == ["account_assets", "instrument_catalog"]
            assert descriptions["catalog-short"]["context_kind"] == "unknown"

    asyncio.run(scenario())


def test_summary_keeps_min_interval_outcome_distinct_and_requires_account(monkeypatch):
    async def scenario():
        async with database(monkeypatch) as (path, _, _):
            seed_event(path, decision="BLOCKED", counted=False, outcome="policy_blocked_min_interval")
            rows = json.loads(
                await account_control_audit.query_account_control_audit_summary(account_id=1, control_date="2026-03-22")
            )
            assert rows == [
                {
                    "operation": "place_order",
                    "decision": "blocked",
                    "outcome": "policy_blocked_min_interval",
                    "count": 1,
                }
            ]
            with pytest.raises(_core.NativeError) as caught:
                await account_control_audit.query_account_control_audit_summary(
                    account_id=99, control_date="2026-03-22"
                )
            assert caught.value.args[0] == "ACCOUNT_NOT_FOUND"

    asyncio.run(scenario())


@pytest.mark.parametrize("skip,limit", [(-1, 100), (0, 501)])
def test_service_rejects_invalid_pagination_without_accessing_storage(monkeypatch, skip, limit):
    async def unexpected(*_args):
        raise AssertionError("invalid pagination reached native storage")

    monkeypatch.setattr(account_control_audit, "native_operation", unexpected)

    async def scenario():
        with pytest.raises(ValueError):
            await account_control_audit.query_account_control_audit_executions(
                account_id=1, control_date="2026-03-22", skip=skip, limit=limit
            )
        with pytest.raises(ValueError):
            await account_control_audit.query_account_control_execution_events(
                execution_id="exec", skip=skip, limit=limit
            )

    asyncio.run(scenario())
