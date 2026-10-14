"""账户基础管理相关路由."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, HTTPException, Response, status
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import and_, col, delete, desc, func, select

from axile.channels import get_channel
from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_function import validate_notification_function
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.api.deps import HistoryPaginationDep, SchedDep, SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.cron import SCHEDULER_TIMEZONE, parse_cron_expr
from axile.server.db.models import (
    Account,
    AccountAssetSnapshot,
    AccountCreate,
    AccountDashboardItemPublic,
    AccountDashboardPublic,
    AccountListPublic,
    AccountNextRunPublic,
    AccountPublic,
    AccountRuntimeSync,
    AccountRuntimeSyncPublic,
    AccountUpdate,
    ExecuteRecord,
    ExecuteRecordListPublic,
    ExecuteRecordPublic,
    Message,
    PortfolioAccount,
    PortfolioAccountListPublic,
    PortfolioAccountPublic,
)
from axile.server.db.models.account_validation import _check_algorithm_channel_compat
from axile.server.db.models.performance import PerformanceSummary
from axile.server.execution.account_runtime_sync import enqueue_account_runtime_sync, reconcile_account_runtime
from axile.server.execution.ctp_channels import drop_account_worker
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.execution.live import live_hub
from axile.server.execution.registry import (
    execution_record_output_status,
    get_execution_task_state,
    get_queued_execution_id,
    get_running_execution_id,
)
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.scheduler import delete_job
from axile.server.integrity import plan_executable_target
from axile.server.performance import close_before, local_time
from axile.server.performance_analysis import read_performance_summaries
from axile.server.repositories import (
    add_record_portfolio_account,
    get_portfolios_every_account,
    get_recent_account_asset_snapshots_for_accounts,
    get_recent_execute_records_for_accounts,
)
from axile.server.target_weight_snapshots import get_latest_account_target_snapshots_for_accounts
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_moment

router = APIRouter()


class AccountCopyRequest(BaseModel):
    """复制账户时仅接收新名称。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


def _account_public(account: Account) -> AccountPublic:
    """将持久化账户转换为包含连接编辑值的读取 DTO。"""
    # ``AccountPublic`` 有从凭证派生的摘要字段，并不属于 ORM 模型；先补全输入
    # 再校验，避免 Pydantic 在响应构造阶段把已提交的更新误报为 500。
    return AccountPublic.model_validate(
        {
            name: getattr(account, name)
            for name in AccountPublic.model_fields
            if name
            not in {
                "account_configured",
                "connection_values",
                "feishu_configured",
                "runtime_sync",
                "notification_state",
            }
        }
        | {
            "account_configured": bool(account.account_config),
            "connection_values": {
                field.name: account.account_config[field.name]
                for field in get_channel(str(account.trade_channel)).descriptor.account_form.fields
                if field.name in account.account_config
            },
            "feishu_configured": bool(account.feishu_key),
            "notification_state": account.notification_state,
        }
    )


def _account_public_with_runtime_sync(account: Account, sync: AccountRuntimeSync) -> AccountPublic:
    """将账户与持久化的运行态对齐状态组装为 API 响应。"""
    return _account_public(account).model_copy(update={"runtime_sync": AccountRuntimeSyncPublic.model_validate(sync)})


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


def _account_route_module() -> Any:
    from axile.server.api.routes import account as account_routes

    return account_routes


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


async def _create_account_record(
    session: SessionDep,
    account: AccountCreate,
    account_config: dict[str, object],
) -> Account:
    """
    创建账户并写入初始组合绑定记录.

    Parameters
    ----------
    session : SessionDep
        当前数据库会话。
    account : AccountCreate
        待创建的账户输入模型。

    Returns
    -------
    Account
        已加入当前会话、并完成初始组合绑定的账户对象。
    """
    code = account.execution_notification_code
    if code is not None and not code.strip():
        code = None
    if code is None and account.feishu_key and "execution_notification_code" not in account.model_fields_set:
        code = DEFAULT_ACCOUNT_NOTIFICATION_CODE
    db_account = Account.model_validate(
        account.model_copy(update={"account_config": account_config, "execution_notification_code": code})
    )
    session.add(db_account)
    await session.flush()

    account_id = cast("int", db_account.id)
    await add_record_portfolio_account(session, account_id, account.portfolio_id)
    return db_account


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
    if "account_control_override" in data and data["account_control_override"] is not None:
        data["account_control_override"] = AccountControlOverride.model_validate(data["account_control_override"])
    return data


