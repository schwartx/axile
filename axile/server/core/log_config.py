"""为 Axile 服务端配置 Loguru sink 与日志拦截.

参考:
- https://github.com/MatthewScholefield/loguru-logging-intercept/blob/main/loguru_logging_intercept.py
- https://github.com/MatthewScholefield/uvicorn-loguru-integration/blob/main/uvicorn_loguru_integration.py.
"""

import atexit
import logging
import traceback
from types import FrameType
from typing import Any, cast

from loguru import logger

from axile.common.config import settings
from axile.common.logging import LogComponent
from axile.common.trade_channel import TradeChannel
from axile.server.core.logging_sink import NativeLogSink

_sink: NativeLogSink | None = None


class InterceptHandler(logging.Handler):
    """将标准库 logging 日志转发到 Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """把标准库日志记录转发到 Loguru."""
        try:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            frame, depth = logging.currentframe(), 1
            while frame is not None and frame.f_code.co_filename in (logging.__file__, __file__):
                frame = cast("FrameType", frame.f_back)
                depth += 1
            logger_with_opts = logger.opt(depth=depth, exception=record.exc_info)
            logger_with_opts.log(level, "{}", record.getMessage())
        except Exception:
            if _sink is not None:
                _sink.invalid()


LOGGING_MODULES = (
    "",
    "uvicorn.error",
    "uvicorn.access",
    "sqlalchemy.engine.Engine",
)

MODULE_LOG_LEVELS = {
    "asyncio": logging.WARNING,
    "threading": logging.WARNING,
    "urllib3": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "starlette.middleware.errors": logging.WARNING,
    "sqlalchemy": logging.WARNING,
    "sqlalchemy.engine.Engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "aiosqlite": logging.WARNING,
}

SUPPRESSED_INFO_LOGGERS = {
    "uvicorn.access",
    "starlette.middleware.errors",
}


def setup_loguru_logging_intercept(level: int = logging.DEBUG) -> None:
    """让选定的标准库 logger 走统一的 Loguru sink."""
    logging.basicConfig(handlers=[InterceptHandler()], level=level)  # noqa
    for logger_name in LOGGING_MODULES:
        mod_logger = logging.getLogger(logger_name)
        mod_logger.handlers = [InterceptHandler(level=level)]
        mod_logger.propagate = False

    for logger_name, logger_level in MODULE_LOG_LEVELS.items():
        logging.getLogger(logger_name).setLevel(logger_level)


# 规则按具体到宽泛排列。显式 ``component`` 始终优先于这里的源码归类。
_COMPONENT_PREFIXES: tuple[tuple[str, LogComponent], ...] = (
    ("axile.executor.algorithms.utils.order_", LogComponent.ORDER),
    ("axile.executor.algorithms", LogComponent.ALGORITHM),
    ("axile.executor.ctp", LogComponent.CTP),
    ("axile.server.execution.ctp_channels", LogComponent.CTP),
    ("axile.executor.gm", LogComponent.GM),
    ("axile.executor.tq", LogComponent.TQ),
    ("axile.server.api", LogComponent.API),
    ("axile.server.execution.worker_backend", LogComponent.WORKER),
    ("axile.server.execution.scheduler", LogComponent.SCHEDULER),
    ("axile.server.execution_audit", LogComponent.AUDIT),
    ("axile.server.error_notifications", LogComponent.NOTIFICATION),
    ("axile.executor.feishu_notifications", LogComponent.NOTIFICATION),
    ("axile.server.execution", LogComponent.EXECUTION),
    ("axile.executor.abstract_executor", LogComponent.EXECUTION),
    ("axile.executor.execution_", LogComponent.EXECUTION),
    ("axile.server", LogComponent.SYSTEM),
    ("axile.executor", LogComponent.EXECUTION),
    ("sqlalchemy", LogComponent.DATABASE),
    ("aiosqlite", LogComponent.DATABASE),
    ("apscheduler", LogComponent.SCHEDULER),
    ("uvicorn", LogComponent.SERVICE),
    ("starlette", LogComponent.SERVICE),
)


def execution_log_context(
    *,
    account_id: object | None = None,
    account_name: str | None = None,
    channel: object | None = None,
    execution_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, object]:
    """
    构造执行链路日志使用的结构化上下文字段.

    Parameters
    ----------
    account_id : object | None, default=None
        账户主键，用于关联同一账户的所有日志。
    account_name : str | None, default=None
        账户名称，便于人工辨识。
    channel : object | None, default=None
        交易渠道；``TradeChannel`` 会被展开为 ``.value`` 字符串。
    execution_id : str | None, default=None
        本次执行的全局标识；填入后可 ``grep`` 出一次执行的全部日志。
    symbol : str | None, default=None
        当前单品种执行会话的交易标的。

    Returns
    -------
    dict[str, object]
        可直接传入 ``logger.contextualize`` / ``logger.bind`` 的上下文字典。
    """
    channel_value = channel.value if isinstance(channel, TradeChannel) else channel
    return {
        "account_id": account_id,
        "account_name": account_name,
        "channel": channel_value,
        "execution_id": execution_id,
        "symbol": symbol,
    }


def _component_label(name: str | None, extra: dict[str, Any]) -> str:
    """根据显式绑定或源码命名空间解析稳定业务组件."""
    explicit = extra.get("component")
    if explicit:
        return str(explicit)

    logger_name = name or ""
    for prefix, component in _COMPONENT_PREFIXES:
        if logger_name.startswith(prefix):
            return component.value
    if logger_name.startswith("axile."):
        return LogComponent.SYSTEM.value
    return LogComponent.EXTERNAL.value


def _exception_payload(exception: Any) -> dict[str, str] | None:
    """把 Loguru 异常记录转换为 JSON 可序列化结构."""
    if exception is None:
        return None
    return {
        "type": exception.type.__name__,
        "message": str(exception.value),
        "traceback": "".join(traceback.format_exception(exception.type, exception.value, exception.traceback)),
    }


def _sink_filter(record: dict[str, Any]) -> bool:
    """过滤低价值第三方日志，保留 WARNING 及以上."""
    logger_name = record.get("name", "")
    level_no = record["level"].no
    if logger_name in SUPPRESSED_INFO_LOGGERS and level_no < logging.WARNING:
        return False
    return True


def setup_logging() -> None:
    """安装进程内共享 sink；重复调用不创建重复写线程。"""
    global _sink
    level = "INFO" if settings.environment == "local" else "WARNING"
    config = (settings.app_log_dir, level, settings.axile_log_rotation)
    if (
        _sink is not None
        and not _sink.closed
        and not _sink.error
        and _sink.config == config
        and not _sink.stats()["stopped"]
    ):
        return
    shutdown_logging()
    _sink = NativeLogSink(*config)
    handler: Any = {
        "sink": _sink,
        "format": lambda record: "{message}",
        "level": level,
        "filter": _sink_filter,
        "backtrace": False,
        "diagnose": False,
    }
    logger.configure(handlers=[handler], extra={})
    setup_loguru_logging_intercept()


def logging_stats() -> dict[str, Any]:
    """返回本进程日志计数，不依赖队列或磁盘可用。"""
    return _sink.stats() if _sink is not None else {"dropped_total": 0, "stopped": True}


def shutdown_logging() -> None:
    """正常关闭时限时排空日志，失败不覆盖业务的退出原因。"""
    if _sink is not None:
        _sink.stop()


atexit.register(shutdown_logging)
