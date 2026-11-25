"""掘金交易与独立行情共用的本机终端准备。"""

import json
import subprocess
import sys
from pathlib import Path
from threading import Lock

import psutil

from axile.executor.algorithms.utils.clock import get_default_clock

_started: dict[Path, subprocess.Popen[bytes]] = {}
_launch_lock = Lock()


def gm_rpc_port(directory: Path) -> int:
    """读取终端配置中的 RPC 端口。"""
    with (directory / "resources" / "app" / "gmserv.json").open(encoding="utf-8") as stream:
        return int(json.load(stream)["default"]["rpcPort"])


def launch_gm_terminal(directory: Path) -> None:
    """启动指定目录中的终端，避免在其启动期间重复启动。"""
    executable = directory / "goldminer3.exe"
    with _launch_lock:
        process = _started.get(executable)
        if process is not None and process.poll() is None:
            return
        _started[executable] = subprocess.Popen(
            [executable],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )


def wait_for_gm_port(port: int, timeout: float) -> bool:
    """在限时内观察终端的 RPC 端口监听。"""
    clock = get_default_clock()
    deadline = clock.monotonic() + timeout
    while (remaining := deadline - clock.monotonic()) > 0:
        if any(
            conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr[1] == port
            for conn in psutil.net_connections(kind="inet")
        ):
            return True
        clock.sleep(min(0.2, remaining))
    return False
