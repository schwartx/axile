"""
Axile 统一 CLI 入口.

使用 Typer 组织主命令与子命令，并在未指定子命令时直接启动 API
服务器。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NotRequired, Optional, TypedDict

import typer


class _ServerOptions(TypedDict):
    host: str
    port: int
    algorithm_dir: Path
    workers: int | None
    reload: bool
    reload_dirs: list[Path]
    migration_runner: NotRequired[Callable[[], None]]


def create_app(migration_runner: Callable[[], None] | None = None) -> typer.Typer:
    """创建 CLI 应用，发行层可注入启动前迁移编排。"""
    app = typer.Typer(
        name="axile", help="Axile 量化交易框架 CLI 工具集", invoke_without_command=True, rich_markup_mode="rich"
    )

    @app.callback()
    def callback(
        ctx: typer.Context,
        host: Annotated[str, typer.Option(help="监听地址（仅允许回环地址）")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="监听端口")] = 8000,
        algorithm_dir: Annotated[Path, typer.Option(help="用户算法目录")] = Path("user_algorithms"),
        workers: Annotated[Optional[int], typer.Option(help="工作进程数；当前仅支持 1，更大值会被拒绝")] = None,
        reload: Annotated[bool, typer.Option(help="启用后端热重载（开发模式）")] = False,
        reload_dir: Annotated[Optional[list[Path]], typer.Option(help="热重载监听目录，可多次传入")] = None,
    ) -> None:
        """处理根命令，未指定子命令时启动 API 服务器。"""
        if ctx.invoked_subcommand is None:
            from axile.server.main import run_server

            kwargs = _ServerOptions(
                host=host,
                port=port,
                algorithm_dir=algorithm_dir,
                workers=workers,
                reload=reload,
                reload_dirs=reload_dir or [],
            )
            if migration_runner is not None:
                kwargs["migration_runner"] = migration_runner
            run_server(**kwargs)

    @app.command("logs")
    def logs(
        paths: Annotated[list[Path], typer.Argument(help="日志文件或 .gz 分片，可传入多个")],
        tree: Annotated[bool, typer.Option("--tree", help="按过程树查看")] = False,
        stats: Annotated[bool, typer.Option("--stats", help="显示各运行最后保存的累计丢弃计数")] = False,
    ) -> None:
        """读取日志时间线、过程树或计数；无需启动服务或加载原生扩展。"""
        from axile.common.log_files import LogDecodeError, format_logs

        try:
            mode = "stats" if stats else "tree" if tree else "timeline"
            output = format_logs(paths, mode)
            if output or mode != "timeline":
                typer.echo(output)
        except (OSError, LogDecodeError) as error:
            raise typer.BadParameter(str(error)) from error

    return app


app = create_app()


def main() -> None:
    """执行 Axile CLI 应用."""
    app()


if __name__ == "__main__":
    main()
