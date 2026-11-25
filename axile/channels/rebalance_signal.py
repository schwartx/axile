"""调仓信号渠道的账户配置与静态能力。"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Literal, NotRequired, TypedDict

from axile.channels.contracts import (
    ChannelAccountForm,
    ChannelDefaults,
    ChannelDescriptor,
    ChannelLeverage,
    ChannelPlugin,
    ChannelSchedule,
)
from axile.common.trade_channel import TradeChannel
from axile.executor.gm_market_data import GMMarketDataConfig
from axile.executor.models.unified_account_assets import Position
from axile.executor.models.unified_input_accounts import BaseAccountConfig, validate_connection_endpoint

SIGNAL_CHANNEL = "rebalance_signal"
SIGNAL_EVENT = "signal.calculated"
TUSHARE_API_URL = "https://api.tushare.pro"


class SignalAccountContext(TypedDict):
    """账户取数函数收到的账户和市场信息。"""

    account_id: int | None
    market: str
    name: NotRequired[str]


@dataclass(kw_only=True)
class SignalAssets:
    """用户录入或自定义函数返回的账户观测，缺省持仓表示未知。"""

    total_asset: float
    available_cash: float
    currency: str
    positions: list[Position] | None = None

    __forbid_unknown__ = True

    def validate(self) -> None:
        """校验人工录入或函数返回的观测。"""
        if not self.currency.strip() or not all(isfinite(v) for v in (self.total_asset, self.available_cash)):
            raise ValueError("资金必须是有限数值，并指定计价币种")
        if self.positions is not None:
            keys = {(row.symbol, row.direction) for row in self.positions}
            if len(keys) != len(self.positions):
                raise ValueError("持仓中有重复的标的和方向")
            if any(
                not row.symbol.strip() or not 0 <= row.available_volume <= row.volume < float("inf")
                for row in self.positions
            ):
                raise ValueError("持仓必须提供标的和有效数量")


@dataclass(kw_only=True)
class SignalQuoteSource:
    """调仓信号使用的一种独立行情来源。"""

    kind: Literal["gm", "tushare", "custom"]
    token: str = field(default="", repr=False)
    connection_mode: Literal["terminal", "service"] = "terminal"
    serv_addr: str | None = None
    terminal_path: str | None = None
    url: str = TUSHARE_API_URL
    code: str = ""

    __forbid_unknown__ = True

    def gm_values(self) -> dict[str, object]:
        """使用与掘金独立行情相同的连接字段。"""
        return {"token": self.token, "connection_mode": self.connection_mode, "serv_addr": self.serv_addr}

    def gm_config(self) -> GMMarketDataConfig:
        """提取掘金行情终端设置。"""
        return GMMarketDataConfig.from_account_config(self.gm_values(), terminal_path=self.terminal_path)

    def validate(self) -> None:
        """在保存和试算时校验所选来源的配置。"""
        if self.kind == "gm":
            self.gm_config()
        elif self.kind == "tushare":
            self.token = self.token.strip()
            if not self.token or any(character.isspace() for character in self.token):
                raise ValueError("请填写有效的 Tushare Token")
            self.url = validate_connection_endpoint(
                self.url,
                scheme="required",
                allowed_schemes=frozenset({"http", "https"}),
                port="optional",
                allow_path=True,
            )
        elif not self.code.strip():
            raise ValueError("请提供自定义行情函数")


@dataclass(kw_only=True)
class SignalAccountConfig(BaseAccountConfig):
    """账户数据和行情来源。"""

    channel_type: TradeChannel = TradeChannel(SIGNAL_CHANNEL)
    manual_assets: SignalAssets | None = None
    account_code: str | None = None
    quote_source: SignalQuoteSource

    def validate(self) -> None:
        """校验所选账户数据来源。"""
        if self.account_code is not None and not self.account_code.strip():
            raise ValueError("自定义账户函数不能为空")
        if self.account_code is None and self.manual_assets is None:
            raise ValueError("请录入资金和持仓，或配置自定义账户函数")


def signal_plugin() -> ChannelPlugin:
    """发出目标数量及通知的渠道，市场由当前组合决定。"""
    return ChannelPlugin(
        descriptor=ChannelDescriptor(
            channel=SIGNAL_CHANNEL,
            label="调仓信号",
            description="计算目标持仓并推送通知，由用户自行交易",
            icon="signal",
            market=None,
            currency="",
            defaults=ChannelDefaults(
                long_leverage=1,
                short_leverage=1,
                execution_timeout=30,
                trade_algorithm=None,
            ),
            leverage=ChannelLeverage(min=1, max=1, step=1),
            account_form=ChannelAccountForm(),
            schedule=ChannelSchedule(kind="continuous"),
            portfolio=None,
        ),
        account_config_model=SignalAccountConfig,
    )
