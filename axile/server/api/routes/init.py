"""首启初始化向导相关路由.

Notes
-----
本模块提供「未完成配置」时初始化向导所需的接口：查询就绪状态、测试数据库
连通性、写入 ``config.toml`` 并触发重启。初始化接口不触达业务数据库，
因此在服务端处于初始化向导模式（数据库尚未迁移）时也能正常工作。
"""

import asyncio
import os
import signal
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from loguru import logger
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from axile.common.config import (
    CONFIG_TOML_PATH,
    SqliteDsn,
    is_configured,
    settings,
    update_config_toml_values,
    write_config_toml,
)
from axile.common.default_system_notification import DEFAULT_SYSTEM_NOTIFICATION_CODE
from axile.common.notification_config import validate_notification_config
from axile.common.notification_function import run_notification_function
from axile.executor.algorithms.utils.clock import clock_now, get_default_clock
from axile.server.db.models.system_notification import SystemNotificationStatePublic
from axile.server.error_notifications import build_system_notification_context, build_test_card
from axile.server.execution.notification_state import get_system_notification_result
from axile.server.native_store import native_operation

router = APIRouter(prefix="/init", tags=["init"])

_DSN_ADAPTER: TypeAdapter[SqliteDsn] = TypeAdapter(SqliteDsn)
_HTTP_TIMEOUT_SECONDS = 15
_RESTART_DELAY_SECONDS = 0.5
# 飞书自定义机器人 webhook 前缀；测试推送走与真实告警一致的地址契约。
_FEISHU_HOOK_BASE = "https://open.feishu.cn/open-apis/bot/v2/hook/"


class InitStatus(BaseModel):
    """初始化就绪状态与预填值.

    Attributes
    ----------
    configured : bool
        是否已完成初始化配置（``config.toml`` 存在）。
    environment : str
        当前运行环境标识。
    values : dict[str, Any]
        向导各字段的预填值（取自当前有效配置）。
    """

    configured: bool
    environment: str
    values: dict[str, Any]


class DbTestRequest(BaseModel):
    """数据库连通性测试载荷."""

    uri: str


class FeishuTestRequest(BaseModel):
    """执行告警飞书机器人连通性测试载荷."""

    key: str | None = None


class NotificationFunctionTestRequest(BaseModel):
    """系统通知函数试跑请求。"""

    code: str
    key: str | None = None
    event_type: Literal["execution_error", "execution_timeout", "account_control.warning"] = "execution_error"


class ExecutionAlertUpdateRequest(BaseModel):
    """系统级执行错误告警配置更新载荷."""

    model_config = ConfigDict(extra="forbid")

    exe_err_feishu_key: str | None = None
    system_execution_notification_mode: Literal["default", "function"] = "default"
    system_execution_notification_code: str | None = None


class TestResult(BaseModel):
    """连通性测试或保存操作的结果.

    Attributes
    ----------
    ok : bool
        是否成功。
    message : str
        面向用户的结果说明。
    """

    ok: bool
    message: str


class NotificationFunctionTestResult(TestResult):
    """函数试跑的源码错误位置，与账户通知编辑器使用同一反馈契约。"""

    error_line: int | None = None
    traceback: str | None = None


class InitSaveRequest(BaseModel):
    """初始化向导保存载荷；凭证省略或为 None 时保留现值，告警空串则清除。"""

    model_config = ConfigDict(extra="forbid")

    sqlalchemy_database_uri: str | None = None
    exe_err_feishu_key: str | None = None
    system_execution_notification_mode: Literal["default", "function"] | None = None
    system_execution_notification_code: str | None = None
    environment: Literal["local", "staging", "production"] = "local"
    app_log_dir: str = "./logs"
    axile_log_rotation: str = "1 day"
    algorithm_modules: list[str] = []
    algorithm_directories: list[str] = []


def _prefill_values() -> dict[str, Any]:
    """
    汇总向导各字段的预填值.

    Returns
    -------
    dict[str, Any]
        取自当前有效配置（``config.toml`` / 默认值）。

    数据库 URI 与 webhook key 均可能携带凭证；状态接口只公开其已配置状态。
    """
    return {
        "sqlalchemy_database_configured": bool(settings.sqlalchemy_database_uri),
        "exe_err_feishu_configured": bool(settings.exe_err_feishu_key),
        "system_execution_notification_mode": settings.system_execution_notification_mode,
        "system_execution_notification_code": settings.system_execution_notification_code,
        "environment": settings.environment,
        "app_log_dir": str(settings.app_log_dir),
        "axile_log_rotation": settings.axile_log_rotation,
        "algorithm_modules": settings.algorithm_modules,
        "algorithm_directories": settings.algorithm_directories,
    }


@router.get("/status")
def init_status() -> InitStatus:
    """返回初始化就绪状态与向导预填值."""
    return InitStatus(
        configured=is_configured(),
        environment=settings.environment,
        values=_prefill_values(),
    )


