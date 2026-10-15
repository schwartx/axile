"""一次性执行器的最终资源释放，不适用于账户常驻 worker。"""

from loguru import logger


def close_executor(executor: object) -> None:
    """释放渠道资源；关闭失败仅记录日志，保留原始执行结果或异常。"""
    try:
        try:
            monitor = getattr(executor, "_connection_monitor", None)
            if monitor is not None:
                monitor.close()
        finally:
            stop = getattr(executor, "stop", None)
            close = getattr(executor, "close", None)
            release = stop if callable(stop) else close
            if callable(release):
                release()
    except Exception:
        logger.exception("执行器渠道资源释放失败")
