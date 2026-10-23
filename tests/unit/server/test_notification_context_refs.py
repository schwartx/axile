"""冻结 outbox 引用仅替换重复业务子树，并能恢复原捕获线格式。"""

import json
from copy import deepcopy

import pytest

from axile.server.business_facts.notification_context import compact_captured_context, hydrate_captured_context


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


def test_reference_roundtrip_preserves_capture_values_presence_and_order() -> None:
    captured = _capture()
    event = _event(captured)
    reference = compact_captured_context(captured, event, event_id="execution:execution-facts")

    assert reference is not None
    assert not {"positions", "orders", "trades", "assets", "targets", "default_feishu_variables"} & set(reference)
    assert "supplements" not in reference["execution_metadata"]
    restored = hydrate_captured_context(reference, event_id="execution:execution-facts", event_context=event)
    assert json.dumps(restored, ensure_ascii=False) == json.dumps(captured, ensure_ascii=False)
    restored["orders"][0]["order_id"] = "edited-preview"
    assert event["orders"][0]["order_id"] == "original"


@pytest.mark.parametrize("key", ["positions", "orders", "trades", "assets", "targets", "default_feishu_variables"])
def test_different_business_subtree_keeps_unique_capture(key) -> None:
    captured = _capture()
    event = _event(captured)
    event[key] = {"different": True}

    assert compact_captured_context(captured, event, event_id="execution:execution-facts") is None


@pytest.mark.parametrize("execution", [None, {}])
def test_reference_preserves_execution_null_and_empty(execution) -> None:
    captured = _capture()
    captured["execution"] = execution
    event = _event(_capture())
    reference = compact_captured_context(captured, event, event_id="execution:execution-facts")

    assert reference is not None
    assert hydrate_captured_context(reference, event_id="execution:execution-facts", event_context=event) == captured


def test_reference_preserves_missing_execution_and_distinguishes_numeric_types() -> None:
    captured = _capture()
    captured.pop("execution")
    event = _event(_capture())
    reference = compact_captured_context(captured, event, event_id="execution:execution-facts")

    assert reference is not None
    assert "execution" not in hydrate_captured_context(
        reference, event_id="execution:execution-facts", event_context=event
    )
    event["assets"]["total_assets"] = 0
    assert compact_captured_context(captured, event, event_id="execution:execution-facts") is None


def test_metadata_cannot_hide_nested_business_data() -> None:
    captured = _capture()
    captured["execution"]["error"] = {"orders": captured["orders"]}

    assert compact_captured_context(captured, _event(captured), event_id="execution:execution-facts") is None


def test_changed_event_or_metadata_reference_is_rejected() -> None:
    captured = _capture()
    event = _event(captured)
    reference = compact_captured_context(captured, event, event_id="execution:execution-facts")
    assert reference is not None
    changed = deepcopy(event)
    changed["execution"]["notified_at"] = "2026-10-20T10:00:00"
    with pytest.raises(ValueError, match="原通知已变化"):
        hydrate_captured_context(reference, event_id="execution:execution-facts", event_context=changed)
    reference["root_metadata"] = {"orders": []}
    with pytest.raises(ValueError, match="非元数据覆盖"):
        hydrate_captured_context(reference, event_id="execution:execution-facts", event_context=event)


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_reference_version_keeps_exact_integer_identity(version) -> None:
    captured = _capture()
    event = _event(captured)
    reference = compact_captured_context(captured, event, event_id="execution:execution-facts")
    assert reference is not None
    reference["$axile_notification_context"] = version
    with pytest.raises(ValueError, match="引用无效"):
        hydrate_captured_context(reference, event_id="execution:execution-facts", event_context=event)


def test_duplicate_field_order_and_nested_supplement_overrides_are_rejected() -> None:
    captured = _capture()
    event = _event(captured)
    reference = compact_captured_context(captured, event, event_id="execution:execution-facts")
    assert reference is not None
    reference["root_order"][0] = reference["root_order"][1]
    with pytest.raises(ValueError, match="字段顺序无效"):
        hydrate_captured_context(reference, event_id="execution:execution-facts", event_context=event)
    captured["execution"]["supplements"] = [{"index": 1, "end_reason": {"orders": []}}]
    assert compact_captured_context(captured, event, event_id="execution:execution-facts") is None
