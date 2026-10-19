"""按运行时注册的交易渠道创建执行器实例."""

from axile.channels import get_channel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.trading_calendar import ShinnyTradingCalendar
from axile.server.connection_state import report_connection_for_account
from axile.server.db.models.account import AccountContext
from axile.server.execution.notification_state import record_notification_result
from axile.server.execution.resources import close_executor
from axile.server.order_submissions import create_order_submission_recorder


def create_executor_instance(account: AccountContext, *, initialize: bool = True) -> AbstractExecutor:
    """
    根据交易渠道创建相应的执行器实例.

    Parameters
    ----------
    account : Account
        待执行的账户模型。

    Returns
    -------
    AbstractExecutor
        与账户交易渠道匹配的执行器实例。

    Raises
    ------
    ValueError
        当交易渠道未被支持时抛出。
    """
    try:
        plugin = get_channel(account.trade_channel)
    except KeyError as exc:
        raise ValueError(f"未注册交易渠道: {account.trade_channel}") from exc
    config_data = dict(account.account_config)
    config_data["channel_type"] = account.trade_channel
    config = plugin.account_config_model.model_validate(config_data)
    with report_connection_for_account(account):
        executor = plugin.create_executor(config)
    try:
        account_id = getattr(account, "id", None)
        if account_id is not None:
            if str(account.trade_channel) == "ctp":
                setattr(
                    executor,
                    "order_submission_recorder",
                    create_order_submission_recorder(account_id, source="main_program"),
                )

            def notification_result(execution_id: str | None, finished_at: str, ok: bool, error: str | None) -> None:
                record_notification_result(account_id, execution_id, finished_at, ok, error)

            setattr(executor, "_notification_result_callback", notification_result)
        setattr(executor, "_requires_connection_initialization", plugin.requires_pre_connect_guard and not initialize)
        set_trading_calendar = getattr(executor, "set_trading_calendar", None)
        if callable(set_trading_calendar):
            set_trading_calendar(ShinnyTradingCalendar())
        declaration = plugin.descriptor.calendar
        set_channel_calendar = getattr(executor, "set_channel_calendar", None)
        if callable(set_channel_calendar):
            set_channel_calendar(declaration.calendar_id if declaration is not None else None)
        if plugin.requires_pre_connect_guard and initialize:
            executor._initialize_connection(config)
            setattr(executor, "_requires_connection_initialization", False)
    except BaseException:
        close_executor(executor)
        raise
    return executor


def initialize_executor_instance(executor: AbstractExecutor) -> None:
    """连接一个由两阶段渠道插件创建的执行器。"""
    if executor.account_config is None:
        raise RuntimeError("执行器缺少账户配置")
    executor._initialize_connection(executor.account_config)
    setattr(executor, "_requires_connection_initialization", False)
