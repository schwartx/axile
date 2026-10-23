"""真实原生摘要事务遵守冻结证据与严格身份契约。"""

from copy import deepcopy

import pytest

from axile.server._native import _core
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


def _reference_content(record_id, projection=2):
    return {REFERENCE_KEY: {"version": 1, "view": "result", "record_id": record_id, "projection_version": projection}}


@pytest.mark.parametrize("memory,kept_memory", [(None, {}), ({"message": "native observation"}, {"memory": None})])
def test_summary_hydration_only_reads_selected_symbols_and_keeps_unique_evidence(native_database, memory, kept_memory):
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
    assert compact[REFERENCE_KEY]["record_id"] == record["id"]
    assert compact["symbol_results"]["A"] == {"sizing": {"target_quantity": 3}, **kept_memory}
    restored = _resolve_summary(native_database, artifact)
    restored.pop(REFERENCE_KEY)
    canonical = read_result(native_database, record)["symbol_results"]["A"]
    assert canonical["availability"] == {
        "target_volume": "absent",
        "final_volume": "absent",
    }
    # 既有品种事实视图省略空 memory；摘要中的独有空值仍保留。
    assert ("memory" in canonical) is (memory is not None)
    assert restored == {
        **content,
        "symbol_results": {"A": {**canonical, "sizing": {"target_quantity": 3}, **kept_memory}},
    }
    assert content == original
    assert set(restored["symbol_results"]) == {"A"}
    restored["symbol_results"]["A"]["orders"].append("preview")
    assert read_result(native_database, record)["symbol_results"]["A"]["orders"] == []
    assert _resolve_summary(native_database, artifact)["symbol_results"]["A"]["orders"] == []
    assert rows(native_database, "execution_artifact")[0]["content"] == compact


@pytest.mark.parametrize("record_id", [True, 0, -1, "1", 1.0, None])
def test_summary_identity_keeps_integer_type_and_positive_range(native_database, record_id):
    persist_result(native_database, {"symbol_results": {}}, execution_id=_EXECUTION)
    artifact = {
        "execution_id": _EXECUTION,
        "artifact_type": "execution_summary",
        "content": _reference_content(record_id),
    }
    with pytest.raises(_core.NativeError, match="终态记录身份") as error:
        _resolve_summary(native_database, artifact)
    assert error.value.args[0] == "BUSINESS_FACTS_REFERENCE"


@pytest.mark.parametrize("projection", [True, "2", 2.0, None, 1])
def test_summary_projection_version_is_not_coerced(native_database, projection):
    record = persist_result(native_database, {"symbol_results": {}}, execution_id=_EXECUTION)
    artifact = {
        "execution_id": _EXECUTION,
        "artifact_type": "execution_summary",
        "content": _reference_content(record["id"], projection),
    }
    with pytest.raises(_core.NativeError, match="业务视图版本无效") as error:
        _resolve_summary(native_database, artifact)
    assert error.value.args[0] == "BUSINESS_FACTS_REFERENCE"


def test_summary_missing_fact_fails_without_mutating_frozen_content(native_database):
    _, artifact = _freeze_summary(native_database, {"symbol_results": {"A": {"orders": []}}}, {"symbol_results": {}})
    original = deepcopy(artifact)
    with pytest.raises(_core.NativeError, match="品种事实缺失") as error:
        _resolve_summary(native_database, artifact)
    assert error.value.args[0] == "BUSINESS_FACTS_REFERENCE"
    assert artifact == original
    assert rows(native_database, "execution_artifact")[0] == original
