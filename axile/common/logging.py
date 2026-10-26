"""定义 Axile 运行时日志使用的稳定业务组件标签."""

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Any, Iterator

LOG_CONTEXT_FIELDS = (
    "account_id",
    "account_name",
    "channel",
    "execution_id",
    "symbol",
    "algorithm",
    "trigger_source",
    "calendar_id",
    "calendar_day",
)
_scope: ContextVar[tuple[dict[str, Any], tuple[dict[str, Any], ...]]] = ContextVar("axile_log_scope", default=({}, ()))


class LogComponent(StrEnum):
    """控制台与结构化日志共享的业务组件."""

    SYSTEM = "系统"
    SERVICE = "服务"
    API = "接口"
    EXECUTION = "执行"
    WORKER = "任务"
    SCHEDULER = "调度"
    AUDIT = "审计"
    ALGORITHM = "算法"
    ORDER = "订单"
    CTP = "CTP"
    GM = "掘金"
    TQ = "天勤"
    MARKET_DATA = "行情"
    NOTIFICATION = "通知"
    MANUAL = "人工"
    SIMULATION = "仿真"
    DATABASE = "数据库"
    EXTERNAL = "外部"


def bind_log_context(logger: Any, *, component: LogComponent | None = None, **context: object) -> Any:
    """为 Loguru logger 绑定业务上下文，并兼容测试中的轻量 logger."""
    values = {key: value for key, value in context.items() if value is not None}
    if component is not None:
        values["component"] = component.value
    bind = getattr(logger, "bind", None)
    return bind(**values) if callable(bind) else logger


def current_log_context() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """快照当前协程的日志上下文与祖先过程，不修改业务状态。"""
    context, spans = _scope.get()
    return dict(context), list(spans)


@contextmanager
def native_log_scope(request: object) -> Iterator[None]:
    """在同步原生调用所在的线程传递一次上下文，日志失败不影响调用。"""
    previous = None
    core = None
    try:
        from axile.server._native import _core as core

        context, spans = current_log_context()
        if isinstance(request, dict):
            context.update({key: request[key] for key in LOG_CONTEXT_FIELDS if request.get(key) is not None})
        previous = core.logging_scope(
            json.dumps({"context": context, "spans": spans}, ensure_ascii=False, allow_nan=False)
        )
    except Exception:
        pass
    try:
        yield
    finally:
        if previous is not None and core is not None:
            try:
                core.logging_scope(previous)
            except Exception:
                pass


@contextmanager
def log_span(name: str, *, component: LogComponent = LogComponent.SYSTEM, **context: object) -> Iterator[None]:
    """记录独立身份的过程；开始或结束丢失时，查看器明确标记不完整。"""
    from loguru import logger

    values, spans = current_log_context()
    values.update({key: value for key, value in context.items() if value is not None})
    descriptor = None
    try:
        from axile.server._native import _core

        descriptor = json.loads(
            _core.logging_span(
                name, json.dumps(values, ensure_ascii=False, allow_nan=False), spans[-1]["id"] if spans else None
            )
        )
    except Exception:
        pass
    if descriptor is not None:
        spans.append(descriptor)
    token = _scope.set((values, tuple(spans)))

    def event(kind: str, template: str, *, exception: BaseException | None = None, **args: object) -> None:
        try:
            logger.opt(exception=exception).bind(
                component=component.value, _log_template=template, _log_args=args, _log_kind=kind
            ).log("WARNING" if exception is not None else "INFO", template.format_map(args))
        except Exception:
            try:
                from axile.server._native import _core

                _core.logging_invalid()
            except Exception:
                pass

    try:
        event("span_start", "{name}开始", name=name)
        try:
            yield
        except BaseException as error:
            event(
                "span_end",
                "{name}完成",
                exception=error,
                name=name,
                outcome="cancelled" if isinstance(error, asyncio.CancelledError) else "error",
            )
            raise
        else:
            event("span_end", "{name}完成", name=name, outcome="ok")
    finally:
        _scope.reset(token)
