"""错误边界保留异常原文和实际 traceback，兼容 JSON 存储与响应。"""

from __future__ import annotations

import json
import math
import traceback
from typing import Any

from pydantic import BaseModel

from axile.executor.connection_state import connection_error_scope


def _finite_error_values(value: Any) -> Any:
    """非有限浮点数也不属于 JSON 值，保留其原始字符串。"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite_error_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_error_values(item) for item in value]
    return value


def json_error_value(value: Any) -> Any:
    """保留全部字段，JSON 不支持的异常值使用其原始字符串。"""
    return json.loads(
        json.dumps(
            _finite_error_values(value),
            default=lambda item: item.model_dump(mode="json") if isinstance(item, BaseModel) else str(item),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def exception_details(exc: BaseException, message: str | None = None) -> dict[str, object]:
    """在捕获处固定原文，优先保留跨进程传来的实际 traceback。"""
    raw = getattr(exc, "technical_detail", None) or str(exc)
    remote = getattr(exc, "formatted_traceback", None)
    trace = remote or "".join(traceback.format_exception(exc))
    result: dict[str, object] = {
        "message": f"{message}：{raw}" if message else str(raw),
        "technical_detail": str(raw),
        "traceback": trace,
        "exception_type": type(exc).__name__,
    }
    scope = connection_error_scope(exc)
    if scope is not None:
        result["error_scope"] = scope
    code = getattr(exc, "reason_code", None)
    if code is not None:
        result["code"] = code
    for key in ("error_id", "retryable", "diagnostics"):
        value = getattr(exc, key, None)
        if value is not None:
            result[key] = value
    validation_errors = getattr(exc, "errors", None) or getattr(exc.__cause__, "errors", None)
    if callable(validation_errors):
        result["validation_errors"] = json_error_value(validation_errors())
    return json_error_value(result)
