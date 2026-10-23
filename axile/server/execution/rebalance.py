"""
编排调仓 execution 的输入解析与入口流程.

该模块负责把账户、组合函数和历史执行快照整理成后端执行层可消费的
标准请求，并处理 inline execution 的注册、收尾与异常桥接。真正的下单
与审计落库由相邻的 backend 与 lifecycle 模块负责。
"""

from dataclasses import dataclass
from typing import cast

import loguru

from axile.common.gm_symbols import normalize_gm_standard_input
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.termination import ExecutionTerminated
from axile.server.core.log_config import execution_log_context
from axile.server.db.models import Account, ExecuteRecord, Portfolio
from axile.server.db.models.account import AccountContext, AccountSnapshot
from axile.server.error_details import exception_details
from axile.server.execution import backend as execution_backend
from axile.server.execution import lifecycle as execution_lifecycle
from axile.server.execution.execution_algorithms import (
    resolve_account_leverages,
    resolve_execution_algorithm_name,
)
from axile.server.execution.execution_records_output import sanitize_standard_input_for_audit
from axile.server.execution.notification_assets import load_notification_snapshot
from axile.server.execution.records import append_error_execute_record
from axile.server.execution.registry import (
    AccountExecutionAlreadyRunningError,
    clear_running_execution,
    register_inline_execution,
)
from axile.server.native_store import native_operation
from axile.server.portfolio_targets import calculate_portfolio_for_account
from axile.server.repositories import get_account_snapshot
from axile.server.target_weight_snapshots import append_target_weight_snapshot
from axile.server.utils import trade_channel_check


@dataclass(frozen=True)
class _ResolvedRebalanceRequest:
    """承载调仓准备阶段解析出的目标权重."""

    portfolio_id: int
    curr_target: dict[str, float]


async def _load_bound_portfolio(
    account: AccountContext,
    account_id: int,
    execution_id: str | None,
) -> Portfolio:
    """
    加载账户当前绑定的最新组合，必要时先落库错误记录.

    Parameters
    ----------
    account : Account
        当前待执行的账户对象。
    account_id : int
        账户 ID。
    execution_id : str | None
        当前 execution 标识；用于关联错误审计。

    Returns
    -------
    Portfolio
        已解析出的组合对象。

    Raises
    ------
    ValueError
        当账户未绑定组合或绑定记录已失效时抛出。
    """
    context = await native_operation("portfolio_operation", {"op": "bound_context", "account_id": account_id})
    curr_portfolio_id = context["portfolio_id"]
    if curr_portfolio_id is None:
        msg = "无法调仓，该账户未绑定组合"
        await append_error_execute_record(account_id=account.id, msg=msg, execution_id=execution_id)
        raise ValueError(msg)

    portfolio = context["portfolio"]

    if portfolio is None:
        msg = f"无法调仓，该账户绑定的组合不存在, 组合id: {curr_portfolio_id}"
        await append_error_execute_record(
            account_id=account.id,
            msg=msg,
            execution_id=execution_id,
        )
        raise ValueError(msg)

    return Portfolio.model_validate(portfolio)


async def _execute_portfolio_function(
    account: AccountContext,
    portfolio: Portfolio,
    execution_id: str | None,
    logger: "loguru.Logger",
) -> dict[str, float]:
    """
    执行账户绑定的组合函数并返回目标权重.

    Parameters
    ----------
    account : Account
        当前待执行的账户对象。
    portfolio : Portfolio
        账户当前绑定的组合。
    execution_id : str | None
        当前 execution 标识；用于关联错误审计。
    logger : loguru.Logger
        当前执行使用的日志对象。

    Returns
    -------
    dict[str, float]
        用户函数计算出的目标权重。

    Raises
    ------
    ValueError
        当自定义脚本执行失败时抛出。

    """
    try:
        result = await calculate_portfolio_for_account(
            account,
            portfolio.custom_calc_py_code,
            execution_id=execution_id,
        )
        if not result.ok or result.target is None:
            raise result.error or ValueError("自定义组合脚本执行失败")
        return result.target
    except Exception as e:
        # 脚本错误行号对用户有定位价值；异常原文属于技术证据，只进 technical_detail 与日志。
        line = getattr(e, "error_line", None)
        where = f"（第 {line} 行）" if isinstance(line, int) else ""
        msg = f"自定义组合脚本执行失败{where}，具体原因见执行证据"
        await append_error_execute_record(
            account_id=account.id,
            msg=msg,
            execution_id=execution_id,
            raw_result=exception_details(e),
        )
        raise ValueError(msg) from e


