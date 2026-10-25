"""账户基础管理相关路由."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from hashlib import sha256
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, HTTPException, Response, status
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from axile.channels import get_channel, list_channels
from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_function import validate_notification_function
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.presets import ensure_account_control_preset_compatible
from axile.executor.algorithms.utils.clock import clock_now
from axile.server._native import _core
from axile.server.api.deps import HistoryPaginationDep, SchedDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.connection_runtime import public_session_views
from axile.server.core.db import analysis_store
from axile.server.cron import SCHEDULER_TIMEZONE, parse_cron_expr
from axile.server.db.models import (
    Account,
    AccountCreate,
    AccountDashboardItemPublic,
    AccountDashboardPublic,
    AccountListPublic,
    AccountNextRunPublic,
    AccountPublic,
    AccountRuntimeSyncPublic,
    AccountUpdate,
    Message,
    PortfolioAccountListPublic,
)
from axile.server.db.models.account_connection import AccountConnectionDetail
from axile.server.db.models.account_validation import _check_algorithm_channel_compat
from axile.server.db.models.base import now_str
from axile.server.execution.account_runtime_sync import reconcile_account_runtime
from axile.server.execution.ctp_channels import drop_account_worker
from axile.server.execution.live import live_hub
from axile.server.execution.registry import (
    get_execution_task_state,
    get_queued_execution_id,
    get_running_execution_id,
)
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.scheduler import delete_job
from axile.server.integrity import plan_executable_target
from axile.server.native_store import native_operation
from axile.server.performance import close_before, local_time
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_moment

router = APIRouter()


class AccountCopyRequest(BaseModel):
    """复制账户时仅接收新名称。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


async def _account_response(account: dict[str, Any], sync: dict[str, Any]) -> AccountPublic:
    """对本次已提交快照复用原生展示规则，保留提交后运行态。"""
    return AccountPublic.model_validate(
        await _account_operation(
            "account_public", {"account": account, "runtime_sync": sync, **_account_read_request()}
        )
    )


def _account_read_request() -> dict[str, object]:
    """只传入渠道白名单与进程会话快照，账户查询直接返回最终 JSON。"""
    return {
        "channel_fields": {
            plugin.descriptor.channel: [field.name for field in plugin.descriptor.account_form.fields]
            for plugin in list_channels()
        },
        "default_notification_hash": sha256(DEFAULT_ACCOUNT_NOTIFICATION_CODE.strip().encode()).hexdigest(),
        "live_sessions": public_session_views(),
    }


def _validate_channel_account_config(channel: TradeChannel | str, config: dict[str, object]) -> dict[str, object]:
    """按渠道模型校验账户配置并返回可直接持久化的规范化结果。"""
    plugin = get_channel(str(channel))
    try:
        validated = plugin.account_config_model.model_validate(config)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False)
        first = errors[0] if errors else {"loc": (), "msg": "账户配置不合法"}
        location = ".".join(str(part) for part in first.get("loc", ()))
        prefix = f"account_config.{location}" if location else "account_config"
        raise ValueError(f"{prefix}: {first.get('msg', '账户配置不合法')}") from exc
    normalized = cast(
        "dict[str, object]",
        validated.model_dump(mode="json", exclude_none=True, exclude={"channel_type"}),
    )
    for field in plugin.descriptor.account_form.fields:
        condition = field.visible_when
        if condition is not None and normalized.get(condition.field) != condition.equals:
            normalized.pop(field.name, None)
    return normalized


def _next_job_run_times(job: Any, *, limit: int = 3) -> list[str]:
    """从 APScheduler 任务真源展开未来触发时间.

    Parameters
    ----------
    job : Any
        APScheduler 任务；须提供 ``next_run_time`` 与 ``trigger``。
    limit : int, optional
        最多返回的触发次数。

    Returns
    -------
    list[str]
        按时间升序排列的 ISO8601 时间；暂停或已结束任务返回空列表。
    """
    current = getattr(job, "next_run_time", None)
    trigger = getattr(job, "trigger", None)
    result: list[str] = []
    while current is not None and len(result) < limit:
        encoded = current.isoformat()
        if not result or result[-1] != encoded:
            result.append(encoded)
        if trigger is None:
            break
        current = trigger.get_next_fire_time(current, current)
    return result