async def _sync_portfolio_binding_update(
    session: SessionDep,
    db_account: Account,
    account_id: int,
    data: dict[str, object],
) -> None:
    """
    根据更新载荷补写账户与组合的绑定历史.

    Parameters
    ----------
    session : SessionDep
        当前数据库会话。
    db_account : Account
        当前数据库中的账户对象。
    account_id : int
        账户 ID。
    data : dict[str, object]
        本次更新对应的持久化字段映射。
    """
    if "portfolio_id" not in data:
        return

    next_portfolio_id = cast("int | None", data["portfolio_id"])
    # 这里必须在 sqlmodel_update 之前比较 old/new，才能准确补写绑定历史。
    if db_account.portfolio_id != next_portfolio_id:
        await add_record_portfolio_account(session, account_id, next_portfolio_id)


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=AccountPublic,
)
async def create_account(
    session: SessionDep,
    sched: SchedDep,
    account: AccountCreate,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AccountPublic:
    """创建账户."""
    account_routes = _account_route_module()
    try:
        if idempotency_key:
            existing_sync = await session.scalar(
                select(AccountRuntimeSync).where(AccountRuntimeSync.create_request_key == idempotency_key)
            )
            if existing_sync is not None:
                existing_account = await _get_account_or_404(session, existing_sync.account_id)
                if existing_sync.status != "synchronized":
                    response.status_code = status.HTTP_202_ACCEPTED
                return _account_public_with_runtime_sync(existing_account, existing_sync)
        account_routes._validate_account_control_binding(account.trade_channel, account.account_control_preset)
        account_config = _validate_channel_account_config(account.trade_channel, account.account_config)
        _check_algorithm_channel_compat(account.algorithm, str(account.trade_channel), "下单算法")
        _check_algorithm_channel_compat(account.empty_positions_algorithm, str(account.trade_channel), "清仓算法")
        if account.execution_notification_code and account.execution_notification_code.strip():
            validate_notification_function(account.execution_notification_code)
        parse_cron_expr(account.cron_expr)
        db_account = await _create_account_record(session, account, account_config)
        await enqueue_account_runtime_sync(
            session,
            cast("int", db_account.id),
            reset_worker=False,
            create_request_key=idempotency_key,
        )
        await session.commit()
        await session.refresh(db_account)
    except ValueError as exc:
        await session.rollback()
        logger.exception(f"创建账户失败: {exc}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        logger.exception(f"创建账户失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(exc)}",
        ) from exc

    from axile.server.execution.notification_outbox import wake_notifications
    from axile.server.execution.supplements import reap_cancelled_queue

    await reap_cancelled_queue(cast("int", db_account.id))
    wake_notifications(cast("int", db_account.id))
    sync = await _reconcile_committed_runtime(session, sched, cast("int", db_account.id))
    if sync.status != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return _account_public_with_runtime_sync(db_account, sync)


@router.post("/{account_id}/copy", status_code=status.HTTP_201_CREATED, response_model=AccountPublic)
async def copy_account(
    session: SessionDep,
    sched: SchedDep,
    account_id: int,
    request: AccountCopyRequest,
    response: Response,
) -> AccountPublic:
    """在来源账户完全暂停后复制配置与初始组合绑定。"""
    async with account_runtime_lock(account_id):
        if not request.name.strip():
            raise HTTPException(status_code=422, detail="新账户名称不能为空")
        source = await _get_account_or_404(session, account_id)
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
            routes = _account_route_module()
            routes._validate_account_control_binding(payload.trade_channel, payload.account_control_preset)
            config = _validate_channel_account_config(payload.trade_channel, payload.account_config)
            _check_algorithm_channel_compat(payload.algorithm, str(payload.trade_channel), "下单算法")
            _check_algorithm_channel_compat(payload.empty_positions_algorithm, str(payload.trade_channel), "清仓算法")
            parse_cron_expr(payload.cron_expr)
            copied = await _create_account_record(session, payload, config)
            copied.copied_from_account_id = account_id
            copied.copied_from_account_name = source.name
            await enqueue_account_runtime_sync(session, cast("int", copied.id), reset_worker=False)
            await session.commit()
            await session.refresh(copied)
        except (ValueError, ValidationError) as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            raise

    sync = await _reconcile_committed_runtime(session, sched, cast("int", copied.id))
    if sync.status != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return _account_public_with_runtime_sync(copied, sync)


