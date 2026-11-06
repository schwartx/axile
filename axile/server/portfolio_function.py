"""编译、执行并校验用户提供的组合目标函数."""

from __future__ import annotations

import traceback

from axile.common.function_contract import accepts_context
from axile.executor.connection_state import connection_error_scope
from axile.executor.models.portfolio_target import PortfolioTarget
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


def _normalize_target(target: object) -> PortfolioTarget:
    """校验旧权重字典或带显式口径的目标返回值。"""
    return PortfolioTarget.from_result(target)


def calculate_portfolio_target(code: str, context: Context) -> PortfolioTarget:
    """在当前 Python 运行环境执行 ``calculate_portfolio(context)``."""
    try:
        namespace: dict[str, object] = {}
        exec(compile(code, "<portfolio>", "exec"), namespace)  # noqa: S102 - 用户明确提供 Python 函数
        function = namespace.get("calculate_portfolio")
        if not callable(function):
            raise ValueError("脚本必须定义 calculate_portfolio(context) 函数")
        if not accepts_context(function):
            raise TypeError("calculate_portfolio 必须是同步函数，且只能接收一个位置参数 context")
        return _normalize_target(function(context))
    except BaseException as exc:  # noqa: BLE001 - 用户函数错误需结构化返回
        if bool(getattr(exc, "requires_session_recovery", False)):
            raise
        raise _error_from_exception(exc) from exc


__all__ = [
    "PortfolioFunctionError",
    "_normalize_target",
    "calculate_portfolio_target",
]