def _next_job_execution_times(job: Any, channel: TradeChannel | str, *, limit: int = 3) -> list[str]:
    """按轻量交易日历过滤未来真正会执行的触发时间。"""
    current = getattr(job, "next_run_time", None)
    trigger = getattr(job, "trigger", None)
    if current is None:
        return []

    result: list[str] = []
    first_day = current.astimezone(SCHEDULER_TIMEZONE).date()
    last_day = first_day + timedelta(days=400)
    while current is not None and len(result) < limit:
        local_time = current.astimezone(SCHEDULER_TIMEZONE)
        calendar_day = local_time.date()
        if calendar_day > last_day:
            break
        decision = evaluate_channel_calendar_moment(channel, local_time)
        if decision.status is not CalendarDecisionStatus.AVAILABLE_CLOSED:
            result.append(current.isoformat())
            if trigger is None:
                break
            current = trigger.get_next_fire_time(current, current)
            continue
        if trigger is None:
            break
        if decision.reason_code == "CALENDAR.SESSION_CLOSED":
            current = trigger.get_next_fire_time(current, current)
            continue
        next_day = datetime.combine(calendar_day + timedelta(days=1), time.min, tzinfo=SCHEDULER_TIMEZONE)
        current = trigger.get_next_fire_time(current, next_day)
    return result


async def _account_operation(name: str, request: dict[str, object]) -> Any:
    """执行完整原生账户操作，并保持路由的错误状态。"""
    try:
        return await native_operation(name, request)
    except _core.NativeError as exc:
        code, message = exc.args[:2]
        status_code = {
            "ACCOUNT_NOT_FOUND": 404,
            "PORTFOLIO_NOT_FOUND": 404,
            "ACCOUNT_CONFLICT": 409,
            "ACCOUNT_SYNC_PENDING": 202,
            "ACCOUNT_INPUT": 422,
        }.get(code, 500)
        raise HTTPException(status_code, detail=str(message)) from exc


def _resolve_next_account_control_binding(
    db_account: Account,
    account: AccountUpdate,
) -> tuple[TradeChannel, str]:
    """
    解析更新后生效的交易渠道与账户控制 preset.

    Parameters
    ----------
    db_account : Account
        当前数据库中的账户对象。
    account : AccountUpdate
        本次更新请求。

    Returns
    -------
    tuple[TradeChannel, str]
        更新后生效的交易渠道与账户控制 preset。
    """
    next_trade_channel = db_account.trade_channel if account.trade_channel is None else account.trade_channel
    next_preset = db_account.account_control_preset
    if account.account_control_preset is not None:
        next_preset = account.account_control_preset
    return next_trade_channel, next_preset


