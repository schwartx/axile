"""统一输入与开放渠道配置校验测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import Field, ValidationError

from axile.channels import (
    AlgorithmReference,
    ChannelAccountForm,
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
from axile.executor.models.unified_input import GMAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_input_accounts import BaseAccountConfig


def _ctp_payload() -> dict[str, object]:
    return {
        "channel_type": "ctp",
        "account_config": {
            "broker_id": "9999",
            "investor_id": "000001",
            "password": "secret",
            "td_front": "tcp://td:10001",
            "md_front": "tcp://md:10002",
            "app_id": "app-id",
            "auth_code": "auth-code",
        },
        "curr_target": {"rb2505": 1.0},
    }


def test_from_dict_requires_explicit_channel() -> None:
    payload = _ctp_payload()
    payload.pop("channel_type")

    with pytest.raises(ValueError, match="必须明确指定 channel_type"):
        UnifiedStandardInput.from_dict(payload)


def test_gm_config_requires_explicit_connection_mode() -> None:
    payload = {
        "channel_type": "gm",
        "account_config": {
            "account_id": "account-id",
            "token": "token",
            "terminal_path": "C:\\GoldMiner3",
            "serv_addr": "127.0.0.1:7001",
        },
        "curr_target": {"SHSE.600000": 1.0},
    }

    with pytest.raises(ValidationError, match="connection_mode"):
        UnifiedStandardInput.from_dict(payload)


def test_gm_config_serializes_only_active_connection_target() -> None:
    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": "gm",
            "account_config": {
                "connection_mode": "service",
                "account_id": "account-id",
                "token": "token",
                "terminal_path": "C:\\ignored",
                "serv_addr": "127.0.0.1:7001",
            },
            "curr_target": {"SHSE.600000": 1.0},
        }
    )

    assert isinstance(standard_input.account_config, GMAccountConfig)
    assert standard_input.to_dict()["account_config"] == {
        "channel_type": "gm",
        "connection_mode": "service",
        "account_id": "account-id",
        "token": "token",
        "serv_addr": "127.0.0.1:7001",
    }


def test_external_plugin_controls_unknown_channel_config_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExternalConfig(BaseAccountConfig):
        account_ref: str = Field(min_length=1)

    class _NoEntryPoints(list[object]):
        def select(self, **_kwargs: object) -> _NoEntryPoints:
            return self

    def target_transform(config: dict[str, float], frame: pd.DataFrame) -> pd.DataFrame:
        del config
        return frame

    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _NoEntryPoints())
    registry._reset_registry_for_tests()
    list_channels()
    register_channel(
        ChannelPlugin(
            descriptor=ChannelDescriptor(
                channel="external-demo",
                label="External Demo",
                description="测试外部渠道",
                icon="plug",
                market="demo",
                schedule=ChannelSchedule(kind="continuous"),
                currency="USD",
                defaults=ChannelDefaults(
                    account_control_preset="default",
                    long_leverage=1,
                    short_leverage=1,
                    execution_timeout=30,
                    trade_algorithm=AlgorithmReference(method="SINGLE-MAKER", params={}),
                ),
                leverage=ChannelLeverage(min=0, max=2, step=0.1),
                account_form=ChannelAccountForm(),
                portfolio=ChannelPortfolioPreset(market_label="Demo", example_symbols=("X",)),
            ),
            account_config_model=ExternalConfig,
            create_executor=lambda config: SimpleNamespace(config=config),
            target_transform=target_transform,
        )
    )
    try:
        standard_input = UnifiedStandardInput.from_dict(
            {
                "channel_type": "external-demo",
                "account_config": {"account_ref": "A-1"},
                "curr_target": {"X": 1.0},
            }
        )
        assert isinstance(standard_input.account_config, ExternalConfig)
        assert standard_input.channel_type == "external-demo"
    finally:
        registry._reset_registry_for_tests()
