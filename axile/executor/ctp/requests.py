"""普通 OpenCTP 请求字段构造函数。"""

from __future__ import annotations

from openctp_ctp import thostmduserapi as md
from openctp_ctp import thosttraderapi as td

from axile.executor.ctp_input_validation import validate_config, validate_instrument
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.order_input_validation import normalize_order_enums

_OFFSETS = {
    "open": td.THOST_FTDC_OF_Open,
    "close": td.THOST_FTDC_OF_Close,
    "close_today": td.THOST_FTDC_OF_CloseToday,
    "close_yesterday": td.THOST_FTDC_OF_CloseYesterday,
    "开": td.THOST_FTDC_OF_Open,
    "平": td.THOST_FTDC_OF_Close,
    "平今": td.THOST_FTDC_OF_CloseToday,
    "平昨": td.THOST_FTDC_OF_CloseYesterday,
}


def resolve_offset(value: object) -> str:
    """将算法开平标志转换为 OpenCTP 常量。"""
    offset = _OFFSETS.get(str(value), str(value))
    if offset not in set(_OFFSETS.values()):
        raise ValueError(f"不支持的开平标志: {value}")
    return offset


def build_authenticate(config: CTPAccountConfig) -> object:
    """构造交易认证请求。"""
    validate_config(config)
    req = td.CThostFtdcReqAuthenticateField()
    req.BrokerID, req.UserID = config.broker_id, config.investor_id
    req.AppID, req.AuthCode = config.app_id, config.auth_code
    return req


def build_trader_login(config: CTPAccountConfig) -> object:
    """构造交易登录请求。"""
    validate_config(config)
    req = td.CThostFtdcReqUserLoginField()
    req.BrokerID, req.UserID, req.Password = config.broker_id, config.investor_id, config.password
    req.UserProductInfo = str(config.product_info or "axile")
    return req


def build_market_login(config: CTPAccountConfig) -> object:
    """构造行情登录请求。"""
    validate_config(config)
    req = md.CThostFtdcReqUserLoginField()
    req.BrokerID, req.UserID, req.Password = config.broker_id, config.investor_id, config.password
    return req


def build_logout(config: CTPAccountConfig) -> object:
    """构造交易登出字段；行情 API 暂不支持登出请求。"""
    req = td.CThostFtdcUserLogoutField()
    req.BrokerID, req.UserID = config.broker_id, config.investor_id
    return req


def build_settlement_confirm(config: CTPAccountConfig) -> object:
    """构造结算确认请求。"""
    validate_config(config)
    req = td.CThostFtdcSettlementInfoConfirmField()
    req.BrokerID, req.InvestorID = config.broker_id, config.investor_id
    return req


def build_query_settlement_confirm(config: CTPAccountConfig) -> object:
    """构造结算确认状态查询请求。"""
    validate_config(config)
    req = td.CThostFtdcQrySettlementInfoConfirmField()
    req.BrokerID, req.InvestorID = config.broker_id, config.investor_id
    return req


def build_query_account(config: CTPAccountConfig) -> object:
    """构造资金查询请求。"""
    validate_config(config)
    req = td.CThostFtdcQryTradingAccountField()
    req.BrokerID, req.InvestorID = config.broker_id, config.investor_id
    return req


def build_query_positions(config: CTPAccountConfig) -> object:
    """构造持仓查询请求。"""
    validate_config(config)
    req = td.CThostFtdcQryInvestorPositionField()
    req.BrokerID, req.InvestorID = config.broker_id, config.investor_id
    return req


def build_query_depth_market_data(symbol: str, exchange_id: str) -> object:
    """按合约构造交易柜台的行情快照查询。"""
    validate_instrument(symbol)
    req = td.CThostFtdcQryDepthMarketDataField()
    req.InstrumentID, req.ExchangeID = symbol, exchange_id
    return req


def build_query_orders(config: CTPAccountConfig, symbol: str | None = None) -> object:
    """构造订单查询请求。"""
    validate_config(config)
    if symbol is not None:
        validate_instrument(symbol, allow_all=True)
    req = td.CThostFtdcQryOrderField()
    req.BrokerID, req.InvestorID = config.broker_id, config.investor_id
    if symbol:
        req.InstrumentID = symbol
    return req


def build_query_trades(config: CTPAccountConfig, symbol: str) -> object:
    """构造成交查询请求。"""
    validate_config(config)
    validate_instrument(symbol, allow_all=True)
    req = td.CThostFtdcQryTradeField()
    req.BrokerID, req.InvestorID, req.InstrumentID = config.broker_id, config.investor_id, symbol
    return req


def build_order_insert(
    config: CTPAccountConfig,
    *,
    symbol: str,
    order_ref: str,
    direction: OrderDirection,
    order_type: OrderType,
    volume: int,
    price: float,
    offset: str,
) -> object:
    """构造普通报单请求。"""
    validate_instrument(symbol)
    direction, order_type = normalize_order_enums(direction, order_type)
    validate_config(config)
    req = td.CThostFtdcInputOrderField()
    req.BrokerID, req.InvestorID, req.UserID = config.broker_id, config.investor_id, config.investor_id
    req.InstrumentID, req.OrderRef = symbol, order_ref
    req.Direction = {OrderDirection.BUY: td.THOST_FTDC_D_Buy, OrderDirection.SELL: td.THOST_FTDC_D_Sell}[direction]
    req.CombOffsetFlag, req.CombHedgeFlag = offset, td.THOST_FTDC_HF_Speculation
    req.OrderPriceType = {
        OrderType.LIMIT: td.THOST_FTDC_OPT_LimitPrice,
        OrderType.MARKET: td.THOST_FTDC_OPT_AnyPrice,
    }[order_type]
    req.LimitPrice, req.VolumeTotalOriginal = price, volume
    req.TimeCondition, req.VolumeCondition, req.MinVolume = td.THOST_FTDC_TC_GFD, td.THOST_FTDC_VC_AV, 1
    req.ContingentCondition, req.ForceCloseReason = td.THOST_FTDC_CC_Immediately, td.THOST_FTDC_FCC_NotForceClose
    return req


def build_order_cancel(config: CTPAccountConfig, *, symbol: str, key: dict[str, object]) -> object:
    """根据稳定订单键构造撤单请求。"""
    validate_instrument(symbol)
    validate_config(config)
    req = td.CThostFtdcInputOrderActionField()
    req.BrokerID, req.InvestorID, req.UserID = config.broker_id, config.investor_id, config.investor_id
    req.InstrumentID, req.ActionFlag = symbol, td.THOST_FTDC_AF_Delete
    req.OrderRef = str(key["order_ref"])
    req.FrontID, req.SessionID = int(key["front_id"]), int(key["session_id"])
    if not req.FrontID or not req.SessionID:
        req.ExchangeID, req.OrderSysID = str(key["exchange_id"]), str(key["order_sys_id"])
    return req


__all__ = [
    "build_authenticate",
    "build_market_login",
    "build_order_cancel",
    "build_order_insert",
    "build_query_account",
    "build_query_orders",
    "build_query_positions",
    "build_query_settlement_confirm",
    "build_query_trades",
    "build_settlement_confirm",
    "build_trader_login",
    "resolve_offset",
]
