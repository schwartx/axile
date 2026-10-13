"""验证临时 CTP 会话，复用原有连接顺序、查询屏障与账户控制。"""

from uuid import uuid4

from axile.channels.contracts import ConnectionProgress
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.ctp.validation_details import (
    account_details,
    instrument_details,
    login_details,
    order_details,
    position_details,
    trade_details,
)
from axile.executor.models.unified_input_accounts import BaseAccountConfig, CTPAccountConfig
from axile.executor.trading_calendar import ShinnyTradingCalendar


def validate_connection(config: BaseAccountConfig, report: ConnectionProgress) -> None:
    """只创建临时执行器；无论验证结果如何，都释放原生 API。"""
    if not isinstance(config, CTPAccountConfig):
        raise TypeError("CTP 配置类型不匹配")
    executor = CTPExecutor(TradeChannel.CTP)
    try:
        executor.account_config = config

        def progress(step, status):
            details = _step_details(executor, step) if status == "passed" else None
            if details is None:
                report(step, status)
            else:
                report(step, status, details)

        executor.set_connection_progress(progress)
        executor.set_trading_calendar(ShinnyTradingCalendar())
        executor.set_channel_calendar("china")
        # 验证尚无持久化账户，但 guard 需要完整身份才会启用。
        # 每次会话使用独立的内存身份；不查询数据库，也不持久化其控制记录。
        validation_identity = uuid4()
        executor.set_account_control_guard(
            AccountControlGuard(
                account_id=validation_identity.int,
                execution_id=f"connection-validation-{validation_identity.hex}",
                channel=TradeChannel.CTP,
                policy=resolve_account_control_policy("ctp"),
                baseline=AccountControlCounterSnapshot(),
            )
        )
        executor._initialize_connection(config)
    finally:
        executor.close()


def _settlement_details(record: dict[str, object]) -> dict[str, object]:
    """将柜台确认记录转换为通用详情行；原始账号完整保留。"""
    fields = (
        ("确认日期", "confirm_date"),
        ("确认时间", "confirm_time"),
        ("结算编号", "settlement_id"),
        ("经纪公司代码", "broker_id"),
        ("投资者代码", "investor_id"),
        ("资金账号", "account_id"),
        ("币种", "currency_id"),
    )
    return {
        "title": "结算确认记录",
        "rows": [
            {"key": "确认方式", "value": "当日已有确认" if record.get("source") == "existing" else "本次连接自动确认"},
            *({"key": label, "value": record.get(key)} for label, key in fields),
        ],
    }


def _step_details(executor: CTPExecutor, step: str) -> dict[str, object] | None:
    """只为有可读业务数据的阶段构造详情。"""
    if step == "settlement" and executor.settlement_confirmation is not None:
        return _settlement_details(executor.settlement_confirmation)
    if step == "instruments":
        return instrument_details(executor._instruments, queried=executor._catalog_queried)
    builders = {
        "trader_login": login_details,
        "query_orders": order_details,
        "query_trades": trade_details,
        "query_positions": position_details,
        "query_account": account_details,
    }
    if step in builders and step in executor.connection_records:
        return builders[step](executor.connection_records[step])
    return None