def _build_account_update_data(account: AccountUpdate) -> dict[str, object]:
    """
    将账户更新请求转换为可直接持久化的字段映射.

    Parameters
    ----------
    account : AccountUpdate
        本次更新请求。

    Returns
    -------
    dict[str, object]
        已完成特殊字段规范化的更新载荷。
    """
    data = account.model_dump(exclude_unset=True, mode="json")
    if data.get("portfolio_id") == -1:
        data["portfolio_id"] = None
    return data


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=AccountPublic,
)
async def create_account(
    sched: SchedDep,
    account: AccountCreate,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AccountPublic:
    """创建账户."""
    try:
        if idempotency_key:
            existing = await _account_operation("account_create_replay", {"idempotency_key": idempotency_key})
            if existing is not None:
                sync = existing["runtime_sync"]
                if sync["status"] != "synchronized":
                    response.status_code = status.HTTP_202_ACCEPTED
                return await _account_response(existing, sync)
        ensure_account_control_preset_compatible(account.account_control_preset, account.trade_channel)
        account.futures_close_policy.validate_channel(account.trade_channel)
        account_config = _validate_channel_account_config(account.trade_channel, account.account_config)
        _check_algorithm_channel_compat(account.algorithm, str(account.trade_channel), "下单算法")
        _check_algorithm_channel_compat(account.empty_positions_algorithm, str(account.trade_channel), "清仓算法")
        if account.execution_notification_code and account.execution_notification_code.strip():
            validate_notification_function(account.execution_notification_code)
        parse_cron_expr(account.cron_expr)
        code = account.execution_notification_code
        if code is not None and not code.strip():
            code = None
        if code is None and account.feishu_key and "execution_notification_code" not in account.model_fields_set:
            code = DEFAULT_ACCOUNT_NOTIFICATION_CODE
        saved = await _account_operation(
            "account_create",
            {
                "account": account.model_dump(mode="json")
                | {"account_config": account_config, "execution_notification_code": code},
                "idempotency_key": idempotency_key,
                "now": now_str(),
            },
        )
        db_account = saved["account"]
        if saved["replayed"]:
            sync = saved["account"]["runtime_sync"]
            if sync["status"] != "synchronized":
                response.status_code = status.HTTP_202_ACCEPTED
            return await _account_response(db_account, sync)
        from axile.server.connection_events import connection_hub

        connection_hub.changed(db_account["id"])
    except ValueError as exc:
        logger.exception(f"创建账户失败: {exc}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"创建账户失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(exc)}",
        ) from exc

    from axile.server.execution.notification_outbox import wake_notifications
    from axile.server.execution.supplements import reap_cancelled_queue

    await reap_cancelled_queue(db_account["id"])
    wake_notifications(db_account["id"])
    sync = await _reconcile_committed_runtime(sched, db_account["id"])
    if sync["status"] != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return await _account_response(db_account, sync)


@router.post("/{account_id}/copy", status_code=status.HTTP_201_CREATED, response_model=AccountPublic)
async def copy_account(
    sched: SchedDep,
    account_id: int,
    request: AccountCopyRequest,
    response: Response,
) -> AccountPublic:
    """在来源账户完全暂停后复制配置与初始组合绑定。"""
    async with account_runtime_lock(account_id):
        if not request.name.strip():
            raise HTTPException(status_code=422, detail="新账户名称不能为空")
        source = await _get_account_or_404(account_id)
        if source.is_started:
            raise HTTPException(status_code=409, detail="请先暂停来源账户的自动调度")
        if sched.get_job(str(account_id)) is not None:  # type: ignore[no-untyped-call]
            raise HTTPException(status_code=409, detail="来源账户的调度任务尚未移除，请稍后重试")
        if get_running_execution_id(account_id) or get_queued_execution_id(account_id):
            raise HTTPException(status_code=409, detail="来源账户仍有执行中或排队中的任务，请等待结束")

        try:
            payload = AccountCreate.model_validate(
                source.model_dump(
                    exclude={"id", "created_at", "updated_at", "copied_from_account_id", "copied_from_account_name"}
                )
                | {"name": request.name.strip(), "is_started": False}
            )
            ensure_account_control_preset_compatible(payload.account_control_preset, payload.trade_channel)
            config = _validate_channel_account_config(payload.trade_channel, payload.account_config)
            _check_algorithm_channel_compat(payload.algorithm, str(payload.trade_channel), "下单算法")
            _check_algorithm_channel_compat(payload.empty_positions_algorithm, str(payload.trade_channel), "清仓算法")
            parse_cron_expr(payload.cron_expr)
            copied = await _account_operation(
                "account_copy",
                {
                    "account_id": account_id,
                    "name": request.name.strip(),
                    "account_config": config,
                    "expected_revision": source.runtime_sync.revision if source.runtime_sync is not None else None,
                    "now": now_str(),
                },
            )
            from axile.server.connection_events import connection_hub

            connection_hub.changed(copied["id"])
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    sync = await _reconcile_committed_runtime(sched, copied["id"])
    if sync["status"] != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return await _account_response(copied, sync)