async def _load_rebalance_account(
    account_id: int,
    logger: "loguru.Logger",
) -> AccountSnapshot:
    """读取不持有数据库会话的独立执行快照。"""
    account = await get_account_snapshot(account_id)
    if account is None:
        msg = f"无法执行任务 账户id: {account_id} 不存在"
        logger.error("{}", msg)
        raise ValueError(msg)
    return account


async def _resolve_rebalance_request(
    account: AccountContext,
    execution_id: str | None,
    logger: "loguru.Logger",
) -> _ResolvedRebalanceRequest:
    """解析本次调仓实际要执行的目标权重."""
    portfolio = await _load_bound_portfolio(account, cast("int", account.id), execution_id)
    curr_target = await _execute_portfolio_function(account, portfolio, execution_id, logger)
    return _ResolvedRebalanceRequest(portfolio_id=cast("int", portfolio.id), curr_target=curr_target)


def _normalize_rebalance_target(
    account: AccountContext,
    curr_target: dict[str, float],
) -> dict[str, float]:
    """
    根据账户杠杆与精度约束规范化调仓目标权重.

    Parameters
    ----------
    account : Account
        当前待执行的账户对象。
    curr_target : dict[str, float]
        原始目标权重。

    Returns
    -------
    dict[str, float]
        已按账户杠杆和 ``weight_precision`` 规整后的目标权重。

    Notes
    -----
    这里必须先按多空杠杆缩放，再按账户精度离散化；否则审计看到的目标、
    执行器实际收到的目标以及账户界面展示值会产生漂移。
    """
    weight_precision = account.weight_precision
    long_leverage, short_leverage = resolve_account_leverages(account)

    def _normalize_weight(weight: float) -> float:
        scaled = weight * long_leverage if weight > 0 else weight * short_leverage
        return round(round(scaled / weight_precision) * weight_precision, 6)

    return {symbol: _normalize_weight(weight) for symbol, weight in curr_target.items()}


async def _load_last_target_snapshot(account: AccountContext) -> dict[str, object]:
    """加载上一次成功调仓记录中的目标快照."""
    try:
        evidence = await native_operation("execution_read", {"op": "last_target", "account_id": account.id})
    except Exception as e:
        loguru.logger.opt(exception=e).error("{}", e)
        raise

    return cast("dict[str, object]", evidence["target"]) if evidence["present"] else {}


def _build_rebalance_standard_input(
    *,
    account: AccountContext,
    curr_target: dict[str, float],
    last_target: dict[str, object],
    execution_id: str | None,
    trigger_source: str,
) -> UnifiedStandardInput:
    """
    构造调仓执行器所需的标准输入模型.

    Parameters
    ----------
    account : Account
        当前待执行的账户对象。
    curr_target : dict[str, float]
        已规范化的目标权重。
    last_target : dict[str, object]
        上一次成功执行记录中的目标快照。
    execution_id : str | None
        当前 execution 标识。
    trigger_source : str
        触发来源。

    Returns
    -------
    UnifiedStandardInput
        传递给执行器的统一标准输入。
    """
    feishu_key = account.feishu_key if account.feishu_key and len(account.feishu_key) else None
    standard_input = UnifiedStandardInput.from_dict(
        {
            "curr_target": curr_target,
            "last_target": last_target,
            "account_config": account.account_config,
            "algorithm": account.algorithm,
            "trade_rules": account.trade_rules,
            "futures_close_policy": account.futures_close_policy.model_dump(),
            "forbidden_symbols": account.forbidden_symbols,
            "risk_symbols": account.risk_symbols,
            "feishu_key": feishu_key,
            "execution_notification_code": account.execution_notification_code,
            "feishu_account": {
                "id": account.id,
                "name": account.name,
                "market": account.market,
                "trade_channel": str(account.trade_channel),
                "brokerage": account.brokerage,
                "remark": account.remark,
                "portfolio_id": account.portfolio_id,
                "weight_precision": account.weight_precision,
                "long_leverage": account.long_leverage,
                "short_leverage": account.short_leverage,
            },
            "execution_timeout": account.execution_timeout,
            "channel_type": str(account.trade_channel),
            "extra": {
                "audit": {
                    "execution_id": execution_id,
                    "account_id": account.id,
                    "channel": str(account.trade_channel),
                    "algorithm": resolve_execution_algorithm_name(account, ExecutionKind.REBALANCE),
                    "trigger_source": trigger_source,
                    "execution_kind": "rebalance",
                },
            },
        }
    )
    if account.trade_channel == TradeChannel.GM:
        return normalize_gm_standard_input(standard_input)
    return standard_input


