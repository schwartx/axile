"""执行器插件工厂测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from axile.channels import (
    AlgorithmReference,
    ChannelAccountForm,
    ChannelCalendar,
    ChannelDefaults,
    ChannelDescriptor,
    ChannelLeverage,
    ChannelPlugin,
    ChannelPortfolioPreset,
    ChannelSchedule,
    list_channels,
    register_channel,
    registry,
)
from axile.executor.models.unified_input_accounts import BaseAccountConfig
from axile.server.execution.factory import create_executor_instance


class _Config(BaseAccountConfig):
    """工厂测试账户配置。"""

    key: str


def _target_transform(config: dict[str, float], frame: pd.DataFrame) -> pd.DataFrame:
    del config
    return frame


def _plugin() -> ChannelPlugin:
    def create_executor(config: BaseAccountConfig) -> SimpleNamespace:
        return SimpleNamespace(
            config=config,
            set_trading_calendar=MagicMock(),
            set_channel_calendar=MagicMock(),
        )

    return ChannelPlugin(
        descriptor=ChannelDescriptor(
            channel="factory-demo",
            label="Factory Demo",
            description="测试执行器工厂",
            icon="plug",
            market="demo",
            schedule=ChannelSchedule(kind="continuous"),
            currency="USD",
            defaults=ChannelDefaults(
                account_control_preset="default",
                long_leverage=1,
                short_leverage=1,
                execution_timeout=60,
                trade_algorithm=AlgorithmReference(method="SINGLE-MAKER", params={}),
            ),
            leverage=ChannelLeverage(min=0, max=2, step=0.1),
            account_form=ChannelAccountForm(),
            calendar=ChannelCalendar(calendar_id="china", label="中国交易日历"),
            portfolio=ChannelPortfolioPreset(market_label="Vendor", example_symbols=("DEMO",)),
        ),
        account_config_model=_Config,
        create_executor=create_executor,
        target_transform=_target_transform,
    )


def test_create_executor_instance_uses_registered_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoEntryPoints(list[object]):
        def select(self, **_kwargs: object) -> _NoEntryPoints:
            return self

    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _NoEntryPoints())
    registry._reset_registry_for_tests()
    list_channels()
    register_channel(_plugin())
    try:
        executor = create_executor_instance(
            SimpleNamespace(trade_channel="factory-demo", account_config={"key": "secret"})
        )
        assert isinstance(executor.config, _Config)
        assert executor.config.channel_type == "factory-demo"
        executor.set_trading_calendar.assert_called_once()
        executor.set_channel_calendar.assert_called_once_with("china")
    finally:
        registry._reset_registry_for_tests()
