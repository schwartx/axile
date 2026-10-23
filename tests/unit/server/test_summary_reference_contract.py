"""历史摘要 codec 与原生业务事务共同遵守的证据与身份契约。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from axile.domain.execution import ExecutionArtifactType
from axile.server._native import _core
from axile.server.business_facts.artifacts import parse_artifact_reference, resolve_artifact_content
from axile.server.business_facts.summary_refs import (
    REFERENCE_KEY,
    BusinessFactReferenceError,
    compact_summary_content,
    hydrate_summary_content,
)


def test_summary_hydration_only_reads_selected_symbols_and_keeps_unique_evidence():
    result = {
        "symbol_results": {
            "A": {"orders": [], "memory": None, "sizing": {"target_quantity": 0}},
            "unrelated": {"orders": [{"order_id": "unrelated"}]},
        }
    }
    content = {
        "symbol_results": {"A": {"orders": [], "memory": None, "sizing": {"target_quantity": 3}}},
        "independent": {"null": None, "zero": 0, "empty": "", "false": False},
    }
    original = deepcopy(content)
    compact = compact_summary_content(content, result, record_id=7)
    assert compact is not None
    assert compact["symbol_results"]["A"] == {"sizing": {"target_quantity": 3}}
    restored = hydrate_summary_content(compact, result, include_reference=False)
    assert restored == content == original
    assert set(restored["symbol_results"]) == {"A"}
    restored["symbol_results"]["A"]["orders"].append("preview")
    assert result["symbol_results"]["A"]["orders"] == []


@pytest.mark.parametrize("record_id", [True, 0, -1, "1", 1.0, None])
def test_summary_identity_keeps_integer_type_and_positive_range(record_id):
    content = {REFERENCE_KEY: {"version": 1, "view": "result", "record_id": record_id, "projection_version": 2}}
    with pytest.raises(BusinessFactReferenceError, match="终态记录身份"):
        parse_artifact_reference(content)


@pytest.mark.parametrize("projection", [True, "2", 2.0, None, 1])
def test_summary_projection_version_is_not_coerced(projection):
    content = {REFERENCE_KEY: {"version": 1, "view": "result", "record_id": 1, "projection_version": projection}}
    with pytest.raises(BusinessFactReferenceError, match="业务视图版本无效"):
        parse_artifact_reference(content)


def test_summary_missing_fact_fails_without_mutating_frozen_content():
    result = {"symbol_results": {"A": {"orders": []}}}
    compact = compact_summary_content({"symbol_results": {"A": {"orders": []}}}, result, record_id=7)
    original = deepcopy(compact)
    with pytest.raises(BusinessFactReferenceError, match="品种事实缺失"):
        hydrate_summary_content(compact, {"symbol_results": {}}, include_reference=False)
    assert compact == original


@pytest.mark.parametrize("as_object", [False, True])
def test_artifact_resolver_passes_one_native_request_with_explicit_owner(monkeypatch, as_object):
    content = {"evidence": {"null": None, "zero": 0, "huge": 2**100 + 1}}
    fields = {"execution_id": "frozen", "artifact_type": ExecutionArtifactType.EXECUTION_SUMMARY, "content": content}
    artifact = SimpleNamespace(**fields) if as_object else fields
    calls = []

    async def resolve(name, request):
        calls.append((name, request))
        return content

    monkeypatch.setattr("axile.server.native_store.native_operation", resolve)
    assert asyncio.run(resolve_artifact_content(artifact, account_id=7)) == content
    assert calls == [
        (
            "resolve_execution_artifact",
            {
                "artifact": {**fields, "artifact_type": "execution_summary"},
                "account_id": 7,
            },
        )
    ]
    assert calls[0][1]["artifact"]["content"]["evidence"]["huge"] == 2**100 + 1


def test_artifact_resolver_keeps_reference_error_type(monkeypatch):
    async def reject(name, request):
        raise _core.NativeError("BUSINESS_FACTS_REFERENCE", "审计业务事实引用与执行账户不一致")

    monkeypatch.setattr("axile.server.native_store.native_operation", reject)
    artifact = {"execution_id": "frozen", "artifact_type": "execution_summary", "content": {}}
    with pytest.raises(BusinessFactReferenceError, match="账户"):
        asyncio.run(resolve_artifact_content(artifact, account_id=8))
