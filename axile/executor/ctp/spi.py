"""OpenCTP SPI 无状态转发器。"""
# ruff: noqa: D102 - OpenCTP 固定命名的覆写回调仅做逐项转发。

from __future__ import annotations

from typing import TYPE_CHECKING

from openctp_ctp import thostmduserapi as md
from openctp_ctp import thosttraderapi as td

if TYPE_CHECKING:
    from axile.executor.ctp.ctp_execute import CTPExecutor


class TraderSpi(td.CThostFtdcTraderSpi):
    """将交易回调原样转发给执行器。"""

    def __init__(self, owner: CTPExecutor) -> None:
        super().__init__()
        self.owner = owner

    def OnFrontConnected(self):
        self.owner._trader_connected_cb()

    def OnFrontDisconnected(self, reason):
        self.owner._disconnected("交易", reason)

    def OnRspAuthenticate(self, row, info, request_id, is_last):
        self.owner._authenticated(row, info, request_id)

    def OnRspUserLogin(self, row, info, request_id, is_last):
        self.owner._logged_in(row, info, request_id)

    def OnRspSettlementInfoConfirm(self, row, info, request_id, is_last):
        self.owner._settled(info, request_id, row)

    def OnRspQrySettlementInfoConfirm(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryInstrument(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryInstrumentCommissionRate(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryTradingAccount(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryInvestorPosition(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryDepthMarketData(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryOrder(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRspQryTrade(self, row, info, request_id, is_last):
        self.owner._query_response(row, info, request_id, is_last)

    def OnRtnOrder(self, row):
        self.owner._on_order(row)

    def OnRtnTrade(self, row):
        self.owner._on_trade(row)

    def OnRspOrderInsert(self, row, info, request_id, is_last):
        self.owner._on_order_insert_error(row, info, source="OnRspOrderInsert")

    def OnErrRtnOrderInsert(self, row, info):
        self.owner._on_order_insert_error(row, info, source="OnErrRtnOrderInsert")

    def OnRspOrderAction(self, row, info, request_id, is_last):
        self.owner._on_control_action_error(row, info, request_id)

    def OnErrRtnOrderAction(self, row, info):
        self.owner._on_control_action_error(row, info)

    def OnRspExecOrderInsert(self, row, info, request_id, is_last):
        self.owner._option_response(row, info)

    def OnErrRtnExecOrderInsert(self, row, info):
        self.owner._option_error(row, info)

    def OnRtnExecOrder(self, row):
        self.owner._option_return(row)

    def OnRspExecOrderAction(self, row, info, request_id, is_last):
        self.owner._option_cancel_response(row, info)

    def OnRspOptionSelfCloseInsert(self, row, info, request_id, is_last):
        self.owner._option_response(row, info)

    def OnErrRtnOptionSelfCloseInsert(self, row, info):
        self.owner._option_error(row, info)

    def OnRtnOptionSelfClose(self, row):
        self.owner._option_return(row)

    def OnRspOptionSelfCloseAction(self, row, info, request_id, is_last):
        self.owner._option_cancel_response(row, info)


class MarketSpi(md.CThostFtdcMdSpi):
    """将行情回调原样转发给执行器。"""

    def __init__(self, owner: CTPExecutor) -> None:
        super().__init__()
        self.owner = owner

    def OnFrontConnected(self):
        self.owner._market_connected_cb()

    def OnFrontDisconnected(self, reason):
        self.owner._disconnected("行情", reason)

    def OnRspUserLogin(self, row, info, request_id, is_last):
        self.owner._market_logged_in(row, info, request_id)

    def OnRspSubMarketData(self, row, info, request_id, is_last):
        self.owner._market_subscribed(row, info)

    def OnRtnDepthMarketData(self, row):
        self.owner._on_quote(row)


__all__ = ["MarketSpi", "TraderSpi"]