@router.get("/", response_model=AccountListPublic)
async def list_accounts(session: SessionDep) -> AccountListPublic:
    """获取所有账户列表."""
    accounts = (await session.execute(select(Account))).scalars().all()
    return AccountListPublic(data=[_account_public(account) for account in accounts])


def _safe_total_asset(assets: dict[str, Any]) -> float:
    """读取持仓计算需要的实时总权益。"""
    try:
        return float(assets.get("total_asset") or 0.0)
    except (TypeError, ValueError):
        return 0.0


@router.get("/dashboard", response_model=AccountDashboardPublic)
async def account_dashboard(session: SessionDep, sched: SchedDep) -> AccountDashboardPublic:
    """
    仪表盘聚合: 一次性返回每个账户的舰队卡所需数据.

    Parameters
    ----------
    session : SessionDep
        当前数据库会话。
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
    accounts = (await session.execute(select(Account))).scalars().all()
    bindings = await get_portfolios_every_account(session)

    account_ids = [cast("int", account.id) for account in accounts]
    performance_by_account = await read_performance_summaries(session, account_ids)
    recent_records_by_account = await get_recent_execute_records_for_accounts(session, account_ids, limit=1)
    recent_by_account = await get_recent_account_asset_snapshots_for_accounts(session, account_ids, limit=1)
    target_pairs = [
        (account_id, portfolio_id) for account_id, portfolio_id in bindings.items() if portfolio_id is not None
    ]
    targets_by_account = await get_latest_account_target_snapshots_for_accounts(session, target_pairs)
    items: list[AccountDashboardItemPublic] = []
    for account in accounts:
        account_id = cast("int", account.id)
        recent = recent_by_account.get(account_id, [])
        latest_snapshot = recent[0] if recent else None
        latest_records = recent_records_by_account.get(account_id, [])
        latest = latest_records[0] if latest_records else None
        assets = latest_snapshot.assets if latest_snapshot is not None else {}
        raw_positions = assets.get("positions")
        positions: list[Any] = raw_positions if isinstance(raw_positions, list) else []
        holdings_count = len(positions)
        valuation_complete = all(
            isinstance(pos, dict) and isinstance(pos.get("market_value"), (int, float)) for pos in positions
        )
        position_weights = (
            sorted((abs(float(pos["market_value"])) for pos in positions), reverse=True)[:12]
            if valuation_complete
            else []
        )
        plugin = get_channel(str(account.trade_channel))
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

        last_output_status = None if latest is None else execution_record_output_status(latest.raw_result)
        performance = performance_by_account.get(account_id, PerformanceSummary())
        previous_close = (
            close_before(performance.points, local_time(latest_snapshot.created_at).date().isoformat())
            if latest_snapshot is not None
            else None
        )
        target_snapshot = targets_by_account.get(account_id)
        target_weights = None if target_snapshot is None else target_snapshot.normalized_weights
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
                name=account.name,
                remark=account.remark,
                market=account.market,
                trade_channel=account.trade_channel,
                is_started=account.is_started,
                portfolio_id=bindings.get(account_id),
                is_scheduled=job is not None,
                next_run_time=next_run.isoformat() if next_run is not None else None,
                total_asset=_safe_total_asset(assets),
                currency=str(currency),
                holdings_count=holdings_count,
                position_weights=position_weights,
                performance=performance,
                asset_observed_at=latest_snapshot.created_at if latest_snapshot is not None else None,
                previous_close=previous_close,
                last_is_success=latest.is_success if latest is not None else None,
                last_exec_at=latest.created_at if latest is not None else None,
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
async def delete_account(session: SessionDep, sched: SchedDep, account_id: int) -> Message:
    """删除账户."""
    # 与对齐互斥，先等锁再读库，避免删除期间旧对齐把 worker 重新建立。
    async with account_runtime_lock(account_id):
        account = await _get_account_or_404(session, account_id)
        await session.delete(account)
        await session.commit()
        await session.close()
        delete_job(sched, account_id)
        await drop_account_worker(account_id)
    return Message(message="成功删除账户")


@router.get(
    "/{account_id}",
    response_model=AccountPublic,
)
async def account_info(session: SessionDep, account_id: int) -> AccountPublic:
    """获取账户详情."""
    return _account_public(await _get_account_or_404(session, account_id))


@router.get(
    "/{account_id}/next_run_time",
    response_model=AccountNextRunPublic,
)
async def account_next_run_time(
    session: SessionDep,
    sched: SchedDep,
    account_id: int,
) -> AccountNextRunPublic:
    """
    获取账户未来三次调度执行时间.

    Parameters
    ----------
    session : SessionDep
        当前数据库会话。
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
    account = await _get_account_or_404(session, account_id)
    job = sched.get_job(str(account_id))  # type: ignore[no-untyped-call]
    next_run_times = _next_job_run_times(job) if job is not None else []
    next_execution_times = _next_job_execution_times(job, account.trade_channel) if job is not None else []
    if account.supplement and job is not None:
        from axile.server.api.routes.account_schedule import SchedulePreviewRequest, schedule_preview
        from axile.server.db.models.supplement import SupplementGroup, SupplementStep

        preview = await schedule_preview(
            SchedulePreviewRequest(
                trade_channel=account.trade_channel,
                cron_expr=account.cron_expr,
                supplement=account.supplement,
                limit=30,
            )
        )
        now = clock_now(tz=SCHEDULER_TIMEZONE)
        pending = (
            await session.scalars(
                select(SupplementStep)
                .join(SupplementGroup, col(SupplementGroup.id) == col(SupplementStep.group_id))
                .where(
                    SupplementGroup.account_id == account_id,
                    SupplementGroup.status == "active",
                    SupplementStep.status == "pending",
                )
            )
        ).all()
        times = {
            item.scheduled_at
            for item in preview.items
            if item.action == "execute" and (item.base_scheduled_at is None or item.base_scheduled_at >= now)
        }
        times.update(
            datetime.fromisoformat(step.scheduled_at)
            for step in pending
            if datetime.fromisoformat(step.scheduled_at) > now
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
    session: SessionDep,
    sched: SchedDep,
    account_id: int,
    account: AccountUpdate,
    response: Response,
) -> AccountPublic:
    """更新账户."""
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        return await _update_account(session, sched, account_id, account, response)