@router.post("/test-db")
async def test_db(payload: DbTestRequest) -> TestResult:
    """
    测试数据库连通性.

    Notes
    -----
    由原生层用给定地址临时建立连接并执行 ``SELECT 1``，结束后关闭连接。
    对 SQLite 而言该操作可能顺带创建库文件（即目标库位置），属预期副作用。
    """
    try:
        path, options = _database_probe_target(payload.uri)
        await native_operation("database_probe", options, database=path)
    except Exception as exc:  # noqa: BLE001 - 连接异常统一转为失败结果
        return TestResult(ok=False, message=f"数据库连接失败：{exc}")
    return TestResult(ok=True, message="数据库连接成功")


def _sqlite_boolean(value: str) -> bool:
    """沿用 SQLite DSN 查询参数接受的布尔拼写。"""
    normalized = value.strip().lower()
    if normalized not in {"true", "yes", "on", "y", "t", "1", "false", "no", "off", "n", "f", "0"}:
        raise ValueError(f"无效的布尔参数：{value}")
    return normalized in {"true", "yes", "on", "y", "t", "1"}


def _database_probe_target(uri: str) -> tuple[str, dict[str, Any]]:
    """保留 SQLite 文件名及原生 URI 参数，不打开配置中的业务库。"""
    _DSN_ADAPTER.validate_python(uri)
    parsed = urlsplit(uri, allow_fragments=False)
    if parsed.netloc or not uri.startswith("sqlite+aiosqlite://"):
        raise ValueError("SQLite 地址不能包含主机或认证信息")
    query = dict(parse_qsl(parsed.query))
    use_uri = _sqlite_boolean(query.pop("uri", "false"))
    timeout = max(float(query.pop("timeout", "5")), 0)
    if "check_same_thread" in query:
        _sqlite_boolean(query.pop("check_same_thread"))
    for key in ("detect_types", "cached_statements"):
        if key in query:
            int(query.pop(key))
    isolation = query.pop("isolation_level", "")
    if isolation.upper() not in {"", "DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
        raise ValueError("无效的 SQLite isolation_level")
    path = parsed.path.removeprefix("/")
    if use_uri:
        if not path:
            raise ValueError("SQLite URI 缺少数据库路径")
        if query:
            path += "?" + "&".join(f"{key}={value}" for key, value in sorted(query.items()))
    else:
        path = path or ":memory:"
        if path != ":memory:":
            path = os.path.abspath(path)
    return path, {"uri": use_uri, "timeout": timeout}


@router.post("/test-feishu")
async def test_feishu(payload: FeishuTestRequest) -> TestResult:
    """
    测试执行告警飞书机器人是否连通.

    Notes
    -----
    向飞书自定义机器人 webhook 推送一张**联通测试卡片**（:func:`~axile.server.
    error_notifications.build_test_card`），走与真实执行错误告警（``send_feishu_error``）
    一致的 HTTP 契约：同一 hook 地址、``msg_type=interactive``
    信封、同构卡片，因此测试通过即代表真实告警链路可用。飞书对逻辑失败仍返回 HTTP 200，
    故以响应体 ``code == 0`` 或 ``StatusMessage == "success"`` 判定成功；测试失败以
    ``ok=False`` 返回（HTTP 200），便于前端统一展示 ✓/✗。
    """
    key = (settings.exe_err_feishu_key if payload.key is None else payload.key).strip()
    if not key:
        return TestResult(ok=False, message="请先填写飞书机器人 key")

    data = {"msg_type": "interactive", "card": build_test_card()}
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{_FEISHU_HOOK_BASE}{key}", json=data) as res:
                result = await res.json()
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一转为失败结果
        return TestResult(ok=False, message=f"推送失败：{exc}")

    ok = isinstance(result, dict) and (result.get("code") == 0 or result.get("StatusMessage") == "success")
    if ok:
        return TestResult(ok=True, message="测试卡片已发送，请在飞书群确认是否收到。")
    detail = (
        str(result.get("msg") or result.get("StatusMessage") or result) if isinstance(result, dict) else str(result)
    )
    return TestResult(ok=False, message=f"飞书返回失败：{detail}")


@router.patch("/execution-alert")
def update_execution_alert(payload: ExecutionAlertUpdateRequest) -> TestResult:
    """
    保存系统级执行错误告警配置并立即生效.

    Parameters
    ----------
    payload : ExecutionAlertUpdateRequest
        新的飞书机器人 key；空串表示关闭推送。

    Returns
    -------
    TestResult
        保存成功时返回立即生效提示。

    Raises
    ------
    HTTPException
        系统尚未完成初始化时返回 409；配置落盘失败时返回 500。

    Notes
    -----
    先持久化再更新进程内配置，确保写盘失败时当前告警行为不发生变化。执行失败链路
    在发送告警时读取同一个 ``settings`` 实例，因此无需重启服务。
    """
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="系统尚未完成初始化，请先完成初始化配置",
        )

    code = (
        settings.system_execution_notification_code
        if payload.system_execution_notification_code is None
        else payload.system_execution_notification_code
    )
    try:
        validate_notification_config(payload.system_execution_notification_mode, code)
        next_feishu_key = (
            settings.exe_err_feishu_key if payload.exe_err_feishu_key is None else payload.exe_err_feishu_key
        )
        update_config_toml_values(
            {
                "exe_err_feishu_key": next_feishu_key,
                "system_execution_notification_mode": payload.system_execution_notification_mode,
                "system_execution_notification_code": code,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入配置文件失败：{exc}",
        ) from exc

    settings.exe_err_feishu_key = next_feishu_key
    settings.system_execution_notification_mode = payload.system_execution_notification_mode
    settings.system_execution_notification_code = code
    logger.info("执行错误告警配置已更新并立即生效。")
    return TestResult(ok=True, message="执行告警配置已保存并立即生效。")


@router.get("/execution-alert/function/default")
def default_system_notification_function() -> dict[str, str]:
    """返回不含凭据的系统告警示例源码。"""
    return {"code": DEFAULT_SYSTEM_NOTIFICATION_CODE}


@router.get("/execution-alert/result")
async def system_notification_result() -> SystemNotificationStatePublic:
    """仅已初始化服务读取真实结果，首启向导无需连接业务数据库。"""
    if not is_configured():
        raise HTTPException(status_code=409, detail="系统尚未完成初始化")
    return await asyncio.to_thread(get_system_notification_result)


@router.post("/execution-alert/function/test")
async def test_system_notification_function(payload: NotificationFunctionTestRequest) -> NotificationFunctionTestResult:
    """运行当前草稿，使用当前凭据选择；不会保存配置或覆盖真实告警摘要。"""
    error = RuntimeError("样例执行超时" if payload.event_type == "execution_timeout" else "样例执行异常")
    context = build_system_notification_context(error, None, event_type=payload.event_type, is_test=True)
    context["account"] = {"id": 0, "name": "样例账户"}
    if payload.event_type == "account_control.warning":
        context["error"] = None
        context["quota"] = {
            "operation": "place_order",
            "control_date": clock_now(tz=ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "timezone": "Asia/Shanghai",
            "used": 400,
            "limit": 500,
            "warning_percent": 80,
            "threshold_count": 400,
            "remaining": 100,
            "source": "axile_local_allowed",
        }
    key = settings.exe_err_feishu_key if payload.key is None else payload.key
    result = await asyncio.to_thread(run_notification_function, payload.code, context, system_feishu_key=key)
    return NotificationFunctionTestResult(
        ok=result.ok,
        message="样例函数运行成功" if result.ok else result.error or "运行失败",
        error_line=result.error_line,
        traceback=getattr(result, "traceback", None),
    )


def _restart_process() -> None:
    """向自身发送 ``SIGTERM`` 触发退出，交由外部 supervisor 拉起以重读配置."""
    # 稍作延迟，确保保存响应已回发给前端后再退出。
    get_default_clock().sleep(_RESTART_DELAY_SECONDS)
    logger.warning("初始化完成，进程即将退出以便重启进入正常模式。")
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/save")
def init_save(payload: InitSaveRequest, background_tasks: BackgroundTasks) -> TestResult:
    """
    校验并写入初始化配置，随后触发进程重启.

    Parameters
    ----------
    payload : InitSaveRequest
        向导收集的完整配置。
    background_tasks : BackgroundTasks
        用于在响应回发后再触发进程退出。

    Returns
    -------
    TestResult
        保存结果；成功时服务将随即退出并由 supervisor（systemd ``Restart=always``）
        拉起，重读 ``config.toml`` 进入正常模式。

    Raises
    ------
    HTTPException
        数据库地址不合法时返回 422；写入文件失败时返回 500。
    """
    mode = payload.system_execution_notification_mode or settings.system_execution_notification_mode
    code = (
        settings.system_execution_notification_code
        if payload.system_execution_notification_code is None
        else payload.system_execution_notification_code
    )
    try:
        validate_notification_config(mode, code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database_uri = (
        payload.sqlalchemy_database_uri
        if payload.sqlalchemy_database_uri is not None
        else str(settings.sqlalchemy_database_uri)
    )
    feishu_key = payload.exe_err_feishu_key if payload.exe_err_feishu_key is not None else settings.exe_err_feishu_key
    try:
        _DSN_ADAPTER.validate_python(database_uri)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"数据库地址不合法：{exc}",
        ) from exc

    values: dict[str, Any] = {
        "sqlalchemy_database_uri": database_uri,
        "exe_err_feishu_key": feishu_key,
        "system_execution_notification_mode": mode,
        "system_execution_notification_code": code,
        "environment": payload.environment,
        "app_log_dir": payload.app_log_dir,
        "axile_log_rotation": payload.axile_log_rotation,
        "algorithm_modules": payload.algorithm_modules,
        "algorithm_directories": payload.algorithm_directories,
    }
    values["time_check_first_run"] = not is_configured() or settings.time_check_first_run
    try:
        write_config_toml(values)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入配置文件失败：{exc}",
        ) from exc

    logger.warning("初始化配置已写入 {}，即将重启进入正常模式。", CONFIG_TOML_PATH)
    background_tasks.add_task(_restart_process)
    return TestResult(ok=True, message="配置已保存，服务正在重启……")