@router.get("/", response_model=AccountListPublic)
async def list_accounts() -> Response:
    """获取所有账户列表."""
    return Response(
        content=await _account_operation("account_list_json", _account_read_request()), media_type="application/json"
    )


def _safe_total_asset(assets: dict[str, Any]) -> float:
    """读取持仓计算需要的实时总权益。"""
    try:
        return float(assets.get("total_asset") or 0.0)
    except (TypeError, ValueError):
        return 0.0


@router.get("/dashboard", response_model=AccountDashboardPublic)
async def account_dashboard(sched: SchedDep) -> AccountDashboardPublic:
    """
    仪表盘聚合: 一次性返回每个账户的舰队卡所需数据.

    Parameters
    ----------
    sched : SchedDep
        当前调度器实例。

    Returns
    -------
    AccountDashboardPublic
        每个账户一项的聚合列表。

    Notes
    -----
    当前金额、持仓与偏离来自有效资产观测；日收益与曲线来自已发布历史绩效。
    ``previous_close`` 是该观测日之前的绩效日末，供卡片用当前权益算日涨跌。
    执行记录、资产、目标与绩效均批量读取，避免逐账户查询。
    """
    context = await _account_operation(
        "account_dashboard_context",
        {"logic_version": LOGIC_VERSION, "engine_version": ENGINE_VERSION, "live_sessions": public_session_views()},
    )
    inputs = context["inputs"]
    items: list[AccountDashboardItemPublic] = []
    for account in context["accounts"]:
        account_id = cast("int", account["id"])
        persisted = inputs[str(account_id)]
        latest_snapshot = persisted["observation"]
        latest = persisted["latest"]
        assets = latest_snapshot["assets"] if latest_snapshot is not None else {}
        raw_positions = assets.get("positions")
        positions: list[Any] = raw_positions if isinstance(raw_positions, list) else []
        holdings_count = len(positions) if isinstance(raw_positions, list) else None
        valuation_complete = isinstance(raw_positions, list) and all(
            isinstance(pos, dict) and isinstance(pos.get("market_value"), (int, float)) for pos in positions
        )
        position_weights = (
            sorted((abs(float(pos["market_value"])) for pos in positions), reverse=True)[:12]
            if valuation_complete
            else []
        )
        plugin = get_channel(str(account["trade_channel"]))
        currency = plugin.descriptor.currency

        job = sched.get_job(str(account_id))  # type: ignore[no-untyped-call]
        next_run = getattr(job, "next_run_time", None) if job is not None else None

        # 在途快照：优先 live hub 的 queued/running/terminating 帧（含阶段），
        # 没有在途帧时退回内存槽位。刚结束的终态帧不填 running_execution_id。
        progress = live_hub.progress_for(account_id)
        if progress is not None and progress.get("status") not in {"queued", "running", "terminating"}:
            progress = None
        running_execution_id = get_running_execution_id(account_id)
        queued_execution_id = get_queued_execution_id(account_id)
        running_kind: str | None = None
        running_phase: str | None = None
        running_status: str | None = None
        pending_execution_id: str | None = None
        pending_kind: str | None = None
        if progress is not None:
            running_execution_id = cast("str", progress.get("execution_id"))
            running_kind = cast("str | None", progress.get("kind"))
            running_phase = cast("str | None", progress.get("phase"))
            running_status = cast("str | None", progress.get("status"))
            pending_execution_id = cast("str | None", progress.get("pending_execution_id"))
            pending_kind = cast("str | None", progress.get("pending_kind"))
        elif running_execution_id is not None:
            task_state = get_execution_task_state(running_execution_id)
            running_kind = task_state.execution_kind.value if task_state is not None else None
            running_status = (
                "terminating" if task_state is not None and task_state.status.value == "TERMINATING" else "running"
            )
            running_phase = "triggered"
            pending_execution_id = queued_execution_id
            if queued_execution_id is not None:
                queued_state = get_execution_task_state(queued_execution_id)
                pending_kind = queued_state.execution_kind.value if queued_state is not None else None
        elif queued_execution_id is not None:
            task_state = get_execution_task_state(queued_execution_id)
            running_execution_id = queued_execution_id
            running_kind = task_state.execution_kind.value if task_state is not None else None
            running_status = "queued"
            running_phase = "queued"

        last_output_status = None if latest is None else latest["display_status"]
        performance = persisted["performance"]
        previous_close = (
            close_before(performance["points"], local_time(latest_snapshot["created_at"]).date().isoformat())
            if latest_snapshot is not None
            else None
        )
        target_weights = persisted["target_weights"]
        off_symbol_count = None
        if latest_snapshot is not None and valuation_complete and isinstance(target_weights, dict):
            off_symbol_count = plan_executable_target(
                positions,
                target_weights,
                _safe_total_asset(assets),
                canonicalize_symbol=plugin.canonicalize_symbol,
                quantize_target_quantity=plugin.quantize_target_quantity,
            ).off_symbol_count

        items.append(
            AccountDashboardItemPublic(
                account_id=account_id,
                name=account["name"],
                remark=account["remark"],
                market=account["market"],
                trade_channel=account["trade_channel"],
                is_started=account["is_started"],
                portfolio_id=persisted["portfolio_id"],
                is_scheduled=job is not None,
                next_run_time=next_run.isoformat() if next_run is not None else None,
                total_asset=_safe_total_asset(assets),
                currency=str(currency),
                holdings_count=holdings_count,
                position_weights=position_weights,
                performance=performance,
                connection=account["connection"],
                asset_observed_at=latest_snapshot["created_at"] if latest_snapshot is not None else None,
                previous_close=previous_close,
                last_is_success=latest["is_success"] if latest is not None else None,
                last_exec_at=latest["created_at"] if latest is not None else None,
                last_output_status=last_output_status,
                off_symbol_count=off_symbol_count,
                running_execution_id=running_execution_id,
                running_kind=running_kind,
                running_phase=running_phase,
                running_status=running_status,
                pending_execution_id=pending_execution_id,
                pending_kind=pending_kind,
            )
        )

    return AccountDashboardPublic(data=items)


