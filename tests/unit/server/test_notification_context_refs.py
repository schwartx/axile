"""冻结 outbox 引用仅替换重复业务子树，并能恢复原捕获线格式。"""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server._native import _core
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows, sql
from tests.unit.server.test_evidence_export import _seed as seed_rows

_EXECUTION = "execution-facts"
_EVENT = f"execution:{_EXECUTION}"
_NOW = "2026-10-20T09:00:01"


def _capture() -> dict:
    return {
        "execution": {"status": "PARTIAL", "error": "保留原文", "notified_at": "2026-10-20T09:00:00"},
        "event_type": "execution.finished",
        "assets": {"total_assets": 0.0, "market_value": None},
        "positions": [{"symbol": "rb2610", "volume": 0, "market_value": None}],
        "orders": [{"order_id": "original", "update_time": "2026-10-20T09:00:00.123456+08:00"}],
        "trades": [],
        "targets": {"current": {}, "previous": {"rb2610": 0.0}},
        "default_feishu_variables": {"positions": [{"symbol": "rb2610"}], "trades": []},
        "is_test": False,
    }


def _event(captured: dict) -> dict:
    event = deepcopy(captured)
    event["execution"].update(
        id="execution-facts", notified_at="2026-10-20T09:00:01", supplements=[{"index": 1, "is_last": True}]
    )
    event["event_id"] = "execution:execution-facts"
    return event


def _freeze_capture(path, captured, event):
    record = persist_result(
        path, {"extra": {"server_notification_context": captured}}, execution_id=_EXECUTION, created_at=_NOW
    )
    seed_rows(
        path,
        ExecutionIntent(
            account_id=1,
            execution_id=_EXECUTION,
            kind=ExecutionKind.REBALANCE,
            trigger_source="manual",
            status=ExecutionTaskStatus.RUNNING,
            channel="ctp",
            started_at=_NOW,
        ),
        SupplementGroup(
            id="context-refs-group",
            account_id=1,
            fingerprint="context-refs",
            base_scheduled_at=_NOW,
            expires_at="2026-10-20T23:00:00",
            configured_count=1,
            effective_count=1,
        ),
        SupplementStep(
            group_id="context-refs-group", index=1, scheduled_at=_NOW, status="running", execution_id=_EXECUTION
        ),
        NotificationEvent(id=_EVENT, account_id=1, event_type="execution.finished", context=event),
    )
    receipt = native_store_call(
        path, "intent_finish", {"execution_id": _EXECUTION, "status": "PARTIAL", "finished_at": _NOW, "now": _NOW}
    )
    assert receipt["changed"]
    return record, rows(path, "execution_extension")[0]["extra"]["server_notification_context"]


def _read_capture(path, record):
    return read_result(path, record)["extra"]["server_notification_context"]


def _replace_reference(path, record, reference):
    sql(
        path,
        "UPDATE execution_extension SET extra=? WHERE record_id=?",
        (json.dumps({"server_notification_context": reference}, ensure_ascii=False), record["id"]),
    )


def test_reference_roundtrip_preserves_capture_values_presence_and_order(native_database) -> None:
    captured = _capture()
    event = _event(captured)
    record, reference = _freeze_capture(native_database, captured, event)

    assert reference["$axile_notification_context"] == 1
    assert not {"positions", "orders", "trades", "assets", "targets", "default_feishu_variables"} & set(reference)
    assert "supplements" not in reference["execution_metadata"]
    restored = _read_capture(native_database, record)
    assert json.dumps(restored, ensure_ascii=False) == json.dumps(captured, ensure_ascii=False)
    restored["orders"][0]["order_id"] = "edited-preview"
    preview = native_store_call(native_database, "notifications_preview", {"account_id": 1})
    assert preview["context"] == event
    assert event["orders"][0]["order_id"] == "original"
    assert _read_capture(native_database, record) == captured


@pytest.mark.parametrize("key", ["positions", "orders", "trades", "assets", "targets", "default_feishu_variables"])
def test_different_business_subtree_keeps_unique_capture(native_database, key) -> None:
    captured = _capture()
    event = _event(captured)
    event[key] = {"different": True}

    record, stored = _freeze_capture(native_database, captured, event)
    assert stored == captured
    assert "$axile_notification_context" not in stored
    assert _read_capture(native_database, record) == captured


