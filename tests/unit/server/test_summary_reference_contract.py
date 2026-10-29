"""执行详情从唯一结果事实读取，不再保存独立的摘要附件。"""

import json
from copy import deepcopy

import pytest

from axile.server._native import _core
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows


@pytest.mark.parametrize("memory", [None, {"message": "native observation"}])
def test_result_read_preserves_symbol_evidence_and_does_not_mutate_facts(native_database, memory):
    result = {
        "symbol_results": {
            "A": {"orders": [], "memory": memory, "sizing": {"target_quantity": 0}},
            "unrelated": {"orders": [{"order_id": "unrelated"}]},
        },
        "independent": {"null": None, "zero": 0, "empty": "", "false": False},
    }
    original = deepcopy(result)
    record = persist_result(native_database, result, execution_id="result-contract")
    restored = read_result(native_database, record)
    expected = deepcopy(original["symbol_results"])
    for symbol in expected.values():
        symbol["availability"] = {"target_volume": "absent", "final_volume": "absent"}
        for order in symbol["orders"]:
            order["availability"] = {"order_id": "value"}
    assert restored["symbol_results"] == expected
    assert (
        rows(native_database, "execution_extension")[0]["audit_evidence"]["unrecognized_fields"]["independent"]
        == original["independent"]
    )
    restored["symbol_results"]["A"]["orders"].append("preview")
    assert read_result(native_database, record)["symbol_results"]["A"]["orders"] == []
    assert result == original
    assert rows(native_database, "execution_artifact") == []
    artifacts = json.loads(
        _core.list_execution_artifacts_json(
            str(native_database), json.dumps({"execution_id": "result-contract", "skip": 0, "limit": 500})
        )
    )
    assert artifacts == {"data": [], "count": 0}


def test_retired_summary_attachment_is_rejected_even_when_result_exists(native_database):
    persist_result(native_database, {"symbol_results": {}}, execution_id="result-contract")
    with pytest.raises(_core.NativeError, match="derived from facts"):
        native_store_call(
            native_database,
            "append_execution_artifact",
            {
                "execution_id": "result-contract",
                "artifact_type": "execution_summary",
                "schema_version": 2,
                "content": {"symbol_results": {"A": {"orders": []}}},
                "created_at": "2026-10-20",
            },
        )
    assert rows(native_database, "execution_artifact") == []
