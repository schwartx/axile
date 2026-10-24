"""旧执行记录读时归一的契约测试：只补明确结论，不改写历史，不透传技术原文。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from axile.server.execution.legacy_compat import normalize_legacy_result

_CASES = json.loads(
    (
        Path(__file__).parents[3] / "rust/storage/src/business_facts_ops/testdata/head_symbols_inputs_v0037.json"
    ).read_text()
)["cases"]


@pytest.mark.parametrize(
    "case", [case for case in _CASES if case["kind"] == "normalize"], ids=lambda case: case["name"]
)
def test_native_adapter_matches_frozen_history(case):
    raw = deepcopy(case["payload"])
    if "error" in case:
        with pytest.raises(TypeError) as error:
            normalize_legacy_result(raw)
        assert str(error.value) == case["error"]
    else:
        assert normalize_legacy_result(raw) == case["expected"]
    assert raw == case["payload"]


def test_adapter_keeps_evidence_numbers_order_and_non_dictionary_identity():
    raw = {"extra": [2**120, float("nan"), float("inf"), -0.0], "outcome": "error", "memory": {"message": "余额不足"}}
    expected = {**raw, "status": "FAILED", "error": "余额不足"}
    assert json.dumps(normalize_legacy_result(raw), ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    for value in (None, False, 0, [], [1], "raw"):
        assert normalize_legacy_result(value) is value
