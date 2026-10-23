"""服务端共用辅助工具."""

from __future__ import annotations

from axile.common.trade_channel import TradeChannel
from axile.server.channel_capabilities import missing_packages
from axile.server.cron import is_blank_cron_expr, parse_cron_expr
from axile.server.db.models.account import AccountContext
from axile.server.error_notifications import (
    build_error_card,
    build_test_card,
    get_external_ip,
    send_feishu_error,
)
from axile.server.execution import records as execution_records

__all__ = [
    "build_error_card",
    "build_test_card",
    "get_external_ip",
    "is_blank_cron_expr",
    "parse_cron_expr",
    "send_feishu_error",
    "trade_channel_check",
]


async def trade_channel_check(account: AccountContext, execution_id: str | None = None) -> None:
    """
    实盘执行前的渠道依赖预检.

    校验账户所属渠道的可选依赖是否已安装；缺任一依赖时写入错误执行记录并抛出
    ``ValueError`` 以阻断后续下单。渠道→依赖映射统一由
    :mod:`axile.server.channel_capabilities` 维护，覆盖全部渠道（含 CTP）。

    Parameters
    ----------
    account : Account
        待执行的账户对象。
    execution_id : str | None, optional
        当前 execution 标识，写入错误记录时一并关联。

    Raises
    ------
    ValueError
        当账户渠道存在未安装的依赖时抛出。
    """
    if missing_packages(account.trade_channel):
        msg = f"请先安装 {TradeChannel(account.trade_channel).name} 对应的依赖"
        error = ValueError(msg)
        await execution_records.append_error_execute_record(
            account_id=account.id,
            msg=msg,
            execution_id=execution_id,
            error=error,
        )
        raise error
