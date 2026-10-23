"""FastAPI 应用组装与启动生命周期钩子."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from loguru import logger

from axile.common.config import API_V1_STR, PROJECT_NAME, is_configured, settings
from axile.executor.algorithms.core.loader import load_algorithm_modules
from axile.server.api.main import api_router
from axile.server.compression import CompressionMiddleware
from axile.server.core.scheduler import scheduler
from axile.server.core.single_worker import ensure_single_worker
from axile.server.error_details import json_error_value
from axile.server.execution.ctp_channels import register_china_channel_jobs
from axile.server.execution.dispatcher import recover_intents_on_startup, shutdown_dispatchers
from axile.server.execution.live import live_hub
from axile.server.execution.worker_backend.manager import shutdown_worker_backend_manager
from axile.server.initial_data import init_scheduler

__all__ = [
    "ALGORITHM_DIRECTORIES_ENV",
    "_app",
    "settings",
    "get_directory_algorithm_modules",
    "load_configured_algorithm_modules",
]

ALGORITHM_DIRECTORIES_ENV = "AXILE_ALGORITHM_DIRECTORIES"


def _initial_algorithm_directories() -> list[Path]:
    """
    读取初始算法目录，优先级为「环境变量 > config.toml > 默认」.

    Returns
    -------
    list[Path]
        算法目录列表；三者皆空时使用默认 ``user_algorithms``。

    Notes
    -----
    Uvicorn 热重载模式会在 worker 子进程中重新 import 本模块，无法继承父进程
    调用 :func:`set_algorithm_directories` 后的模块级状态。因此 reload 启动器会用
    ``AXILE_ALGORITHM_DIRECTORIES`` 把目录传给 worker。持久化目录则由向导/配置页写入
    ``config.toml`` 的 ``algorithm_directories``，进程重启后经此读回。
    """
    raw = os.getenv(ALGORITHM_DIRECTORIES_ENV, "")
    env_directories = [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]
    if env_directories:
        return env_directories

    configured = [Path(item).expanduser() for item in settings.algorithm_directories if item.strip()]
    return configured or [Path("user_algorithms")]


_algorithm_directories: list[Path] = _initial_algorithm_directories()


def set_algorithm_directories(directories: Iterable[Path | str]) -> None:
    """
    覆盖应用启动时要扫描的用户算法目录集合。

    Parameters
    ----------
    directories : Iterable[Path | str]
        需要参与算法模块发现的目录列表；空字符串会被忽略。

    Notes
    -----
    目录配置保存在模块级状态中，是因为 CLI 会在 FastAPI 应用对象已经构造后，
    再根据命令行参数覆写这份启动配置。
    """
    global _algorithm_directories
    _algorithm_directories = [Path(directory).expanduser() for directory in directories if str(directory).strip()]


def _get_directory_algorithm_modules(directory: Path) -> list[str]:
    """
    解析单个用户算法目录中可导入的模块名。

    Parameters
    ----------
    directory : Path
        用户提供的算法目录。

    Returns
    -------
    list[str]
        可以交给 ``load_algorithm_modules`` 加载的模块名列表。

    Notes
    -----
    目录既可以是标准 Python 包，也可以只是若干平铺的 ``.py`` 文件。
    两种场景都通过把父目录加入 ``sys.path`` 来保持导入路径一致。
    """
    package_dir = directory.expanduser().resolve(strict=False)

    if not package_dir.exists():
        logger.info("用户算法目录不存在，跳过加载: {}", package_dir)
        return []
    if not package_dir.is_dir():
        raise NotADirectoryError(f"算法目录不是有效目录: {package_dir}")

    import_root = str(package_dir.parent)
    # 只注入父目录，避免把包目录本身作为顶层路径后改变模块解析结果。
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

    if (package_dir / "__init__.py").exists():
        return [package_dir.name]

    modules = [
        f"{package_dir.name}.{file.stem}"
        for file in sorted(package_dir.glob("*.py"))
        if file.name != "__init__.py" and not file.stem.startswith("_")
    ]
    if not modules:
        logger.info("用户算法目录中未找到可加载模块: {}", package_dir)
    return modules


def _load_configured_algorithm_directories() -> list[str]:
    """
    加载目录配置中声明的用户算法模块。

    Returns
    -------
    list[str]
        成功完成扫描并触发加载的目录列表。
    """
    loaded_directories: list[str] = []

    for directory in _algorithm_directories:
        modules = _get_directory_algorithm_modules(directory)
        if not modules:
            continue

        load_algorithm_modules(modules)
        loaded_directories.append(str(directory))
        logger.info("已加载用户算法目录: {} -> {}", directory, ", ".join(modules))

    return loaded_directories


def _load_configured_algorithm_modules() -> list[str]:
    """
    加载环境配置中显式声明的算法模块。

    Returns
    -------
    list[str]
        实际成功导入的模块名列表。
    """
    modules = [item.strip() for item in settings.algorithm_modules if item.strip()]
    loaded_modules: list[str] = []

    for module in modules:
        try:
            load_algorithm_modules([module])
        except ModuleNotFoundError as exc:
            if exc.name == module:
                logger.info("用户算法模块不存在，跳过加载: {}", module)
                continue
            logger.exception("加载用户算法模块失败: {}", module)
            raise
        except Exception:
            logger.exception("加载用户算法模块失败: {}", module)
            raise
        else:
            loaded_modules.append(module)
            logger.info("已加载用户算法模块: {}", module)

    return loaded_modules


get_directory_algorithm_modules = _get_directory_algorithm_modules
load_configured_algorithm_modules = _load_configured_algorithm_modules


async def _recover_accounts_in_background() -> None:
    """账户连接属于账户运行态，恢复失败不能阻止 API 提供服务。"""
    try:
        await init_scheduler()
    except Exception:  # noqa: BLE001 - 后台恢复入口必须记录异常而非传播到服务生命周期
        logger.exception("后台恢复账户运行态失败")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """
    在启动时拉起后台服务，并在关闭时停止它们.

    Notes
    -----
    未完成初始化配置（见 :func:`axile.common.config.is_configured`）时进入
    「初始化向导模式」：跳过算法加载、调度器启动与账户初始化（``init_scheduler``
    会读取数据库），仅由前端展示初始化向导并调用 ``/api/v1/init`` 接口；
    待配置写入并重启后再进入正常模式。

    启动最开始会强制校验单 worker 部署（见
    :func:`axile.server.core.single_worker.ensure_single_worker`）。该校验放在
    lifespan 而非仅 CLI，是为了让 ``uvicorn --workers N`` /
    ``gunicorn -w N`` 这类绕过 CLI 的启动方式同样被拦下。
    """
    # 多 worker 会让账户执行互斥锁、调度器与实盘进度镜像（均为进程内内存态）失效，
    # 可能重复调度与重复下单，因此在做任何事之前先拒绝启动，而不是带病运行。
    ensure_single_worker()

    # 把执行实时态广播中枢绑到主事件循环：事件可能经独立线程产生，需 call_soon_threadsafe
    # 回投到这里才能安全投递给 SSE 订阅者（见 axile.server.execution.live）。向导模式下也绑，
    # 无害且让 SSE 端点始终可用。
    live_hub.bind_loop(asyncio.get_running_loop())

    if not is_configured():
        logger.warning("axile 未完成初始化配置，进入初始化向导模式（跳过调度与账户加载）。")
        yield
        return
    from axile.server._native import _core
    from axile.server.core.db import database_path, initialize_database
    from axile.server.native_store import native_call
    from axile.server.performance_analysis import AnalysisManager, ensure_native_capabilities

    ensure_native_capabilities()
    await asyncio.to_thread(initialize_database)
    analysis = AnalysisManager()
    from axile.server.account_control.monitor import monitor, recover_sessions
    from axile.server.connection_state import recover_connection_sessions

    await recover_connection_sessions()
    await recover_sessions()
    monitor.start()
    _load_configured_algorithm_directories()
    _load_configured_algorithm_modules()
    register_china_channel_jobs(scheduler)
    scheduler.start()
    from axile.server.time_check.service import service as time_check_service

    await time_check_service.start(first_run=settings.time_check_first_run)
    from axile.server.execution import notification_outbox
    from axile.server.execution.supplements import recover_supplements

    notification_outbox.recovering = True
    try:
        await recover_supplements()
        await recover_intents_on_startup()
    finally:
        notification_outbox.recovering = False
    await notification_outbox.recover_notifications()
    # 核心服务先就绪；每个账户独立恢复，不等待柜台登录、合约查询或账户限流。
    account_recovery = asyncio.create_task(_recover_accounts_in_background(), name="axile-account-recovery")
    from axile.server.execution.catalog_preheat import arm_catalog_preheat

    arm_catalog_preheat()
    _.state.account_recovery = account_recovery
    try:
        await analysis.start()
        logger.warning("axile已经成功运行!")
        yield
    finally:
        from axile.server.connection_validation import service as validation_service

        account_recovery.cancel()
        await asyncio.gather(account_recovery, return_exceptions=True)
        await analysis.stop()
        await validation_service.close()
        await time_check_service.close()
        await monitor.close()
        scheduler.shutdown()
        await shutdown_dispatchers()
        shutdown_worker_backend_manager()
        await native_call(_core.close_database, database_path())
        logger.warning("axile已经关闭!")


def custom_generate_unique_id(route: APIRoute) -> str:
    """
    生成稳定的 OpenAPI operation id。

    Parameters
    ----------
    route : APIRoute
        当前注册到 FastAPI 的路由对象。

    Returns
    -------
    str
        由首个 tag 与路由名拼接出的 operation id。

    Notes
    -----
    这里显式使用 tag 前缀，避免不同路由模块里重复的处理函数名在 OpenAPI
    文档中发生冲突。
    """
    return f"{route.tags[0]}-{route.name}"


_app = FastAPI(
    title=PROJECT_NAME,
    openapi_url=f"{API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

_app.add_middleware(CompressionMiddleware, minimum_size=4096)


_app.include_router(api_router, prefix=API_V1_STR)


def _validation_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """保留全部字段校验信息，包括 input 与 ctx。"""
    return json_error_value(exc.errors())


@_app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """返回完整请求校验错误，异常类型值转换为原始字符串."""
    errors = _validation_errors(exc)
    logger.error(
        "422 Unprocessable Entity\nURL: {}\nErrors: {}",
        request.url,
        errors,
    )
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": errors}),
    )


# 内嵌前端静态资源目录（由 ./ui 构建产物拷贝至此）。
_STATIC_DIR = Path(__file__).parent / "_static"

# 这些前缀属于后端接口与文档，绝不能被 SPA 回退逻辑接管。
_RESERVED_PREFIXES: tuple[str, ...] = (
    API_V1_STR.strip("/") + "/",
    "docs",
    "redoc",
    "openapi.json",
    "ws/",
)


def _is_reserved_path(path: str) -> bool:
    """
    判断给定路径是否属于后端接口或文档，不应由前端 SPA 接管。

    Parameters
    ----------
    path : str
        去除前导斜杠后的请求路径。

    Returns
    -------
    bool
        命中任一保留前缀时返回 ``True``。
    """
    return path.startswith(_RESERVED_PREFIXES)


def _mount_embedded_frontend() -> None:
    """
    将 ``./ui`` 的构建产物挂载为内嵌实盘前端。

    Notes
    -----
    仅当 ``_static`` 目录存在时才注册前端路由，因此未构建前端时后端 API 行为不变。
    静态资源通过 ``/assets`` 提供，其余未命中后端接口的路径统一回退到
    ``index.html``，交由前端哈希路由处理。
    """
    if not _STATIC_DIR.exists():
        logger.info("未发现内嵌前端构建产物，跳过前端挂载: {}", _STATIC_DIR)
        return

    index_file = _STATIC_DIR / "index.html"
    assets_dir = _STATIC_DIR / "assets"
    if assets_dir.exists():
        _app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @_app.get("/", tags=["frontend"], include_in_schema=False, response_model=None)
    async def serve_index() -> FileResponse:
        """返回内嵌前端入口页面."""
        return FileResponse(index_file)

    @_app.get("/{path:path}", tags=["frontend"], include_in_schema=False, response_model=None)
    async def spa_fallback(path: str) -> FileResponse | JSONResponse:
        """对未命中后端接口的路径回退到前端入口页或静态文件."""
        if _is_reserved_path(path):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        candidate = (_STATIC_DIR / path).resolve()
        if candidate.is_file() and _STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)

        return FileResponse(index_file)

    _ = (serve_index, spa_fallback)  # 由装饰器注册，显式引用以满足静态检查。
    logger.info("已挂载内嵌实盘前端: {}", _STATIC_DIR)


_mount_embedded_frontend()