@router.delete("/{account_id}")
async def delete_account(sched: SchedDep, account_id: int) -> Message:
    """删除账户."""
    # 与对齐互斥，先等锁再读库，避免删除期间旧对齐把 worker 重新建立。
    async with account_runtime_lock(account_id):
        await _account_operation("account_delete", {"account_id": account_id})
        delete_job(sched, account_id)
        await drop_account_worker(account_id)
        from axile.server.connection_runtime import forget_session

        forget_session(account_id)
    return Message(message="成功删除账户")


@router.get(
    "/{account_id}",
    response_model=AccountPublic,
)
async def account_info(account_id: int) -> Response:
    """获取账户详情."""
    return Response(
        content=await _account_operation("account_info_json", _account_read_request() | {"account_id": account_id}),
        media_type="application/json",
    )


@router.get("/{account_id}/connection", response_model=AccountConnectionDetail)
async def account_connection(account_id: int) -> Response:
    """只读取最新连接结果及完整详情，不触发 SDK。"""
    return Response(
        content=await _account_operation(
            "account_connection_json", _account_read_request() | {"account_id": account_id}
        ),
        media_type="application/json",
    )


@router.get(
    "/{account_id}/next_run_time",
    response_model=AccountNextRunPublic,
)
async def account_next_run_time(
    sched: SchedDep,
    account_id: int,
) -> AccountNextRunPublic:
    """
    获取账户未来三次调度执行时间.

    Parameters
    ----------
    sched : SchedDep
        当前调度器实例。
    account_id : int
        账户 ID。

    Returns
    -------
    AccountNextRunPublic
        账户的调度状态与未来触发时间。

    Raises
    ------
    HTTPException
        账户不存在时抛出 404。

    Notes
    -----
    首次时间直接读取 APScheduler 的 ``next_run_time``，后续时间从同一任务 trigger
    继续展开。
    """
    context = await _account_operation("account_next_run_context", {"account_id": account_id})
    account = Account.model_validate(context["account"])
    job = sched.get_job(str(account_id))  # type: ignore[no-untyped-call]
    next_run_times = _next_job_run_times(job) if job is not None else []
    next_execution_times = _next_job_execution_times(job, account.trade_channel) if job is not None else []
    if account.supplement and job is not None:
        from axile.server.api.routes.account_schedule import SchedulePreviewRequest, schedule_preview

        preview = await schedule_preview(
            SchedulePreviewRequest(
                trade_channel=account.trade_channel,
                cron_expr=account.cron_expr,
                supplement=account.supplement,
                limit=30,
            )
        )
        now = clock_now(tz=SCHEDULER_TIMEZONE)
        times = {
            item.scheduled_at
            for item in preview.items
            if item.action == "execute" and (item.base_scheduled_at is None or item.base_scheduled_at >= now)
        }
        times.update(
            datetime.fromisoformat(scheduled_at)
            for scheduled_at in context["pending_scheduled_at"]
            if datetime.fromisoformat(scheduled_at) > now
        )
        next_execution_times = [value.isoformat() for value in sorted(times)[:3]]
    return AccountNextRunPublic(
        account_id=account_id,
        is_scheduled=job is not None,
        next_run_time=next_run_times[0] if next_run_times else None,
        next_run_times=next_run_times,
        next_execution_times=next_execution_times,
    )


