"""执行编排使用的杠杆与算法解析辅助函数."""

from axile.channels import get_channel
from axile.domain.execution import ExecutionKind
from axile.server.db.models.account import AccountContext


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
        return "按品种算法"
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
