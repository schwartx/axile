"""执行结论里「交易时段关闭」的身份。

活路径只许把 :data:`COMMON_SESSION_CLOSED` 写入 ``AlgorithmResult.reason_code``。
产品钟 / 下单守卫的渠道内部码（TQ ``CLOSED``、``CTP.SESSION.CLOSED``）不得进入执行结论。
曾经落库的旧码和旧中文整句只在 ``axile.server.execution.legacy_compat`` 读时回填。
"""

COMMON_SESSION_CLOSED = "COMMON.SESSION.CLOSED"
SESSION_CLOSED_MESSAGE = "非交易时段"


def session_closed_scope(result: object) -> str | None:
    """识别明确的全休市或部分休市结果；真实失败、其他阻断和订单证据不被跳过。"""
    if not isinstance(result, dict) or result.get("reason_code") != COMMON_SESSION_CLOSED:
        return None
    status = result.get("status")
    if status not in {"BLOCKED", "PARTIAL"} or result.get("task_status") == "TERMINATED":
        return None
    if status == "BLOCKED" and (result.get("orders") or result.get("trades")):
        return None
    symbols = result.get("symbol_results", {})
    if not isinstance(symbols, dict):
        return None
    if not symbols:
        return "all" if status == "BLOCKED" else None
    kinds = [_session_result_kind(value) for value in symbols.values()]
    if None in kinds or "closed" not in kinds:
        return None
    if "open" in kinds:
        return "partial" if status == "PARTIAL" else None
    return "all" if status == "BLOCKED" else None


def _session_result_kind(result: object) -> str | None:
    """只接受成功结果或没有订单、成交的明确休市阻断。"""
    if not isinstance(result, dict):
        return None
    if result.get("status") in {"SUCCEEDED", "NOOP"}:
        return "open"
    if (
        result.get("status") == "BLOCKED"
        and result.get("reason_code") == COMMON_SESSION_CLOSED
        and not result.get("orders")
        and not result.get("trades")
    ):
        return "closed"
    return None
