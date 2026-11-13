"""执行编排使用的杠杆与算法解析辅助函数."""

from copy import deepcopy
from datetime import datetime
from typing import Any

from axile.channels import get_channel
from axile.channels.schedule_clock import is_within_schedule_windows
from axile.domain.execution import ExecutionKind
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.cron import SCHEDULER_TIMEZONE
from axile.server.db.models.account import AccountContext
from axile.server.db.models.algorithm_schedule import AlgorithmSchedule


def _select_algorithm(
    algorithm: dict[str, Any], schedule: AlgorithmSchedule, selected_at: datetime
) -> tuple[dict[str, Any], dict[str, object]]:
    """使用同一选择时刻解析完整配置，不合并其他范围的参数。"""
    matched = next(
        (rule for rule in schedule if is_within_schedule_windows(selected_at, [(rule.start, rule.end)])), None
    )
    return deepcopy(matched.algorithm if matched is not None else algorithm), {
        "start": matched.start if matched is not None else None,
        "end": matched.end if matched is not None else None,
    }


def select_rebalance_algorithm(account: AccountContext) -> tuple[AccountContext, dict[str, object]]:
    """开跑前选定一次算法，只修改本次快照，完整参数与选择时刻一起冻结。"""
    selected_at = clock_now(tz=SCHEDULER_TIMEZONE)
    algorithm, period = _select_algorithm(account.algorithm, account.algorithm_schedule, selected_at)
    selection: dict[str, object] = {
        "selected_at": selected_at.isoformat(),
        "timezone": str(SCHEDULER_TIMEZONE),
        **period,
    }
    products = {}
    product_selection = {}
    for key, profile in account.product_algorithms.items():
        selected, period = _select_algorithm(profile.algorithm, profile.algorithm_schedule, selected_at)
        products[key] = profile.model_copy(
            update={"algorithm": {**selected, "params": selected.get("params") or {}}, "algorithm_schedule": []}
        )
        product_selection[key] = period
    if products:
        selection["product_algorithms"] = product_selection
    return account.model_copy(update={"algorithm": algorithm, "product_algorithms": products}), selection


def resolve_account_leverages(account: AccountContext) -> tuple[float, float]:
    """
    解析账户执行时使用的多空杠杆.

    Parameters
    ----------
    account : Account
        当前执行对应的账户对象。

    Returns
    -------
    tuple[float, float]
        解析后的多头与空头杠杆。
    """
    defaults = get_channel(account.trade_channel).descriptor.defaults
    default_long = defaults.long_leverage
    default_short = defaults.short_leverage

    long_leverage = float(account.long_leverage if account.long_leverage is not None else default_long)
    short_leverage = float(account.short_leverage if account.short_leverage is not None else default_short)
    return long_leverage, short_leverage


def resolve_execution_algorithm_name(
    account: AccountContext,
    execution_kind: ExecutionKind,
    algorithm_override: dict[str, object] | None = None,
) -> str:
    """
    解析当前 execution 应写入审计和状态表的算法名.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    execution_kind : ExecutionKind
        当前执行类型。
    algorithm_override : dict[str, object] | None, optional
        显式指定的算法配置。

    Returns
    -------
    str
        应写入 execution 元数据的算法名。
    """
    if algorithm_override is not None:
        return str(algorithm_override.get("method", "SINGLE-MAKER"))
    if execution_kind == ExecutionKind.CLEAR_POSITIONS:
        empty_algorithm = resolve_empty_positions_algorithm(account)
        return str(empty_algorithm.get("method", "SINGLE-MAKER"))
    if account.product_algorithms:
        return "按品种"
    return str(account.algorithm.get("method", "SINGLE-MAKER"))


def resolve_empty_positions_algorithm(
    account: AccountContext,
    algorithm_override: dict[str, object] | None = None,
) -> dict[str, object]:
    """
    解析清仓路径实际使用的算法配置.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    algorithm_override : dict[str, object] | None, optional
        显式指定的清仓算法配置。

    Returns
    -------
    dict[str, object]
        实际用于清仓执行的算法配置。
    """
    if algorithm_override is not None:
        return dict(algorithm_override)
    if account.empty_positions_algorithm is not None:
        return dict(account.empty_positions_algorithm)
    default = get_channel(account.trade_channel).descriptor.defaults.empty_positions_algorithm
    if default is None:
        return {"method": "SINGLE-MAKER", "params": {}}
    return dict(default.model_dump(mode="python"))
