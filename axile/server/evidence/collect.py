"""由原生只读快照采集证据，Python 保留时区解释和归档数据模型。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axile.executor.algorithms.utils.clock import clock_now, get_default_clock
from axile.server._native import _core
from axile.server.evidence.models import (
    EvidenceError,
    EvidenceSnapshot,
    EvidenceWindow,
    ExecutionEvidence,
    ExportBudget,
)
from axile.server.native_store import native_evidence_operation


def _local_offset(value: datetime) -> int:
    """按 Python 对无时区时间的解释保留跳时缺口的原始 offset。"""
    return int((value.replace(tzinfo=UTC) - value.astimezone(UTC)).total_seconds())


def _window_request(window: EvidenceWindow) -> dict[str, object]:
    """固定自然日边界及服务器历史本地时间的 offset 变更。"""
    lower = (window.start - timedelta(days=2)).replace(tzinfo=None)
    upper = (window.end + timedelta(days=2)).replace(tzinfo=None)
    offsets: list[list[object]] = []
    if not window.legacy_timezone:
        current = lower - timedelta(days=1)
        while current <= upper + timedelta(days=1):
            seconds = _local_offset(current)
            if not offsets or offsets[-1][1] != seconds:
                boundary = current
                if offsets:
                    previous = current - timedelta(hours=1)
                    while (boundary - previous).total_seconds() > 1:
                        midpoint = previous + timedelta(seconds=int((boundary - previous).total_seconds()) // 2)
                        if _local_offset(midpoint) == offsets[-1][1]:
                            previous = midpoint
                        else:
                            boundary = midpoint
                offsets.append([boundary.isoformat(timespec="seconds"), seconds])
            current += timedelta(hours=1)
    return {
        "start_ms": window.start.timestamp() * 1000,
        "end_ms": window.end.timestamp() * 1000,
        "coarse_start": lower.isoformat(timespec="seconds"),
        "coarse_end": upper.isoformat(timespec="seconds"),
        "legacy_timezone": window.legacy_timezone or None,
        "local_offsets": offsets,
    }


def _evidence_error(error: Exception) -> EvidenceError:
    code, message = error.args
    status = {
        "EVIDENCE_NOT_FOUND": 404,
        "EVIDENCE_ACCOUNT_NOT_FOUND": 404,
        "EVIDENCE_OWNER_CONFLICT": 409,
        "EVIDENCE_IDENTIFIER_CONFLICT": 409,
        "EVIDENCE_INVALID_DATA": 409,
        "EVIDENCE_TOO_LARGE": 413,
        "EVIDENCE_TOO_MANY_EXECUTIONS": 413,
        "EVIDENCE_TIMEOUT": 408,
        "EVIDENCE_CANCELLED": 408,
    }.get(code, 422)
    return EvidenceError(message, code=code, status=status)


async def collect_evidence(
    budget: ExportBudget,
    *,
    account_id: int | None = None,
    execution_id: str | None = None,
    execution_ids: list[str] | None = None,
    window: EvidenceWindow | None = None,
) -> EvidenceSnapshot:
    """一次原生调用读取完整证据，不接触交易进程或创建缺失材料。"""
    budget.check()
    snapshot_at = clock_now(tz=UTC).isoformat()
    request = {
        "account_id": account_id,
        "execution_id": execution_id,
        "execution_ids": execution_ids,
        "window": _window_request(window) if window is not None else None,
        "limits": {
            "executions": budget.limits.executions,
            "rows": budget.limits.rows - budget.rows,
            "bytes": budget.limits.bytes - budget.bytes,
            "seconds": budget.limits.seconds - (get_default_clock().time() - budget.started),
        },
    }
    try:
        data = await native_evidence_operation(request, budget.cancelled)
    except _core.NativeError as error:
        if error.args and str(error.args[0]).startswith("EVIDENCE_") and error.args[0] != "EVIDENCE_READ":
            raise _evidence_error(error) from error
        raise
    budget.check(rows=data["rows"], size=data["bytes"])
    return EvidenceSnapshot(
        account=data["account"],
        executions={key: ExecutionEvidence(**item) for key, item in data["executions"].items()},
        account_operations={key: ExecutionEvidence(**item) for key, item in data["account_operations"].items()},
        source_counts=data["source_counts"],
        selected_execution_ids=list(execution_ids) if execution_ids is not None else None,
        window=window,
        snapshot_at=snapshot_at,
    )