async def _update_account(
    session: SessionDep,
    sched: SchedDep,
    account_id: int,
    account: AccountUpdate,
    response: Response,
) -> AccountPublic:
    """更新账户."""
    db_account = await _get_account_or_404(session, account_id)
    account_routes = _account_route_module()

    try:
        # 账户控制 binding 要按“更新后的目标状态”校验，不能只看当前库里的旧值。
        next_trade_channel, next_preset = _resolve_next_account_control_binding(db_account, account)
        account_routes._validate_account_control_binding(next_trade_channel, next_preset)
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
        runtime_changed = bool({"account_config", "trade_channel", "is_started"} & data.keys())
        from axile.server.execution.supplements import cancel_groups_in_session

        changed_schedule = any(
            key in data
            and data[key]
            != (
                getattr(db_account, key).model_dump(mode="json")
                if isinstance(getattr(db_account, key), BaseModel)
                else getattr(db_account, key)
            )
            for key in ("cron_expr", "supplement", "trade_channel", "portfolio_id")
        )
        if (db_account.supplement or db_account.has_pending_supplement) and (
            changed_schedule or data.get("is_started") is False
        ):
            await cancel_groups_in_session(
                session, account_id, "account_stopped" if data.get("is_started") is False else "configuration_changed"
            )
        await _sync_portfolio_binding_update(session, db_account, account_id, data)
        db_account.sqlmodel_update(data)

        session.add(db_account)
        await enqueue_account_runtime_sync(session, account_id, reset_worker=runtime_changed)
        await session.commit()
        await session.refresh(db_account)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(exc)}",
        ) from exc

    from axile.server.execution.notification_outbox import wake_notifications
    from axile.server.execution.supplements import reap_cancelled_queue

    await reap_cancelled_queue(cast("int", db_account.id))
    wake_notifications(cast("int", db_account.id))
    sync = await _reconcile_committed_runtime(session, sched, cast("int", db_account.id))
    if sync.status != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return _account_public_with_runtime_sync(db_account, sync)


@router.get("/{account_id}/runtime-sync", response_model=AccountRuntimeSyncPublic)
async def account_runtime_sync_status(session: SessionDep, account_id: int) -> AccountRuntimeSyncPublic:
    """查询账户运行态对齐的失败原因与重试计数。"""
    await _get_account_or_404(session, account_id)
    sync = await session.scalar(select(AccountRuntimeSync).where(AccountRuntimeSync.account_id == account_id))
    if sync is None:
        raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail="账户运行态尚待首次同步")
    return AccountRuntimeSyncPublic.model_validate(sync)