@router.patch(
    "/{account_id}",
    response_model=AccountPublic,
)
async def update_account(
    sched: SchedDep,
    account_id: int,
    account: AccountUpdate,
    response: Response,
) -> AccountPublic:
    """更新账户."""
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        return await _update_account(sched, account_id, account, response)


async def _update_account(
    sched: SchedDep,
    account_id: int,
    account: AccountUpdate,
    response: Response,
) -> AccountPublic:
    """更新账户."""
    db_account = await _get_account_or_404(account_id)

    try:
        # 账户控制 binding 要按“更新后的目标状态”校验，不能只看当前库里的旧值。
        next_trade_channel, next_preset = _resolve_next_account_control_binding(db_account, account)
        ensure_account_control_preset_compatible(next_preset, next_trade_channel)
        # 同渠道 PATCH 保留未提交的凭证；切换渠道则以新配置替换，避免旧渠道
        # 专属字段混入新模型校验。
        next_account_config = (
            db_account.account_config
            if account.account_config is None
            else account.account_config
            if next_trade_channel != db_account.trade_channel
            else {**db_account.account_config, **account.account_config}
        )
        normalized_account_config = _validate_channel_account_config(next_trade_channel, next_account_config)
        # 算法与渠道按"更新后的目标状态"配对校验:PATCH 可能只改其一,必须取生效后的组合。
        next_close_policy = account.futures_close_policy or db_account.futures_close_policy
        next_close_policy.validate_channel(next_trade_channel)
        next_algorithm = account.algorithm if account.algorithm is not None else db_account.algorithm
        next_empty_algorithm = (
            account.empty_positions_algorithm
            if account.empty_positions_algorithm is not None
            else db_account.empty_positions_algorithm
        )
        _check_algorithm_channel_compat(next_algorithm, str(next_trade_channel), "下单算法")
        _check_algorithm_channel_compat(next_empty_algorithm, str(next_trade_channel), "清仓算法")

        data = _build_account_update_data(account)
        if "supplement" in data and data["supplement"] is not None and "cron_expr" not in data:
            raise ValueError("启用或修改补发时必须同时提交基础 cron_expr")
        if (
            isinstance(data.get("execution_notification_code"), str)
            and not str(data["execution_notification_code"]).strip()
        ):
            data["execution_notification_code"] = None
        if (
            data.get("feishu_key")
            and not db_account.feishu_key
            and not db_account.execution_notification_code
            and "execution_notification_code" not in data
        ):
            data["execution_notification_code"] = DEFAULT_ACCOUNT_NOTIFICATION_CODE
        next_notification_code = data.get("execution_notification_code", db_account.execution_notification_code)
        if next_notification_code:
            validate_notification_function(str(next_notification_code))
        if account.account_config is not None or account.trade_channel is not None:
            data["account_config"] = normalized_account_config
        Account.model_validate(db_account.model_dump() | data)
        saved = await _account_operation(
            "account_update",
            {
                "account_id": account_id,
                "patch": data,
                "expected_revision": db_account.runtime_sync.revision if db_account.runtime_sync is not None else None,
                "now": now_str(),
            },
        )
        db_account = saved["account"]
        if saved["runtime_changed"]:
            from axile.server.connection_events import connection_hub

            connection_hub.changed(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(exc)}",
        ) from exc

    from axile.server.execution.notification_outbox import wake_notifications
    from axile.server.execution.supplements import reap_cancelled_queue

    await reap_cancelled_queue(db_account["id"])
    wake_notifications(db_account["id"])
    sync = await _reconcile_committed_runtime(sched, db_account["id"])
    if sync["status"] != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return await _account_response(db_account, sync)


