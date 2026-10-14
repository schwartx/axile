"""编译、执行并校验用户提供的组合目标函数."""

from __future__ import annotations

import math
import traceback
from dataclasses import dataclass
from typing import cast

from axile.common.function_contract import accepts_context
from axile.executor.connection_state import connection_error_scope
from axile.server.context import Context


class PortfolioFunctionError(ValueError):
    """携带源码定位信息的组合函数错误."""

    def __init__(
        self,
        message: str,
        *,
        error_line: int | None = None,
        error_offset: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        formatted_traceback: str | None = None,
        error_scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_line: int | None = error_line
        self.error_offset: int | None = error_offset
        self.error_type: str | None = error_type
        self.error_message: str | None = error_message
        self.formatted_traceback: str | None = formatted_traceback
        self.error_scope = error_scope


@dataclass(slots=True)
class PortfolioFunctionResult:
    """组合函数的结构化执行结果."""

    ok: bool
    target: dict[str, float] | None = None
    error: PortfolioFunctionError | None = None

    def to_payload(self) -> dict[str, object]:
        """转换为可跨 worker 管道传输的字典."""
        if self.ok:
            return {"ok": True, "target": dict(self.target or {})}
        error = self.error or PortfolioFunctionError("自定义组合函数执行失败")
        return {
            "ok": False,
            "error": str(error),
            "error_line": error.error_line,
            "error_offset": error.error_offset,
            "error_type": error.error_type,
            "error_message": error.error_message,
            "traceback": error.formatted_traceback,
            "error_scope": error.error_scope,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "PortfolioFunctionResult":
        """从 worker 响应恢复结构化执行结果."""
        if payload.get("ok") is True:
            raw_target = payload.get("target")
            return cls(ok=True, target=cast("dict[str, float]", raw_target if isinstance(raw_target, dict) else {}))
        message = str(payload.get("error") or payload.get("error_message") or "自定义组合函数执行失败")
        return cls(
            ok=False,
            error=PortfolioFunctionError(
                message,
                error_line=cast("int | None", payload.get("error_line")),
                error_offset=cast("int | None", payload.get("error_offset")),
                error_type=str(payload.get("error_type") or "RuntimeError"),
                error_message=str(payload.get("error_message") or message),
                formatted_traceback=str(payload.get("traceback") or ""),
                error_scope=cast("str | None", payload.get("error_scope")),
            ),
        )


def _error_from_exception(exc: BaseException) -> PortfolioFunctionError:
    """把编译或运行异常转换为可展示的结构化错误."""
    line = exc.lineno if isinstance(exc, SyntaxError) else None
    offset = exc.offset if isinstance(exc, SyntaxError) else None
    if line is None:
        for frame in traceback.extract_tb(exc.__traceback__):
            if frame.filename == "<portfolio>":
                line = frame.lineno
    message = (
        getattr(exc, "technical_detail", None)
        or (exc.msg if isinstance(exc, SyntaxError) else str(exc))
        or type(exc).__name__
    )
    return PortfolioFunctionError(
        message,
        error_line=line,
        error_offset=offset,
        error_type=type(exc).__name__,
        error_message=message,
        formatted_traceback=getattr(exc, "formatted_traceback", None) or "".join(traceback.format_exception(exc)),
        error_scope=connection_error_scope(exc),
    )


def _normalize_target(target: object) -> dict[str, float]:
    """校验并规范化 ``symbol -> weight`` 返回值."""
    if not isinstance(target, dict):
        raise TypeError(f"calculate_portfolio 必须返回 dict[str, float]，实际返回 {type(target).__name__}")
    normalized: dict[str, float] = {}
    for key, value in cast("dict[object, object]", target).items():
        if not isinstance(key, str):
            raise TypeError(f"权重字典的键必须是品种字符串，实际为 {type(key).__name__}: {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"品种 {key} 的权重必须是数字，实际为 {type(value).__name__}: {value!r}")
        weight = float(value)
        if not math.isfinite(weight):
            raise ValueError(f"品种 {key} 的权重不是有限数值: {value!r}")
        normalized[key] = weight
    return normalized


def calculate_portfolio_target(code: str, context: Context) -> PortfolioFunctionResult:
    """在当前 Python 运行环境执行 ``calculate_portfolio(context)``."""
    try:
        namespace: dict[str, object] = {}
        exec(compile(code, "<portfolio>", "exec"), namespace)  # noqa: S102 - 用户明确提供 Python 函数
        function = namespace.get("calculate_portfolio")
        if not callable(function):
            raise ValueError("脚本必须定义 calculate_portfolio(context) 函数")
        if not accepts_context(function):
            raise TypeError("calculate_portfolio 必须是同步函数，且只能接收一个位置参数 context")
        return PortfolioFunctionResult(ok=True, target=_normalize_target(function(context)))
    except BaseException as exc:  # noqa: BLE001 - 用户函数错误需结构化返回
        if bool(getattr(exc, "requires_session_recovery", False)):
            raise
        return PortfolioFunctionResult(ok=False, error=_error_from_exception(exc))


def portfolio_result_from_exception(exc: BaseException) -> PortfolioFunctionResult:
    """把执行器准备或 IPC 异常转换为组合函数结果."""
    return PortfolioFunctionResult(ok=False, error=_error_from_exception(exc))


__all__ = [
    "PortfolioFunctionError",
    "PortfolioFunctionResult",
    "_normalize_target",
    "calculate_portfolio_target",
    "portfolio_result_from_exception",
]