@router.post("/{account_id}/runtime-sync/retry", response_model=AccountRuntimeSyncPublic)
async def retry_account_runtime_sync(
    session: SessionDep,
    sched: SchedDep,
    account_id: int,
    response: Response,
) -> AccountRuntimeSyncPublic:
    """显式重试账户 scheduler 和渠道 worker 的运行态对齐。"""
    await _get_account_or_404(session, account_id)
    sync = await _reconcile_committed_runtime(session, sched, account_id)
    if sync.status != "synchronized":
        response.status_code = status.HTTP_202_ACCEPTED
    return AccountRuntimeSyncPublic.model_validate(sync)


async def _reconcile_committed_runtime(session: SessionDep, sched: SchedDep, account_id: int) -> AccountRuntimeSync:
    """释放已提交配置的读取会话；等锁期间被删除的账户仍返回 404。"""
    await session.close()
    try:
        return await reconcile_account_runtime(account_id, sched)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账户不存在") from exc


@router.get("/execute_records/{account_id}", response_model=ExecuteRecordListPublic)
async def list_execute_records(
    session: SessionDep,
    account_id: int,
    pagination: HistoryPaginationDep,
    execution_id: str | None = None,
    record_id: int | None = None,
) -> ExecuteRecordListPublic:
    """获取账户的执行记录."""
    db_account = await _get_account_or_404(session, account_id)

    filters = [ExecuteRecord.account_id == db_account.id]
    if execution_id is not None:
        filters.append(ExecuteRecord.execution_id == execution_id)
    if record_id is not None:
        filters.append(ExecuteRecord.id == record_id)
    count_statement = select(func.count()).select_from(ExecuteRecord).where(*filters)
    count = (await session.execute(count_statement)).scalar_one()

    statement = (
        select(ExecuteRecord)
        .where(*filters)
        # execute_record 没有独立时间排序键时，按自增 id 倒序近似“最新在前”的时间线。
        .order_by(desc(ExecuteRecord.id))
        .offset(pagination.skip)
        .limit(pagination.limit)
    )
    records = (await session.execute(statement)).scalars().all()

    data = [
        # 旧记录读时归一：列表响应与新记录同形，前端不需要 legacy 分支。
        ExecuteRecordPublic.model_validate(record).model_copy(
            update={"raw_result": normalize_legacy_result(record.raw_result)}
        )
        for record in records
    ]
    return ExecuteRecordListPublic(
        data=data,
        count=count,
    )


@router.get("/portfolio_records/{account_id}", response_model=PortfolioAccountListPublic)
async def list_portfolio_records(
    session: SessionDep,
    account_id: int,
    pagination: HistoryPaginationDep,
) -> PortfolioAccountListPublic:
    """获取账户的组合更换记录."""
    db_account = await _get_account_or_404(session, account_id)

    count_statement = (
        select(func.count()).select_from(PortfolioAccount).where(PortfolioAccount.account_id == db_account.id)
    )
    count = (await session.execute(count_statement)).scalar_one()

    # 组合绑定记录保持插入顺序返回，便于直接还原账户组合切换历史。
    statement = (
        select(PortfolioAccount)
        .where(PortfolioAccount.account_id == db_account.id)
        .offset(pagination.skip)
        .limit(pagination.limit)
    )
    records = (await session.execute(statement)).scalars().all()

    return PortfolioAccountListPublic(
        data=[PortfolioAccountPublic.model_validate(record) for record in records],
        count=count,
    )


@router.delete("/execute_records/{account_id}", response_model=Message)
async def delete_execute_records(session: SessionDep, account_id: int) -> Message:
    """删除账户的执行记录."""
    await _get_account_or_404(session, account_id)

    # 执行来源快照属于执行历史的一部分，随记录清理；人工刷新快照保持独立观测语义。
    await session.execute(
        delete(AccountAssetSnapshot).where(
            and_(
                AccountAssetSnapshot.account_id == account_id,
                AccountAssetSnapshot.source == "execution",
            )
        )
    )
    # execution event / artifact 审计流仍不联动。
    stmt = delete(ExecuteRecord).where(and_(ExecuteRecord.account_id == account_id))
    await session.execute(stmt)
    await session.commit()

    return Message(message="成功删除")
