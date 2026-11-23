"""调仓信号渠道的账户配置与静态能力。"""

from dataclasses import dataclass
from math import isfinite

from axile.channels.contracts import (
    ChannelAccountForm,
    ChannelDefaults,
    ChannelDescriptor,
    ChannelLeverage,
    ChannelPlugin,
    ChannelSchedule,
)
from axile.common.trade_channel import TradeChannel
from axile.executor.models.unified_account_assets import Position
from axile.executor.models.unified_input_accounts import BaseAccountConfig

SIGNAL_CHANNEL = "rebalance_signal"
SIGNAL_EVENT = "signal.calculated"


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
class SignalAccountConfig(BaseAccountConfig):
    """账户数据来源和用户自选的行情函数。"""

    channel_type: TradeChannel = TradeChannel(SIGNAL_CHANNEL)
    manual_assets: SignalAssets | None = None
    account_code: str | None = None
    quote_code: str

    def validate(self) -> None:
        """校验所选账户来源与行情函数配置。"""
        if not self.quote_code.strip():
            raise ValueError("请提供自定义行情函数")
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
