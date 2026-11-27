"""调仓信号渠道的账户配置与静态能力。"""

from dataclasses import dataclass, field
from decimal import Decimal
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
from axile.channels.market_data import MarketDataConfiguration
from axile.channels.registry import list_channels
from axile.common.trade_channel import TradeChannel
from axile.executor.gm_market_data import GMMarketDataConfig
from axile.executor.models.unified_account_assets import Position
from axile.executor.models.unified_input_accounts import BaseAccountConfig, validate_connection_endpoint

SIGNAL_CHANNEL = "rebalance_signal"
SIGNAL_EVENT = "signal.calculated"
TUSHARE_API_URL = "https://api.tushare.pro"
_QUOTE_MARKETS = {"gm": "ashare", "tushare": "ashare", "ctp": "ctp", "tq": "ctp"}


def signal_market_descriptor(market: str) -> ChannelDescriptor:
    """按组合使用的市场名称获取渠道声明的市场规则。"""
    for plugin in list_channels():
        descriptor = plugin.descriptor
        if descriptor.market and descriptor.portfolio and descriptor.portfolio.market_label == market:
            return descriptor
    raise ValueError("请选择已注册的调仓信号市场")


def validate_signal_quote_market(market: str, kind: str) -> None:
    """校验账户的预置行情与所选市场一致。"""
    descriptor = signal_market_descriptor(market)
    if kind != "custom" and _QUOTE_MARKETS[kind] != descriptor.market:
        raise ValueError("所选行情来源不支持该市场")


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

    kind: Literal["gm", "tushare", "ctp", "tq", "custom"]
    token: str = field(default="", repr=False)
    connection_mode: Literal["terminal", "service"] = "terminal"
    serv_addr: str | None = None
    terminal_path: str | None = None
    url: str = TUSHARE_API_URL
    md_front: str = ""
    tq_username: str = field(default="", repr=False)
    tq_password: str = field(default="", repr=False)
    code: str = ""

    __forbid_unknown__ = True

    def gm_values(self) -> dict[str, object]:
        """使用与掘金独立行情相同的连接字段。"""
        return {"token": self.token, "connection_mode": self.connection_mode, "serv_addr": self.serv_addr}

    def gm_config(self) -> GMMarketDataConfig:
        """提取掘金行情终端设置。"""
        return GMMarketDataConfig.from_account_config(self.gm_values(), terminal_path=self.terminal_path)

    def futures_config(self) -> MarketDataConfiguration:
        """从行情草稿构建独立期货行情配置。"""
        if self.kind == "ctp":
            try:
                from axile.executor.ctp.market_data import CtpMarketDataConfig
            except ImportError as exc:
                raise ValueError("当前服务未安装 CTP 行情依赖") from exc
            return CtpMarketDataConfig.from_front(self.md_front)

        from axile.executor.tq.market_data import TQMarketDataConfig

        return TQMarketDataConfig.from_account_config(
            {"tq_username": self.tq_username, "tq_password": self.tq_password}
        )

    def validate(self) -> None:
        """在保存和试算时校验所选来源的配置。"""
        if self.kind == "gm":
            self.gm_config()
        elif self.kind in ("ctp", "tq"):
            self.futures_config()
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
    follow_target: bool = False
    follow_fee_bps: float = 5
    quote_source: SignalQuoteSource

    @classmethod
    def prepare(cls, config: dict[str, object]) -> dict[str, object]:
        """将已保存的小数费率转换为基点。"""
        if "follow_fee_rate" not in config:
            return config
        normalized = config.copy()
        rate = normalized.pop("follow_fee_rate")
        if type(rate) in (int, float):
            rate = float(Decimal(str(rate)) * 10_000)
        normalized.setdefault("follow_fee_bps", rate)
        return normalized

    def validate(self) -> None:
        """校验所选账户数据来源。"""
        if self.account_code is not None and not self.account_code.strip():
            raise ValueError("自定义账户函数不能为空")
        if not 0 <= self.follow_fee_bps <= 10_000:
            raise ValueError("跟随手续费率必须在 0 到 10000 bp 之间")
        if self.account_code is not None and self.follow_target:
            raise ValueError("自定义账户函数提供实仓，不能开启跟随目标")
        if self.account_code is None and self.manual_assets is None:
            raise ValueError("请录入资金和持仓，或配置自定义账户函数")


def signal_plugin() -> ChannelPlugin:
    """发出所选市场的目标数量及通知。"""
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
            leverage=ChannelLeverage(min=0, max=125, step=0.1),
            account_form=ChannelAccountForm(),
            schedule=ChannelSchedule(kind="continuous"),
            portfolio=None,
        ),
        account_config_model=SignalAccountConfig,
    )
