"""真实原生摘要事务遵守冻结证据与严格身份契约。"""

from copy import deepcopy

import pytest

from axile.server.business_facts.summary_refs import REFERENCE_KEY
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result, read_result, rows

_EXECUTION = "summary-reference-contract"


def _freeze_summary(path, content, result):
    native_store_call(
        path,
        "append_execution_artifact",
        {
            "execution_id": _EXECUTION,
            "artifact_type": "execution_summary",
            "schema_version": 2,
            "content": content,
            "created_at": "2026-10-20",
        },
    )
    record = persist_result(path, result, execution_id=_EXECUTION)
    return record, rows(path, "execution_artifact")[0]


def _resolve_summary(path, artifact):
    return native_store_call(path, "resolve_execution_artifact", {"artifact": artifact, "account_id": 1})


@pytest.mark.parametrize("memory", [None, {"message": "native observation"}])
def test_full_summary_freezes_selected_symbols_and_unique_evidence(native_database, memory):
    result = {
        "symbol_results": {
            "A": {"orders": [], "memory": memory, "sizing": {"target_quantity": 0}},
            "unrelated": {"orders": [{"order_id": "unrelated"}]},
        }
    }
    content = {
        "symbol_results": {"A": {"orders": [], "memory": None, "sizing": {"target_quantity": 3}}},
        "independent": {"null": None, "zero": 0, "empty": "", "false": False},
    }
    original = deepcopy(content)
    record, artifact = _freeze_summary(native_database, content, result)
    compact = artifact["content"]
    assert REFERENCE_KEY not in compact
    restored = _resolve_summary(native_database, artifact)
    assert restored == compact
    assert restored == original
    assert content == original
    assert set(restored["symbol_results"]) == {"A"}
    restored["symbol_results"]["A"]["orders"].append("preview")
    assert read_result(native_database, record)["symbol_results"]["A"]["orders"] == []
    assert _resolve_summary(native_database, artifact)["symbol_results"]["A"]["orders"] == []
    assert rows(native_database, "execution_artifact")[0]["content"] == compact


def test_full_summary_does_not_depend_on_terminal_symbol_presence(native_database):
    _, artifact = _freeze_summary(native_database, {"symbol_results": {"A": {"orders": []}}}, {"symbol_results": {}})
    original = deepcopy(artifact)
    assert _resolve_summary(native_database, artifact) == original["content"]
    assert artifact == original
    assert rows(native_database, "execution_artifact")[0] == original
