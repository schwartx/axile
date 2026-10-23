"""启动 Axile API 服务的 CLI 入口."""

import argparse
import ipaddress
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn
from loguru import logger

from axile.common.config import is_configured
from axile.server.app import (  # pyright: ignore[reportPrivateUsage]
    ALGORITHM_DIRECTORIES_ENV,
    _app,
    set_algorithm_directories,
)
from axile.server.core.db import initialize_database
from axile.server.core.log_config import setup_logging
from axile.server.core.single_worker import MultiWorkerNotSupportedError, validate_workers_option

__all__ = ["build_arg_parser", "run_server", "main", "uvicorn"]

# 仅允许绑定回环地址，防止服务对外网暴露（尤其禁止 0.0.0.0 / :: 等通配地址）。
_WILDCARD_HOSTS = frozenset({"", "0", "0.0.0.0", "::", "*"})
_LOOPBACK_ALIASES = frozenset({"localhost"})

if sys.platform == "win32":
    import asyncio

    # NOTE: Windows 下 uvicorn 配合 aiohttp 仍可能踩到 ProactorEventLoop 的
    # 兼容性问题，这里统一切回 Selector 策略以保持服务端行为稳定。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _force_exit(_signum: int, _frame: object) -> None:
    """收到终止信号后立即退出，不等待清理流程完成."""
    os._exit(0)


def _validate_host(host: str) -> str:
    """
    校验监听地址，仅允许回环地址以避免对外暴露。

    Parameters
    ----------
    host : str
        计划绑定的监听地址。

    Returns
    -------
    str
        校验通过后的监听地址（原样返回）。

    Raises
    ------
    SystemExit
        当传入 ``0.0.0.0``、``::`` 等通配地址或任何非回环地址时直接退出进程。

    Notes
    -----
    Axile 实盘服务默认只服务本机前端与本地调用方，明确禁止绑定到对外网络接口，
    以降低未授权访问与实盘下单的风险。
    """
    candidate = host.strip()
    lowered = candidate.lower()

    if lowered in _LOOPBACK_ALIASES:
        return candidate

    if lowered in _WILDCARD_HOSTS:
        print(f"[ERROR] 禁止绑定通配地址 {host!r}：为避免对外暴露，仅允许回环地址（127.0.0.0/8、localhost、::1）。")
        sys.exit(1)

    try:
        parsed_ip = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        print(f"[ERROR] 非法监听地址 {host!r}：仅允许回环地址（127.0.0.0/8、localhost、::1）。")
        sys.exit(1)

    if not parsed_ip.is_loopback:
        print(f"[ERROR] 禁止绑定非回环地址 {host!r}：为避免对外暴露（如 0.0.0.0），仅允许回环地址。")
        sys.exit(1)

    return candidate


def build_arg_parser() -> argparse.ArgumentParser:
    """构建服务器启动命令的参数解析器."""
    parser = argparse.ArgumentParser(description="启动基于Uvicorn的FastAPI应用")
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="将socket绑定到指定主机(默认: 127.0.0.1，仅允许回环地址)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="将socket绑定到指定端口(默认:8000)",
    )
    parser.add_argument(
        "--algorithm-dir",
        type=str,
        default="user_algorithms",
        help="用户算法目录，默认: ./user_algorithms",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="工作进程数；当前仅支持 1，传入更大值会直接拒绝启动（实盘互斥与调度均为进程内状态）",
    )
    parser.add_argument("--reload", action="store_true", help="启用后端热重载（开发模式）")
    parser.add_argument(
        "--reload-dir",
        action="append",
        default=[],
        help="热重载监听目录，可多次传入；默认监听 axile 与用户算法目录",
    )
    return parser


def _get_reload_dirs(algorithm_dir: Path, reload_dirs: list[Path] | None) -> list[Path]:
    """
    生成 Uvicorn 热重载监听目录.

    Parameters
    ----------
    algorithm_dir : Path
        当前用户算法目录。
    reload_dirs : list[Path] | None
        CLI 显式传入的额外监听目录。

    Returns
    -------
    list[Path]
        去重后的监听目录列表，顺序稳定。
    """
    ordered: list[Path] = [Path("axile"), algorithm_dir]
    if reload_dirs:
        ordered.extend(reload_dirs)

    result: list[Path] = []
    seen: set[str] = set()
    for directory in ordered:
        key = str(directory)
        if key in seen:
            continue
        seen.add(key)
        result.append(directory)
    return result


