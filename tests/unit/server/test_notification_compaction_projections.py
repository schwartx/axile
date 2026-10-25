"""完整通知快照不得丢失已冻结的分析与活动列表投影。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server._native import _core
from axile.server.db.models.schedule import ActivityExecutionRecord
from tests.conftest import native_store_call
from tests.fixtures.rust_store.supplement_rows import SupplementGroup, SupplementStep
from tests.unit.server._business_facts_test_support import persist_result, rows, sql
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import read_execution_result
from tests.unit.server.test_evidence_export import _seed as seed_rows

_EXECUTION = "notification-compaction-projections"
_CREATED = "2026-10-20T09:00:10+08:00"


def _result():
    return {
        "status": "PARTIAL",
        "success": False,
        "execution_time": 0.0,
        "account_assets": {
            "available_cash": 0.0,
            "total_asset": 1000.0,
            "market_value": None,
            "source": "real",
            "positions": [{"symbol": "A", "volume": 2, "available_volume": 0}],
        },
        "symbol_results": {
            "A": {
                "status": "PARTIAL",
                "first_tick": {"last_price": 100.0, "bid_price": 100.0, "ask_price": 100.0},
                "sizing": {"current_quantity": 0, "target_quantity": 3, "unit_multiplier": 10},
                "orders": [{"order_id": "o1", "direction": "BUY", "filled_volume": 2}],
                "trades": [
                    {
                        "trade_id": "t1",
                        "order_id": "o1",
                        "trade_price": 101.0,
                        "trade_volume": 2,
                        "trade_time": _CREATED,
                        "extra": {"commission": 0, "commission_asset": "CNY"},
                    }
                ],
            }
        },
        "extra": {
            "server_notification_context": {
                "account": {"id": 1, "name": "frozen-name"},
                "execution": {"status": "PARTIAL", "execution_time": 0.0},
                "orders": [{"order_id": "o1", "filled_volume": 2}],
                "assets": {"total_assets": None, "available_cash": 0.0},
            },
            "plugin_diagnostic": "unchanged native evidence",
        },
    }


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    seed_rows(native_database, build_account(portfolio_id=None))
    return native_database


def _seed(path):
    record = persist_result(
        path, _result(), execution_id=_EXECUTION, created_at=_CREATED, raw_input={"curr_target": {"A": 0.25}}
    )
    seed_rows(
        path,
        (
            "execution_intent",
            dict(
                account_id=1,
                execution_id=_EXECUTION,
                kind=ExecutionKind.REBALANCE,
                trigger_source="manual",
                status=ExecutionTaskStatus.RUNNING,
                channel="ctp",
                started_at=_CREATED,
            ),
        ),
        SupplementGroup(
            id="notification-compaction-group",
            account_id=1,
            fingerprint="notification-compaction-fingerprint",
            base_scheduled_at=_CREATED,
            expires_at="2026-10-20T23:00:00+08:00",
            configured_count=1,
            effective_count=1,
        ),
        SupplementStep(
            group_id="notification-compaction-group",
            index=1,
            scheduled_at=_CREATED,
            status="running",
            execution_id=_EXECUTION,
        ),
    )
    return record


def _projection_rows(path, record_id):
    return {
        name: [row for row in rows(path, name) if row["record_id"] == record_id]
        for name in ("execution_symbol_result", "execution_symbol_sizing", "business_trade")
    }


def _activity(path):
    page = json.loads(_core.journal(_core.Store(str(path)), 1, 0, 100))
    return ActivityExecutionRecord.model_validate(
        next(row["record"] for row in page["data"] if row["kind"] == "execution")
    )


def _extra(path):
    return rows(path, "execution_extension")[0]["extra"]


def _finish(path):
    return native_store_call(
        path,
        "intent_finish",
        {"execution_id": _EXECUTION, "status": "PARTIAL", "finished_at": _CREATED, "now": _CREATED},
    )


def _event():
    context = deepcopy(_result()["extra"]["server_notification_context"])
    context.update(event_id=f"execution:{_EXECUTION}", event_type="execution.finished")
    context["execution"].update(id=_EXECUTION, notified_at=_CREATED)
    return (
        "notification_event",
        dict(id=f"execution:{_EXECUTION}", account_id=1, event_type="execution.finished", context=context),
    )


def test_full_context_preserves_projections_and_activity_without_requested_analysis(database):
    """真实终态事务完整保留通知，成交数和成本仍立即可读。"""
    record = _seed(database)
    before = _projection_rows(database, record["id"])
    result_before = asyncio.run(read_execution_result(record))
    assert all(before.values())
    activity = _activity(database)
    assert activity.trade_count == 1
    assert activity.summary is not None and activity.summary["cost"] is not None
    _finish(database)
    assert _projection_rows(database, record["id"]) == before
    assert _extra(database)["server_notification_context"] == _result()["extra"]["server_notification_context"]
    assert not _finish(database)["changed"]
    state = rows(database, "account_analysis")[0]
    assert state["current_snapshot"] is None and not state["requested"]
    after_activity = _activity(database)
    assert after_activity.trade_count == activity.trade_count
    assert after_activity.summary == activity.summary
    assert _projection_rows(database, record["id"]) == before
    assert asyncio.run(read_execution_result(record)) == result_before


def test_real_extension_evidence_change_still_invalidates_projections(database):
    """真实扩展变化仍增加来源版本，并保持事实可用。"""
    record = _seed(database)
    _finish(database)
    assert _extra(database)["server_notification_context"] == _result()["extra"]["server_notification_context"]
    assert all(_projection_rows(database, record["id"]).values())
    version = rows(database, "account_analysis")[0]["source_version"]
    sql(
        database,
        "UPDATE execution_extension SET extra=? WHERE record_id=?",
        (json.dumps({**_extra(database), "plugin_diagnostic": "changed native evidence"}), record["id"]),
    )
    assert all(_projection_rows(database, record["id"]).values())
    assert rows(database, "account_analysis")[0]["source_version"] > version


def test_distinct_business_context_is_not_compacted_or_reprojected(database):
    """outbox 的业务差异保留原始正文与投影。"""
    record = _seed(database)
    before = _projection_rows(database, record["id"])
    result_before = asyncio.run(read_execution_result(record))
    event = _event()
    event[1]["context"]["orders"][0]["filled_volume"] = 1
    seed_rows(database, event)
    version = rows(database, "account_analysis")[0]["source_version"]
    _finish(database)
    assert "$axile_notification_context" not in _extra(database)["server_notification_context"]
    assert _projection_rows(database, record["id"]) == before
    assert asyncio.run(read_execution_result(record)) == result_before
    assert rows(database, "account_analysis")[0]["source_version"] == version


def test_notification_rolls_back_with_outbox(database):
    """原生终态事务失败时回滚 outbox、步骤和业务投影。"""
    record = _seed(database)
    before = _projection_rows(database, record["id"])
    result_before = asyncio.run(read_execution_result(record))
    state_before = rows(database, "account_analysis")[0]
    sql(
        database,
        """CREATE TRIGGER reject_notification AFTER INSERT ON notification_event
        BEGIN SELECT RAISE(ABORT,'synthetic outer transaction failure'); END""",
    )
    with pytest.raises(_core.NativeError, match="synthetic outer transaction failure"):
        _finish(database)
    assert rows(database, "notification_event") == []
    assert _projection_rows(database, record["id"]) == before
    assert asyncio.run(read_execution_result(record)) == result_before
    assert rows(database, "account_analysis")[0] == state_before
    assert rows(database, "supplement_step")[0]["status"] == "running"
    assert rows(database, "supplement_group")[0]["status"] == "active"
    assert rows(database, "execution_intent")[0]["status"] == "RUNNING"


@pytest.mark.parametrize("existing_event", [False, True])
def test_supplement_finish_keeps_projections_in_real_caller_transaction(database, existing_event):
    """创建或复用通知的真实终态阶段保持完整投影。"""
    record = _seed(database)
    if existing_event:
        seed_rows(database, _event())
    before = _projection_rows(database, record["id"])
    _finish(database)
    assert rows(database, "supplement_step")[0]["status"] == "finished"
    assert rows(database, "supplement_group")[0]["status"] == "completed"
    assert _projection_rows(database, record["id"]) == before
    activity = _activity(database)
    assert activity.trade_count == 1
    assert activity.summary is not None and activity.summary["cost"] == 20.0
    assert _projection_rows(database, record["id"]) == before