async def _build_rebalance_backend_request(
    *,
    account: AccountContext,
    curr_target: dict[str, float],
    execution_id: str | None,
    trigger_source: str,
    logger: "loguru.Logger",
    cleanup: bool,
    normalized_target: dict[str, float] | None = None,
) -> execution_backend.RebalanceBackendRequest:
    """
    组装后端执行调仓所需的请求对象.

    Parameters
    ----------
    account : Account
        当前待执行的账户对象。
    curr_target : dict[str, float]
        原始目标权重。
    execution_id : str | None
        当前 execution 标识。
    trigger_source : str
        触发来源。
    logger : loguru.Logger
        当前执行使用的日志对象。
    cleanup : bool
        是否在执行结束后执行底层清理逻辑。

    Returns
    -------
    execution_backend.RebalanceBackendRequest
        可直接交给 backend 执行层的请求对象。
    """
    if isinstance(account, Account):
        account = account.snapshot()
    if normalized_target is None:
        normalized_target = _normalize_rebalance_target(account, curr_target)
    last_target = await _load_last_target_snapshot(account)
    notification_snapshot = await load_notification_snapshot(account)
    # 审计输入、执行器输入与回传给调用方的 curr_target 必须共享同一份
    # 规范化结果，避免回放执行时出现“显示目标”和“实际下单目标”不一致。
    standard_input = _build_rebalance_standard_input(
        account=account,
        curr_target=normalized_target,
        last_target=last_target,
        execution_id=execution_id,
        trigger_source=trigger_source,
    )
    if execution_id and account.supplement:
        from axile.server.execution.supplement_notifications import execution_steps

        if await execution_steps(execution_id):
            standard_input.execution_notification_code = None
            standard_input.extra = {**standard_input.extra, "server_notification": True}
    long_leverage, short_leverage = resolve_account_leverages(account)
    audit_input = sanitize_standard_input_for_audit(standard_input)
    audit_input["strategy_target"] = dict(curr_target)
    audit_input["sizing_context"] = {
        "long_leverage": long_leverage,
        "short_leverage": short_leverage,
        "weight_precision": account.weight_precision,
    }
    return execution_backend.RebalanceBackendRequest(
        account=account,
        standard_input=standard_input,
        standard_input_dict=standard_input.to_dict(),
        audit_input=audit_input,
        execution_id=execution_id,
        trigger_source=trigger_source,
        cleanup=cleanup,
        curr_target=normalized_target,
        last_target=last_target,
        logger=logger,
        notification_snapshot=notification_snapshot,
    )


async def _run_account_rebalance(
    account_id: int,
    execution_id: str | None = None,
    trigger_source: str = "scheduler",
    logger: "loguru.Logger | None" = None,
) -> ExecuteRecord:
    """
    加载账户并执行一次调仓主流程.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    execution_id : str | None, optional
        当前 execution 标识；为空时表示未显式跟踪。
    trigger_source : str, optional
        本次执行的触发来源。
    logger : loguru.Logger | None, optional
        用于输出日志的 logger；未提供时使用全局 logger。

    Returns
    -------
    ExecuteRecord
        本次调仓完成后持久化的执行记录。
    """
    if logger is None:
        logger = loguru.logger

    account = await _load_rebalance_account(account_id, logger)
    logger.info("执行任务")
    await trade_channel_check(account, execution_id=execution_id)
    request = await _resolve_rebalance_request(account, execution_id, logger)

    normalized_target = _normalize_rebalance_target(account, request.curr_target)
    await append_target_weight_snapshot(
        portfolio_id=request.portfolio_id,
        account_id=cast("int", account.id),
        raw_weights=request.curr_target,
        normalized_weights=normalized_target,
        source="execution",
        execution_id=execution_id,
    )

    return await trade(
        account,
        request.curr_target,
        execution_id=execution_id,
        trigger_source=trigger_source,
        logger=logger,
        normalized_target=normalized_target,
    )


