"""统一目标数量换算证据回归测试."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from loguru import logger

from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.models.execution_result import TargetSizingStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.tq.tq_execute import TQExecutor
from tests.unit.executor.ctp._quote_test_support import fresh_quote


def _assets(equity: float = 100_000.0) -> UnifiedAccountAssets:
    return UnifiedAccountAssets(
        available_cash=equity,
        total_asset=equity,
        market_value=0.0,
        positions=[],
    )


def _with_sizing_quote(executor):
    # 此处只验证数量换算；新鲜度门禁由 CTP F12 入口回归覆盖。
    executor._snapshot_quote_error = lambda symbol, quote: None
    executor._quotes = {"m2701": fresh_quote("m2701", "")}
    executor._quotes["m2701"].last_price = 3000


def test_ctp_weight_below_one_contract_is_structured_quantization() -> None:
    executor = object.__new__(CTPExecutor)
    executor._instruments = {"m2701": SimpleNamespace(VolumeMultiple=10, PriceTick=1)}
    _with_sizing_quote(executor)

    sizing = executor._calculate_generic_sizing(0.01, 3_000.0, _assets(), {}, symbol="m2701")

    assert sizing.raw_quantity == pytest.approx(1 / 30)
    assert sizing.target_quantity == 0
    assert sizing.unit_notional == 30_000
    assert sizing.reason_code == "COMMON.SIZING.BELOW_MIN_QUANTITY"


def test_ctp_direct_lots_mode_does_not_treat_input_as_weight() -> None:
    executor = object.__new__(CTPExecutor)
    executor._instruments = {}

    sizing = executor._calculate_generic_sizing(
        -2.8,
        0.0,
        _assets(),
        {"sizing_mode": "lots"},
        symbol="TA701",
    )

    assert sizing.sizing_mode == "lots"
    assert sizing.raw_quantity == -2.8
    assert sizing.target_quantity == -2
    assert sizing.reference_price is None
    assert sizing.reason_code == "COMMON.SIZING.QUANTIZED"


def test_ctp_missing_contract_multiplier_is_unavailable() -> None:
    executor = object.__new__(CTPExecutor)
    executor._instruments = {"m2701": SimpleNamespace(PriceTick=1)}
    _with_sizing_quote(executor)

    sizing = executor._calculate_generic_sizing(0.2, 3_000.0, _assets(), {}, symbol="m2701")

    assert sizing.status is TargetSizingStatus.UNAVAILABLE
    assert sizing.target_quantity is None
    assert sizing.reason_code == "COMMON.SIZING.MISSING_UNIT_MULTIPLIER"


def test_tq_weight_below_one_contract_uses_quote_multiplier() -> None:
    executor = object.__new__(TQExecutor)
    executor._quote_snapshot = lambda _symbol: {"volume_multiple": 10}

    sizing = executor._calculate_generic_sizing(-0.01, 3_000.0, _assets(), {}, symbol="DCE.m2701")

    assert sizing.raw_quantity == pytest.approx(-1 / 30)
    assert sizing.target_quantity == 0
    assert sizing.reason_code == "COMMON.SIZING.BELOW_MIN_QUANTITY"


class _GenericExecutor(AbstractExecutorCapabilityMixin):
    logger = logger

    def get_min_notional(self, _symbol: str) -> None:
        return None


def test_missing_market_data_is_not_silently_rendered_as_zero() -> None:
    executor = _GenericExecutor()

    decisions = executor.calculate_target_sizing(
        {"missing": 0.2},
        _assets(),
        {},
        {},
        {},
    )

    assert decisions["missing"].status is TargetSizingStatus.UNAVAILABLE
    assert decisions["missing"].target_quantity is None
    assert decisions["missing"].reason_code == "COMMON.SIZING.MISSING_MARKET_DATA"


def test_invalid_market_price_is_not_silently_rendered_as_zero() -> None:
    executor = _GenericExecutor()

    decisions = executor.calculate_target_sizing(
        {"bad": 0.2},
        _assets(),
        {
            "bad": UnifiedPriceData(
                symbol="bad",
                last_price=0.0,
                asks=[(0.0, 0.0)],
                bids=[(0.0, 0.0)],
                volume=0.0,
                timestamp=0,
                update_time="2026-08-27T14:00:00",
            )
        },
        {},
        {},
    )

    assert decisions["bad"].status is TargetSizingStatus.UNAVAILABLE
    assert decisions["bad"].target_quantity is None
    assert decisions["bad"].reason_code == "COMMON.SIZING.INVALID_PRICE"