def run_server(
    *,
    host: str,
    port: int,
    algorithm_dir: Path = Path("user_algorithms"),
    workers: int | None = None,
    reload: bool = False,
    reload_dirs: list[Path] | None = None,
    migration_runner: Callable[[], None] | None = None,
) -> None:
    """
    根据给定参数启动 Uvicorn 服务器.

    Parameters
    ----------
    host : str
        监听地址；仅允许回环地址，传入 ``0.0.0.0`` 等通配地址会直接退出。
    port : int
        监听端口。
    algorithm_dir : Path, default=Path("user_algorithms")
        用户算法目录。
    workers : int | None, default=None
        工作进程数；当前仅支持单 worker，大于 1 会直接拒绝启动。
    reload : bool, default=False
        是否启用 Uvicorn 热重载；仅用于本地开发。
    reload_dirs : list[Path] | None, optional
        额外热重载监听目录。
    migration_runner : Callable[[], None] | None, optional
        发行层迁移回调；完成初始化后、启动服务前调用，异常会阻止启动。
        缺省时仅升级公共 Axile 迁移链。

    Raises
    ------
    SystemExit
        当 ``host`` 非回环地址，或 ``workers`` 大于 1 时退出进程。

    Notes
    -----
    ``--workers`` 此前是**无效参数**：本函数用 ``Server.run()`` 直接启动，绕过了
    Uvicorn 的多进程 supervisor，取值从不被消费，却给运维「可以横向扩容」的错觉。
    现在显式拒绝大于 1 的取值，让 CLI 行为与实际能力一致。
    """
    # 与 _validate_host 一致：CLI 入口把护栏异常转成友好提示 + 非零退出码，
    # 而不是甩一段 traceback 给运维。
    try:
        validate_workers_option(workers)
    except MultiWorkerNotSupportedError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    if not reload:
        # CLI 入口收到终止信号时直接退出，避免在本地开发或受 supervisor 管理时
        # 长时间等待后台线程、WebSocket 或 worker 清理流程。reload 模式交给
        # Uvicorn supervisor 处理信号，避免 worker 子进程残留。
        signal.signal(signal.SIGINT, _force_exit)
        signal.signal(signal.SIGTERM, _force_exit)

    host = _validate_host(host)
    set_algorithm_directories([algorithm_dir])
    if reload:
        os.environ[ALGORITHM_DIRECTORIES_ENV] = str(algorithm_dir)

    uvicorn_config = uvicorn.Config(
        "axile.server.app:_app" if reload else _app,
        host=host,
        port=port,
        workers=None if reload else workers,
        reload=reload,
        reload_dirs=[str(path) for path in _get_reload_dirs(algorithm_dir, reload_dirs)] if reload else None,
    )

    # Uvicorn 会在 Config 构造阶段准备自己的日志对象；自定义日志桥接若提前安装，
    # 很容易被后续配置覆盖，因此这里明确放在 Config 之后执行。
    setup_logging()

    # 进程真正开始接收请求前先追平 schema，避免 API 入口和后台任务看到不一致的表结构。
    # 未完成初始化配置时跳过迁移：此时数据库地址尚未由向导最终确定，触达数据库会生成无用的库文件。
    if is_configured():
        initialize_database(migration_runner)
    else:
        logger.warning("axile 未完成初始化配置，跳过数据库迁移，进入初始化向导模式。")

    server = uvicorn.Server(config=uvicorn_config)

    try:
        server.run()
    except KeyboardInterrupt:
        pass  # pragma: full coverage
    finally:
        if uvicorn_config.uds and os.path.exists(uvicorn_config.uds):
            os.remove(uvicorn_config.uds)  # pragma: py-win32


def main() -> None:
    """命令行入口：解析参数并启动服务器."""
    parser = build_arg_parser()
    args = parser.parse_args()
    run_server(
        host=args.host,
        port=args.port,
        algorithm_dir=Path(args.algorithm_dir),
        workers=args.workers,
        reload=args.reload,
        reload_dirs=[Path(directory) for directory in args.reload_dir],
    )


if __name__ == "__main__":
    main()
