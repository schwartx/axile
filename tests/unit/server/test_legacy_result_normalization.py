"""旧执行记录读时归一的契约测试：只补明确结论，不改写历史，不透传技术原文。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from axile.executor.session_closed import COMMON_SESSION_CLOSED
from axile.server.execution.legacy_compat import normalize_legacy_result
from tests.unit.server._legacy_result_oracle import normalize_legacy_result as original_normalize

_CASES = json.loads(
    (
        Path(__file__).parents[3] / "rust/storage/src/business_facts_ops/testdata/head_symbols_inputs_v0037.json"
    ).read_text()
)["cases"]


@pytest.mark.parametrize(
    "case", [case for case in _CASES if case["kind"] == "normalize"], ids=lambda case: case["name"]
)
def test_native_adapter_matches_frozen_history_and_python_reference(case):
    raw = deepcopy(case["payload"])
    if "error" in case:
        for normalize in (normalize_legacy_result, original_normalize):
            with pytest.raises(TypeError) as error:
                normalize(raw)
            assert str(error.value) == case["error"]
    else:
        assert normalize_legacy_result(raw) == original_normalize(raw) == case["expected"]
    assert raw == case["payload"]


def test_adapter_keeps_evidence_numbers_order_and_non_dictionary_identity():
    raw = {"extra": [2**120, float("nan"), float("inf"), -0.0], "outcome": "error", "memory": {"message": "余额不足"}}
    assert json.dumps(normalize_legacy_result(raw), ensure_ascii=False) == json.dumps(
        original_normalize(raw), ensure_ascii=False
    )
    for value in (None, False, 0, [], [1], "raw"):
        assert normalize_legacy_result(value) is value


def test_legacy_outcome_backfills_status_without_override():
    """旧 outcome 补出 status；已有 status 一律不覆盖。"""
    assert normalize_legacy_result({"outcome": "error"})["status"] == "FAILED"
    assert normalize_legacy_result({"outcome": "completed"})["status"] == "SUCCEEDED"
    assert normalize_legacy_result({"outcome": "not_reached"})["status"] == "PARTIAL"
    assert normalize_legacy_result({"outcome": "blocked"})["status"] == "BLOCKED"
    # 冲突时以明确 status 为准，不翻转历史结论。
    assert normalize_legacy_result({"status": "SUCCEEDED", "outcome": "error"})["status"] == "SUCCEEDED"


def test_success_records_do_not_get_fabricated_error():
    """成功态不强造错误。"""
    assert "error" not in normalize_legacy_result({"outcome": "completed"})
    assert "error" not in normalize_legacy_result({"status": "NOOP", "memory": {"message": "没有需要执行的交易"}})


def test_suffixed_account_error_does_not_invent_session_closed_code() -> None:
    raw = {"status": "BLOCKED", "error": "非交易时段，2 个品种未执行", "symbol_results": {}}
    assert "reason_code" not in normalize_legacy_result(raw)


def test_mixed_blocked_reasons_do_not_lift_account_code() -> None:
    raw = {
        "status": "BLOCKED",
        "error": "2 个品种执行受阻",
        "symbol_results": {
            "IF2609": {"status": "BLOCKED", "error": "非交易时段", "reason_code": "CLOSED"},
            "ag2612": {
                "status": "BLOCKED",
                "error": "未配置合约交易时段",
                "reason_code": "CTP.SESSION.NO_SESSION_TABLE",
            },
        },
    }
    normalized = normalize_legacy_result(raw)
    assert normalized["symbol_results"]["IF2609"]["reason_code"] == COMMON_SESSION_CLOSED
    assert normalized["symbol_results"]["ag2612"]["reason_code"] == "CTP.SESSION.NO_SESSION_TABLE"
    assert "reason_code" not in normalized or normalized.get("reason_code") is None


def test_explicit_null_reason_preserves_account_failure_alongside_closed_symbol():
    raw = {
        "status": "PARTIAL",
        "error": "最终持仓查询失败",
        "reason_code": None,
        "symbol_results": {"A": {"status": "BLOCKED", "reason_code": COMMON_SESSION_CLOSED}},
    }
    normalized = normalize_legacy_result(raw)
    assert normalized["reason_code"] is None
    assert normalized["error"] == "最终持仓查询失败"
