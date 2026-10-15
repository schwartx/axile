"""开放交易渠道插件注册表测试。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import Field, ValidationError

from axile.channels import (
    AlgorithmReference,
    ChannelAccountField,
    ChannelAccountForm,
    ChannelDefaults,
    ChannelDescriptor,
    ChannelLeverage,
    ChannelPlugin,
    ChannelPortfolioPreset,
    ChannelSchedule,
    ChannelUi,
    ChannelUnits,
    get_channel,
    list_channels,
    register_channel,
    registry,
)
from axile.executor.models.unified_input_accounts import BaseAccountConfig
from axile.server.api.routes.capabilities import list_channel_capabilities
from axile.server.execution.factory import create_executor_instance


class DemoAccountConfig(BaseAccountConfig):
    """测试插件账户配置。"""

    token: str = Field(min_length=1)


def _target_transform(config: dict[str, float], frame: pd.DataFrame) -> pd.DataFrame:
    transformed = frame.copy()
    transformed["contribution"] = transformed["weight"] * transformed["strategy"].map(config)
    return transformed


def _plugin(
    channel: str = "vendor-demo",
    *,
    required_modules: tuple[str, ...] = (),
    max_parallel_symbols: int | None = 4,
) -> ChannelPlugin:
    return ChannelPlugin(
        descriptor=ChannelDescriptor(
            channel=channel,
            label="Vendor Demo",
            description="用于验证开放渠道边界",
            icon="plug",
            market="demo",
            schedule=ChannelSchedule(kind="continuous"),
            currency="USD",
            units=ChannelUnits(
                quantity_kind="base_asset",
                quantity_label="资产",
                quantity_max_decimals=8,
                price_label="USD",
            ),
            ui=ChannelUi(
                account_connect_lead="填写供应商令牌。",
                leverage_note="支持双向目标仓位",
            ),
            defaults=ChannelDefaults(
                long_leverage=2,
                short_leverage=1,
                execution_timeout=90,
                trade_algorithm=AlgorithmReference(method="DEMO", params={"pace": 2}),
                empty_positions_algorithm=AlgorithmReference(method="DEMO-EMPTY", params={}),
            ),
            leverage=ChannelLeverage(min=0, max=10, step=0.5),
            account_form=ChannelAccountForm(
                fields=(ChannelAccountField(name="token", label="Token", kind="secret", width="full"),)
            ),
            portfolio=ChannelPortfolioPreset(market_label="Vendor", example_symbols=("DEMO",)),
        ),
        account_config_model=DemoAccountConfig,
        create_executor=lambda config: SimpleNamespace(config=config),
        target_transform=_target_transform,
        execution_backend="process",
        required_modules=required_modules,
        max_parallel_symbols=max_parallel_symbols,
    )


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """隔离每个测试的入口点与进程内注册状态。"""

    class _NoEntryPoints(list[object]):
        def select(self, **_kwargs: object) -> _NoEntryPoints:
            return self

    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _NoEntryPoints())
    registry._reset_registry_for_tests()
    yield
    registry._reset_registry_for_tests()


def test_builtin_channels_have_stable_order_and_descriptor_shape() -> None:
    plugins = list_channels()

    assert all(plugin.execution_backend == "process" for plugin in plugins)
    assert [plugin.descriptor.channel for plugin in plugins] == ["ctp", "gm", "tq"]
    assert [plugin.descriptor.schedule.kind for plugin in plugins] == ["cn_futures", "cn_stock", "cn_futures"]
    assert plugins[0].descriptor.schedule.night == plugins[2].descriptor.schedule.night
    assert plugins[0].descriptor.schedule.night is not None
    assert plugins[0].descriptor.schedule.night.close == ("02:25",)
    assert plugins[0].descriptor.schedule.night.m15[0] == "21:00"
    assert plugins[0].descriptor.schedule.night.m15[-1] == "02:29"
    assert [window.start for window in plugins[0].descriptor.schedule.windows] == ["09:00", "13:00", "21:00"]
    assert [window.end for window in plugins[1].descriptor.schedule.windows] == ["11:30", "15:00"]
    assert plugins[1].descriptor.schedule.night is None
    assert plugins[0].descriptor.account_form.fields
    assert plugins[0].descriptor.defaults.trade_algorithm.method == "TARGET-POS-TASK"
    assert plugins[0].descriptor.portfolio.market_label == "期货"
    assert plugins[0].descriptor.portfolio.example_symbols == ("rb2610", "ag2612")
    assert plugins[0].descriptor.units.quantity_label == "手"
    assert plugins[1].descriptor.units.quantity_label == "股"
    assert plugins[1].descriptor.ui.show_short_leverage is False
    assert plugins[2].descriptor.defaults.trade_algorithm.method == "TARGET-POS-TASK"
    assert plugins[1].descriptor.portfolio.market_label == "A股"
    assert plugins[1].descriptor.portfolio.example_symbols == ("600000.SH", "000001.SZ")
    assert plugins[2].descriptor.portfolio == plugins[0].descriptor.portfolio
    assert [plugin.max_parallel_symbols for plugin in plugins] == [None, None, None]
    assert plugins[2].descriptor.account_form.fields[0].default is None
    assert [plugin.descriptor.calendar.calendar_id for plugin in plugins if plugin.descriptor.calendar] == [
        "china",
        "china",
        "china",
    ]


def test_plugins_default_to_process_and_reject_removed_thread_backend() -> None:
    plugin = _plugin()
    assert ChannelPlugin.__dataclass_fields__["execution_backend"].default == "process"
    with pytest.raises(ValueError, match="仅支持 process"):
        replace(plugin, execution_backend="thread")  # type: ignore[arg-type]


def test_entry_point_is_loaded_once_and_appended_after_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    class _EntryPoint:
        name = "vendor"
        value = "vendor.plugin:channel"

        def load(self) -> ChannelPlugin:
            loads.append(self.name)
            return _plugin()

    class _EntryPoints(list[_EntryPoint]):
        def select(self, **kwargs: object) -> _EntryPoints:
            return self if kwargs == {"group": "axile.channels"} else _EntryPoints()

    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _EntryPoints([_EntryPoint()]))

    assert get_channel("vendor-demo").descriptor.label == "Vendor Demo"
    assert list_channels()[-1].descriptor.channel == "vendor-demo"
    assert loads == ["vendor"]


def test_factory_validates_plugin_config_before_creating_executor() -> None:
    list_channels()
    register_channel(_plugin())
    account = SimpleNamespace(trade_channel="vendor-demo", account_config={"token": "secret"})

    executor = create_executor_instance(account)

    assert isinstance(executor.config, DemoAccountConfig)
    assert executor.config.channel_type == "vendor-demo"

    with pytest.raises(ValidationError):
        create_executor_instance(SimpleNamespace(trade_channel="vendor-demo", account_config={"token": ""}))


def test_capabilities_expose_descriptor_and_runtime_availability() -> None:
    list_channels()
    register_channel(_plugin(required_modules=("module_that_does_not_exist_for_axile_test",)))

    capability = next(item for item in list_channel_capabilities() if item.channel == "vendor-demo")

    assert capability.label == "Vendor Demo"
    assert capability.currency == "USD"
    assert capability.units.quantity_kind == "base_asset"
    assert capability.units.quantity_max_decimals == 8
    assert capability.ui.account_connect_lead == "填写供应商令牌。"
    assert capability.ui.position_value_label == "市值"
    assert capability.defaults.execution_timeout == 90
    assert capability.account_form.fields[0].name == "token"
    assert capability.portfolio.market_label == "Vendor"
    assert capability.portfolio.example_symbols == ("DEMO",)
    assert capability.available is False
    assert capability.missing_packages == ["module_that_does_not_exist_for_axile_test"]
