"""冻结重构前的 Python 规则，独立核验 Rust 归一化行为。"""

import re
from typing import Any

from axile.executor.session_closed import COMMON_SESSION_CLOSED, SESSION_CLOSED_MESSAGE

_SESSION_CLOSED_CODES = frozenset({COMMON_SESSION_CLOSED, "CLOSED", "CTP.SESSION.CLOSED"})
_SESSION_CLOSED_ERRORS = frozenset({SESSION_CLOSED_MESSAGE, "当前不在交易时段", "当前不在交易时间"})
_STATUS_FROM_OUTCOME = {"completed": "SUCCEEDED", "not_reached": "PARTIAL", "error": "FAILED", "blocked": "BLOCKED"}
_OUTCOME_FROM_STATUS = {value: key for key, value in _STATUS_FROM_OUTCOME.items()}
_OUTCOME_FROM_STATUS["NOOP"] = "completed"
_TERMINATED_OUTCOME = "terminated"
_TERMINATED_TASK_STATUS = "TERMINATED"
_LEGACY_ERROR_COPY = {
    "error": "执行失败，具体原因见执行证据",
    "not_reached": "执行不到位",
    "blocked": "执行受阻，具体原因见执行证据",
    _TERMINATED_OUTCOME: "执行已终止",
}
_EXCEPTION_CLASS = re.compile("\\b[\\w.]*(Error|Exception)\\b[:\\s]")
_TECHNICAL_MARKERS = ("Traceback", "ErrorID=", "return_code=")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _is_technical_text(text: str) -> bool:
    return bool(
        _EXCEPTION_CLASS.search(text)
        or any((marker in text for marker in _TECHNICAL_MARKERS))
        or re.fullmatch("(?:-?\\d+|[A-Z][A-Z0-9_.]*)", text.strip())
        or (text == "gm_authentication_error")
    )


def _legacy_message(raw: dict[str, Any]) -> str | None:
    memory = raw.get("memory")
    candidates = (
        raw.get("outcome_reason"),
        raw.get("msg"),
        memory.get("message") if isinstance(memory, dict) else None,
    )
    texts = [text for item in candidates if (text := _text(item))]
    return next((text for text in texts if not _is_technical_text(text)), texts[0] if texts else None)


def normalize_legacy_result(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    result = dict(raw)
    outcome = result.get("outcome")
    if not _text(result.get("status")) and isinstance(outcome, str):
        result["status"] = _STATUS_FROM_OUTCOME.get(outcome)
        if outcome == _TERMINATED_OUTCOME and (not _text(result.get("task_status"))):
            result["task_status"] = _TERMINATED_TASK_STATUS
    if not _text(result.get("outcome")):
        status = result.get("status")
        result["outcome"] = _OUTCOME_FROM_STATUS.get(status) if isinstance(status, str) else None
        if result["outcome"] is None and result.get("task_status") == _TERMINATED_TASK_STATUS:
            result["outcome"] = _TERMINATED_OUTCOME
    if not _text(result.get("error")):
        legacy = _legacy_message(raw)
        needs_error = (
            _is_unsuccessful(result.get("status"))
            or result.get("task_status") == _TERMINATED_TASK_STATUS
            or (result.get("status") is None and legacy is not None)
        )
        if needs_error:
            if legacy is not None and (not _is_technical_text(legacy)):
                result["error"] = legacy
            else:
                if legacy is not None:
                    result.setdefault("technical_detail", legacy)
                result["error"] = _LEGACY_ERROR_COPY.get(str(result.get("outcome"))) or "执行结果待确认"
    elif _is_technical_text(result["error"]):
        result.setdefault("technical_detail", result["error"])
        reason = _text(result.get("outcome_reason"))
        result["error"] = (
            reason
            if reason and (not _is_technical_text(reason))
            else _LEGACY_ERROR_COPY.get(_OUTCOME_FROM_STATUS.get(str(result.get("status")), "")) or "执行结果待确认"
        )
    symbols = result.get("symbol_results")
    if isinstance(symbols, dict):
        result["symbol_results"] = {symbol: normalize_legacy_result(item) for symbol, item in symbols.items()}
    _fill_reason_code(result)
    return result


def _as_reason_code(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return COMMON_SESSION_CLOSED if text in _SESSION_CLOSED_CODES else text


def _fill_reason_code(result: dict[str, Any]) -> None:
    existing = _text(result.get("reason_code"))
    if existing is not None:
        if existing in _SESSION_CLOSED_CODES and existing != COMMON_SESSION_CLOSED:
            result["reason_code"] = COMMON_SESSION_CLOSED
        return
    if "reason_code" in result:
        return
    memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
    from_memory = _as_reason_code(memory.get("symbol_decision_reason_code"))
    if from_memory is not None:
        result["reason_code"] = from_memory
        return
    if result.get("status") == "BLOCKED" and result.get("error") in _SESSION_CLOSED_ERRORS:
        result["reason_code"] = COMMON_SESSION_CLOSED
        return
    symbols = result.get("symbol_results")
    if not isinstance(symbols, dict) or not symbols:
        return
    failed_codes: set[str | None] = set()
    for item in symbols.values():
        if not isinstance(item, dict) or not _is_unsuccessful(item.get("status")):
            continue
        failed_codes.add(_text(item.get("reason_code")))
    if failed_codes == {COMMON_SESSION_CLOSED}:
        result["reason_code"] = COMMON_SESSION_CLOSED


def _is_unsuccessful(status: object) -> bool:
    return status in {"FAILED", "PARTIAL", "BLOCKED", "TERMINATED"}