@pytest.mark.parametrize("execution", [None, {}])
def test_reference_preserves_execution_null_and_empty(native_database, execution) -> None:
    captured = _capture()
    captured["execution"] = execution
    event = _event(_capture())
    record, reference = _freeze_capture(native_database, captured, event)

    assert reference["$axile_notification_context"] == 1
    restored = _read_capture(native_database, record)
    assert restored == captured
    assert type(restored["execution"]) is type(execution)


@pytest.mark.parametrize("total_assets", [0.0, 0])
def test_reference_preserves_missing_execution_and_distinguishes_numeric_types(native_database, total_assets) -> None:
    captured = _capture()
    captured.pop("execution")
    event = _event(_capture())
    event["assets"]["total_assets"] = total_assets
    record, stored = _freeze_capture(native_database, captured, event)

    assert ("$axile_notification_context" in stored) is (type(total_assets) is float)
    assert "execution" not in _read_capture(native_database, record)
    assert _read_capture(native_database, record) == captured


def test_metadata_cannot_hide_nested_business_data(native_database) -> None:
    captured = _capture()
    captured["execution"]["error"] = {"orders": captured["orders"]}

    record, stored = _freeze_capture(native_database, captured, _event(captured))
    assert "$axile_notification_context" not in stored
    assert stored == _read_capture(native_database, record) == captured


def test_changed_event_is_rejected(native_database) -> None:
    captured = _capture()
    event = _event(captured)
    record, reference = _freeze_capture(native_database, captured, event)
    assert reference["$axile_notification_context"] == 1
    changed = deepcopy(event)
    changed["execution"]["notified_at"] = "2026-10-20T10:00:00"
    with pytest.raises(sqlite3.IntegrityError, match="Frozen notification context"):
        sql(native_database, "UPDATE notification_event SET context=? WHERE id=?", (json.dumps(changed), _EVENT))
    # 坏样本绕过写保护后恢复触发器，再让真实原生读取校验哈希。
    _core.close_database(str(native_database))
    with closing(sqlite3.connect(native_database)) as connection, connection:
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            ("business_facts_0037_notification_outbox_update",),
        ).fetchone()[0]
        connection.execute("DROP TRIGGER business_facts_0037_notification_outbox_update")
        connection.execute("UPDATE notification_event SET context=? WHERE id=?", (json.dumps(changed), _EVENT))
        connection.execute(guard)
    with pytest.raises(_core.NativeError, match="notification identity or digest mismatch"):
        _read_capture(native_database, record)


def test_metadata_reference_cannot_override_business_data(native_database) -> None:
    captured = _capture()
    record, reference = _freeze_capture(native_database, captured, _event(captured))
    reference["root_metadata"] = {"orders": []}
    _replace_reference(native_database, record, reference)
    with pytest.raises(_core.NativeError, match="notification root metadata override"):
        _read_capture(native_database, record)


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_reference_version_keeps_exact_integer_identity(native_database, version) -> None:
    captured = _capture()
    event = _event(captured)
    record, reference = _freeze_capture(native_database, captured, event)
    reference["$axile_notification_context"] = version
    _replace_reference(native_database, record, reference)
    with pytest.raises(_core.NativeError, match="invalid notification reference"):
        _read_capture(native_database, record)


def test_duplicate_field_order_is_rejected(native_database) -> None:
    captured = _capture()
    event = _event(captured)
    record, reference = _freeze_capture(native_database, captured, event)
    reference["root_order"][0] = reference["root_order"][1]
    _replace_reference(native_database, record, reference)
    with pytest.raises(_core.NativeError, match="duplicate notification order key"):
        _read_capture(native_database, record)


def test_nested_supplement_metadata_cannot_hide_business_data(native_database) -> None:
    captured = _capture()
    event = _event(captured)
    captured["execution"]["supplements"] = [{"index": 1, "end_reason": {"orders": []}}]
    record, stored = _freeze_capture(native_database, captured, event)
    assert "$axile_notification_context" not in stored
    assert stored == _read_capture(native_database, record) == captured
