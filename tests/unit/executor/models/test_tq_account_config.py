from __future__ import annotations

import pytest

from axile.common.serialization import from_payload, parse_input
from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.core.base import get_algorithm_metadata
from axile.executor.models.unified_input import TQAccountConfig, UnifiedStandardInput


def test_live_mode_requires_explicit_selection_and_live_fields() -> None:
    config = from_payload(
        TQAccountConfig,
        {
            "account_mode": "live",
            "tq_username": "user",
            "tq_password": "secret",
            "broker_name": "broker",
            "account_id": "account",
            "account_password": "trade-secret",
            "initial_balance": 123,
        },
    )

    assert config.channel_type == TradeChannel("tq")
    assert config.account_mode == "live"
    assert config.broker_name == "broker"
    assert config.initial_balance == 10_000_000


def test_live_requires_broker_credentials() -> None:
    with pytest.raises(ValueError, match="TqAccount 模式缺少必填字段"):
        parse_input(TQAccountConfig, {"account_mode": "live", "tq_username": "user", "tq_password": "secret"})


def test_account_mode_has_no_default_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="account_mode"):
        parse_input(TQAccountConfig, {"tq_username": "user", "tq_password": "secret"})
    with pytest.raises(ValueError, match="extra_forbidden"):
        parse_input(
            TQAccountConfig, {"account_mode": "kq", "tq_username": "user", "tq_password": "secret", "legacy": True}
        )


def test_unified_input_uses_tq_plugin_defaults() -> None:
    standard_input = from_payload(
        UnifiedStandardInput,
        {
            "channel_type": "tq",
            "account_config": {"account_mode": "kq", "tq_username": "user", "tq_password": "secret"},
            "curr_target": {"rb2610": 0.2},
        },
    )

    assert isinstance(standard_input.account_config, TQAccountConfig)
    assert standard_input.algorithm == {"method": "TARGET-POS-TASK", "params": {}}
    assert get_algorithm_metadata("TARGET-POS-TASK").channels == frozenset({"ctp", "tq"})
