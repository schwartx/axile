"""账户控制审计模型测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import (
    AccountControlEventWrite,
    AccountControlOverride,
)
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.server.db.models import AccountControlCounterDelta, AccountControlEvent
from tests.unit.executor._account_control_test_support import normalize_account_control_override


def _clock() -> datetime:
    return datetime(2026, 3, 22, 9, 31, 15)


def test_account_control_event_schema_supports_audit_drill_down_queries() -> None:
    """审计事件应使用显式列与读侧索引支撑下钻查询。"""
    event_columns = set(AccountControlEvent.__table__.columns.keys())
    delta_columns = set(AccountControlCounterDelta.__table__.columns.keys())
    index_columns = {
        index.name: tuple(column.name for column in index.columns) for index in AccountControlEvent.__table__.indexes
    }

    assert event_columns == {
        "account_id",
        "control_date",
        "execution_id",
        "context_kind",
        "source",
        "seq",
        "channel",
        "operation",
        "symbol",
        "metadata",
        "decision",
        "counted",
        "outcome",
        "event_uid",
        "created_at",
        "occurred_at_ms",
        "id",
    }
    assert delta_columns == {
        "account_id",
        "execution_id",
        "context_kind",
        "source",
        "control_date",
        "bucket_type",
        "bucket_start",
        "scope_type",
        "symbol",
        "operation",
        "delta_count",
        "delta_uid",
        "id",
    }

    assert {
        "account_id",
        "control_date",
        "execution_id",
        "symbol",
        "metadata",
        "occurred_at_ms",
    } <= event_columns

    assert index_columns["ix_account_control_event_account_date_created"] == (
        "account_id",
        "control_date",
        "created_at",
    )
    assert index_columns["ix_account_control_event_execution_seq"] == ("execution_id", "seq")
    assert index_columns["ix_account_control_event_account_date_symbol_created"] == (
        "account_id",
        "control_date",
        "symbol",
        "created_at",
    )
    assert index_columns["ix_account_control_event_account_operation_occurred"] == (
        "account_id",
        "operation",
        "occurred_at_ms",
    )
    assert index_columns["ix_account_control_event_account_symbol_operation_occurred"] == (
        "account_id",
        "symbol",
        "operation",
        "occurred_at_ms",
    )


def test_account_control_event_write_and_guard_emit_monotonic_seq() -> None:
    """写模型与防护层都应显式写出 execution 内递增 seq。"""
    event = AccountControlEventWrite.model_validate(
        {
            "account_id": 1,
            "control_date": "2026-03-22",
            "execution_id": "exec-seq",
            "channel": TradeChannel.CTP,
            "operation": "place_order",
            "symbol": "rb2610",
            "metadata": {"order_id": "oid-1"},
            "decision": "allowed",
            "counted": True,
            "outcome": "submitted",
            "event_uid": "event-seq-1",
            "seq": 1,
            "occurred_at_ms": 1_763_202_660_123,
        }
    )

    assert event.seq == 1
    assert event.occurred_at_ms == 1_763_202_660_123

    guard = AccountControlGuard(
        account_id=1,
        execution_id="exec-seq",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(normalize_account_control_override({"place_order": {"per_day": 1}})),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )

    attempt = guard.begin_operation("place_order", symbol="rb2610")
    attempt.record_outcome("submitted", metadata={"order_id": "oid-1"})

    with pytest.raises(AccountControlBlockedError):
        guard.begin_operation("place_order", symbol="rb2610")

    _, events = guard.flush_records()

    assert [item.seq for item in events] == [1, 2]
    assert events[0].metadata == {"order_id": "oid-1"}
    assert events[0].outcome == "submitted"
    assert events[1].outcome == "policy_blocked"
