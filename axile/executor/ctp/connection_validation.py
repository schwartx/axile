"""验证临时 CTP 会话，复用原有连接顺序、查询屏障与账户控制。"""

from types import MappingProxyType
from uuid import uuid4

from openctp_ctp import thosttraderapi as td

from axile.channels.contracts import ConnectionProgress
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.connection_reconciliation import reconcile_account_connection
from axile.executor.connection_validation import validation_progress
from axile.executor.ctp.ctp_execute import CTPExecutor, CtpRequestError
from axile.executor.instrument_catalog_records import InstrumentRecord
from axile.executor.models.unified_input_accounts import BaseAccountConfig, CTPAccountConfig
from axile.executor.trading_calendar import ShinnyTradingCalendar


def _load_sample(executor: CTPExecutor) -> dict[str, InstrumentRecord]:
    """连接诊断始终真实查询螺纹钢样本，不经过共享缓存。"""
    request = td.CThostFtdcQryInstrumentField()
    request.ExchangeID, request.ProductID = "SHFE", "rb"
    executor._catalog_queried = True
    rows = executor._query("ReqQryInstrument", request)
    catalog = {
        str(row.InstrumentID): InstrumentRecord(MappingProxyType(vars(row)))
        for row in rows
        if getattr(row, "InstrumentID", "")
        and getattr(row, "ExchangeID", "") == "SHFE"
        and getattr(row, "ProductID", "") == "rb"
    }
    if not catalog:
        raise CtpRequestError("螺纹钢合约查询返回空结果")
    return catalog


def validate_connection(config: BaseAccountConfig, report: ConnectionProgress) -> None:
    """只创建临时执行器；无论验证结果如何，都释放原生 API。"""
    if not isinstance(config, CTPAccountConfig):
        raise TypeError("CTP 配置类型不匹配")
    executor = CTPExecutor(TradeChannel.CTP)
    try:
        executor.account_config = config

        executor._validation_catalog = lambda: _load_sample(executor)
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
        with validation_progress("ctp", report):
            executor._initialize_connection(config)
            reconcile_account_connection(executor)
    finally:
        executor.close()
