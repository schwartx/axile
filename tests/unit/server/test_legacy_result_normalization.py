"""旧执行记录读时归一的契约测试：只补明确结论，不改写历史，不透传技术原文。"""

from axile.executor.session_closed import COMMON_SESSION_CLOSED
from axile.server.execution.legacy_compat import normalize_legacy_result


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