@router.get("/{account_id}/runtime-sync", response_model=AccountRuntimeSyncPublic)
async def account_runtime_sync_status(account_id: int) -> Response:
    """查询账户运行态对齐的失败原因与重试计数。"""
    return Response(
        content=await _account_operation("account_runtime_sync_json", {"account_id": account_id}),
        media_type="application/json",
    )


@router.post("/{account_id}/runtime-sync/retry", response_model=AccountRuntimeSyncPublic)
async def retry_account_runtime_sync(
    sched: SchedDep,
    account_id: int,
    response: Response,
) -> AccountRuntimeSyncPublic:
    """显式重试账户 scheduler 和渠道 worker 的运行态对齐。"""
    sync = await _reconcile_committed_runtime(sched, account_id)
    if sync["status"] != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return AccountRuntimeSyncPublic.model_validate(sync)


async def _reconcile_committed_runtime(sched: SchedDep, account_id: int) -> dict[str, Any]:
    """已提交配置在事务外对齐；等锁期间被删除的账户仍返回 404。"""
    try:
        return await reconcile_account_runtime(account_id, sched)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账户不存在") from exc


@router.get("/execute_records/{account_id}")
async def list_execute_records(
    account_id: int,
    pagination: HistoryPaginationDep,
    execution_id: str | None = None,
    record_id: int | None = None,
) -> Response:
    """直接返回原生执行摘要页，不读取完整执行证据。"""
    try:
        content = await run_in_threadpool(
            _core.execution_records,
            analysis_store(),
            account_id,
            pagination.skip,
            pagination.limit,
            execution_id,
            record_id,
        )
        return Response(content=content, media_type="application/json")
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        raise HTTPException(503, detail={"code": error.args[0], "message": error.args[1]}) from error


@router.get("/execute_records/{account_id}/{record_id}")
async def execute_record_detail(account_id: int, record_id: int) -> Response:
    """从同一原生读快照返回账户内执行的完整业务证据。"""
    try:
        content = await run_in_threadpool(_core.execution_record_detail, analysis_store(), account_id, record_id)
        return Response(content=content, media_type="application/json")
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        if error.args[0] == "RECORD_NOT_FOUND":
            raise HTTPException(404, detail="执行记录不存在") from error
        raise HTTPException(503, detail={"code": error.args[0], "message": error.args[1]}) from error


@router.get("/portfolio_records/{account_id}", response_model=PortfolioAccountListPublic)
async def list_portfolio_records(
    account_id: int,
    pagination: HistoryPaginationDep,
) -> Response:
    """获取账户的组合更换记录."""
    return Response(
        content=await _account_operation(
            "account_portfolio_records_json",
            {"account_id": account_id, "skip": pagination.skip, "limit": pagination.limit},
        ),
        media_type="application/json",
    )


@router.delete("/execute_records/{account_id}", response_model=Message)
async def delete_execute_records(account_id: int) -> Message:
    """删除账户的执行记录."""
    await _account_operation("delete_execution_history", {"account_id": account_id})
    return Message(message="成功删除")
