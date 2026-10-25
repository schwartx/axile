"""终态摘要保留原字段证据，并单独提供历史展示语义。"""

import pytest

from scripts.business_facts_contract import business_evidence
from tests.fixtures.rust_store.execution_rows import ExecuteRecord
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows


def _roundtrip(path, payload):
    record = persist_result(path, payload)
    return record, read_result(path, record)


@pytest.mark.parametrize(
    "termination",
    [
        None,
        "柜台原文",
        {},
        {"reason": None, "mode": "", "cancel_attempted": False, "cancel_failed_order_ids": []},
        {"cancel_failed_order_ids": ["", "duplicate", "duplicate", None, {"native": 1}], "unknown": {"n": 0}},
        {"cancel_failed_order_ids": {"legacy": True}, "finished_at": "2026-10-20 09:00:00.000000"},
    ],
)
def test_head_preserves_termination_occurrences_and_container_states(native_database, termination):
    _, restored = _roundtrip(native_database, {"termination": termination})
    assert business_evidence(restored) == business_evidence({"termination": termination})


def test_head_preserves_source_values_and_compact_display_separately(native_database):
    row, restored = _roundtrip(
        native_database, {"status": "PARTIAL", "error": None, "execution_time": 0, "success": False}
    )
    assert row["outcome"] is None
    assert business_evidence(row["display_outcome"]) == business_evidence("not_reached")
    assert business_evidence(row["task_status"]) == business_evidence("FAILED")
    assert business_evidence(restored) == business_evidence(
        {"status": "PARTIAL", "error": None, "execution_time": 0, "success": False}
    )
    assert business_evidence(restored["execution_time"]) == business_evidence(0)
    assert "outcome" not in restored
    assert business_evidence(restored["task_status"]) == business_evidence("FAILED")


def test_invalid_known_field_is_evidence_without_sql_coercion(native_database):
    payload = {"status": {"legacy": True}, "execution_time": "0", "success": 0, "outcome_reason": ""}
    row, restored = _roundtrip(native_database, payload)
    assert business_evidence((row["status"], row["execution_time"], row["success"])) == business_evidence(
        (None, None, None)
    )
    assert business_evidence(restored) == business_evidence(payload)


def test_terminal_model_does_not_store_or_rebuild_full_documents(native_database):
    row = persist_result(native_database, {})
    assert {"raw_input", "raw_result"}.isdisjoint(rows(native_database, "executerecord")[0])
    assert {"raw_input", "raw_result"}.isdisjoint(ExecuteRecord.model_fields)
    assert row["facts_complete"] is True
    record = ExecuteRecord(account_id=1, execution_id=None, is_success=0)
    assert not hasattr(record, "raw_input")
    assert not hasattr(record, "raw_result")
    assert not {"field_metadata", "termination_metadata"}.intersection(record.model_dump())