async def trade(
    account: AccountContext,
    curr_target: dict[str, float],
    execution_id: str | None = None,
    trigger_source: str = "scheduler",
    logger: "loguru.Logger | None" = None,
    cleanup: bool = True,
    normalized_target: dict[str, float] | None = None,
) -> ExecuteRecord:
    """
    为账户执行一次调仓任务，并持久化结果.

    Parameters
    ----------
    account : Account
        待执行的账户对象。
    curr_target : dict[str, float]
        本次调仓目标权重。
    execution_id : str | None, optional
        当前 execution 标识。
    trigger_source : str, optional
        本次执行的触发来源。
    logger : loguru.Logger | None, optional
        用于输出日志的 logger；未提供时使用全局 logger。
    cleanup : bool, optional
        是否在执行结束后执行底层清理逻辑。

    Returns
    -------
    ExecuteRecord
        本次调仓对应的执行记录。
    """
    if logger is None:
        logger = loguru.logger

    request = await _build_rebalance_backend_request(
        account=account,
        curr_target=curr_target,
        execution_id=execution_id,
        trigger_source=trigger_source,
        logger=logger,
        cleanup=cleanup,
        normalized_target=normalized_target,
    )
    return await execution_backend.run_rebalance_via_backend(request=request)


async def execute_trade(
    account_id: int,
    execution_id: str | None = None,
    trigger_source: str = "scheduler",
    *,
    lock_acquired: bool = False,
) -> ExecuteRecord:
    """
    立即为指定账户执行一次交易任务.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    execution_id : str | None, optional
        指定的 execution 标识；为空时按当前流程生成或注册。
    trigger_source : str, optional
        本次执行的触发来源。
    lock_acquired : bool, optional
        调用方是否已完成运行中任务互斥注册。

    Returns
    -------
    ExecuteRecord
        本次调仓对应的执行记录。
    """
    account: AccountContext | None = None
    tracked_execution_id = execution_id
    if not lock_acquired:
        account = await _load_rebalance_account(account_id, loguru.logger)
        tracked_execution_id = register_inline_execution(
            account=account,
            execution_kind=ExecutionKind.REBALANCE,
            execution_id=execution_id,
            algorithm_name=resolve_execution_algorithm_name(account, ExecutionKind.REBALANCE),
        )

    try:
        # 当调用方已持有运行锁时，这里只复用既有 execution 上下文，不重复做
        # 注册和释放；否则会打乱上层对 execution 生命周期的单点管理。
        if account is None:
            account = await _load_rebalance_account(account_id, loguru.logger)

        with loguru.logger.contextualize(
            **execution_log_context(
                account_id=account_id,
                account_name=account.name,
                channel=account.trade_channel,
                execution_id=tracked_execution_id,
            )
        ):
            record = await _run_account_rebalance(
                account_id,
                execution_id=tracked_execution_id,
                trigger_source=trigger_source,
            )
        if not lock_acquired:
            await execution_lifecycle.mark_inline_execution_succeeded(
                cast("str", tracked_execution_id),
                record,
            )
        return record
    except ExecutionTerminated as exc:
        if not lock_acquired:
            await execution_lifecycle.handle_inline_execution_terminated(
                account_id=account_id,
                execution_id=cast("str", tracked_execution_id),
                execution_kind=ExecutionKind.REBALANCE,
                exc=exc,
            )
        raise
    except Exception as e:
        if isinstance(e, AccountExecutionAlreadyRunningError):
            raise
        if not lock_acquired:
            await execution_lifecycle.handle_inline_execution_failure(
                account=account,
                execution_id=cast("str", tracked_execution_id),
                error=e,
            )
        raise
    finally:
        if not lock_acquired:
            # inline execution 的兜底清理始终在本层完成，确保注册成功后无论
            # 成功、失败还是终止都能释放账户级运行占位。
            clear_running_execution(account_id, cast("str", tracked_execution_id))
