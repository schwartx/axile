"""账户控制审计查询；HTTP 直接使用原生编码的 JSON。"""

from axile.server.native_store import native_operation

EXECUTION_LIST_DEFAULT_LIMIT = 100
EXECUTION_EVENTS_DEFAULT_LIMIT = 200
AUDIT_QUERY_MAX_LIMIT = 500


def _normalize_pagination(*, skip: int, limit: int, default_limit: int) -> tuple[int, int]:
    if skip < 0:
        raise ValueError("skip 必须大于等于 0")
    normalized_limit = default_limit if limit <= 0 else limit
    if normalized_limit > AUDIT_QUERY_MAX_LIMIT:
        raise ValueError(f"limit 不能超过 {AUDIT_QUERY_MAX_LIMIT}")
    return skip, normalized_limit


async def query_account_control_audit_summary(*, account_id: int, control_date: str) -> bytes:
    """查询账户日审计汇总，保留 operation、decision、outcome 分组。"""
    return await native_operation(
        "control_audit_summary_json", {"account_id": account_id, "control_date": control_date}
    )


async def query_account_control_audit_executions(
    *, account_id: int, control_date: str, skip: int = 0, limit: int = EXECUTION_LIST_DEFAULT_LIMIT
) -> bytes:
    """按最后事件时间和上下文 ID 倒序查询一页控制上下文。"""
    skip, limit = _normalize_pagination(skip=skip, limit=limit, default_limit=EXECUTION_LIST_DEFAULT_LIMIT)
    return await native_operation(
        "control_audit_executions_json",
        {"account_id": account_id, "control_date": control_date, "skip": skip, "limit": limit},
    )


async def query_account_control_execution_events(
    *,
    execution_id: str,
    symbol: str | None = None,
    operation: str | None = None,
    skip: int = 0,
    limit: int = EXECUTION_EVENTS_DEFAULT_LIMIT,
) -> bytes:
    """按 seq 和自增 ID 查询一页控制事件，保留事件时间及 metadata。"""
    skip, limit = _normalize_pagination(skip=skip, limit=limit, default_limit=EXECUTION_EVENTS_DEFAULT_LIMIT)
    return await native_operation(
        "control_audit_events_json",
        {"execution_id": execution_id, "symbol": symbol, "operation": operation, "skip": skip, "limit": limit},
    )
