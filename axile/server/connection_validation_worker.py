"""隔离原生 SDK；通过私有 Pipe 传递凭据与安全阶段事件。"""

from __future__ import annotations

import os
import signal
import tempfile
import threading
import traceback
from multiprocessing.connection import Connection
from pathlib import Path

from loguru import logger

from axile.executor.connection_progress import connection_step_error


def _stop(_signum: int, _frame: object) -> None:
    """允许主线程进入渠道验证器的 finally 释放原生 API。"""
    raise SystemExit(0)


def validation_worker(connection: Connection) -> None:
    """运行单个验证会话；柜台错误原文与结构化阶段结果一并返回。"""
    logger.remove()
    # 原生库可能直接写 stdout/stderr；凭据和原始柜台响应不可进入服务日志。
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    if os.name != "nt":
        signal.signal(signal.SIGTERM, _stop)
    lock = threading.Lock()

    def send(event: dict[str, object]) -> None:
        with lock:
            connection.send(event)

    request: dict[str, object] = {}
    try:
        request = connection.recv()
        tempfile.tempdir = request["temp_dir"]
        from axile.channels import get_channel

        channel = request["channel"]
        if not isinstance(channel, str):
            raise ValueError("连接验证缺少渠道标识")
        plugin = get_channel(channel)
        config = plugin.account_config_model.model_validate(request["account_config"])
        validate = plugin.validate_connection
        if validate is None:
            raise RuntimeError("渠道未声明验证器")

        def report(step, status, details=None):
            send({"step": step, "status": status, "details": details})

        validate(config, report)
        send({"result": "succeeded"})
    except Exception as exc:  # noqa: BLE001 - 在进程边界组装结构化错误
        error = connection_step_error(exc)
        send(
            {
                **error.model_dump(mode="json"),
                "result": "failed",
                # 仅保留异常类型及代码位置；不包含异常原文、源码行或局部变量。
                "diagnostic": {
                    "exception_type": type(exc).__name__,
                    "frames": [
                        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                        for frame in traceback.extract_tb(exc.__traceback__)[-8:]
                    ],
                },
            }
        )
    finally:
        connection.close()
