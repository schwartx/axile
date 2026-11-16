"""执行器相关共享fixtures."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.models.unified_account_assets import (
    Position,
    UnifiedAccountAssets,
)
from axile.executor.models.unified_order import UnifiedOrder


@pytest.fixture
def mock_base_executor():
    """创建模拟的基础执行器."""
    from axile.executor.abstract_executor.base import AbstractExecutor

    executor = MagicMock(spec=AbstractExecutor)
    executor.logger = MagicMock()
    executor.channel_type = TradeChannel("external")
    return executor


@pytest.fixture
def sample_account_assets():
    """创建示例账户资产."""
    return UnifiedAccountAssets(
        available_cash=10000.0,
        total_asset=15000.0,
        market_value=5000.0,
        positions=[
            Position(
                symbol="rb2610",
                position_side="LONG",
                volume=0.5,
                available_volume=0.5,
                avg_price=30000.0,
                unrealized_pnl=500.0,
            )
        ],
    )


@pytest.fixture
def sample_order():
    """创建示例订单."""
    return UnifiedOrder.create(
        order_id="TEST_001",
        symbol="rb2610",
        direction="BUY",
        order_type="LIMIT",
        volume=0.1,
        price=30000.0,
    )


@pytest.fixture
def mock_ctp_trader():
    """创建模拟CTP交易器."""
    trader = MagicMock()
    trader.instruments = {"rb2603": object(), "m2605&m2605": object()}
    trader.get_positions_summary = MagicMock(return_value=[])
    return trader


@pytest.fixture
def ctp_executor(mock_ctp_trader):
    """创建CTP执行器实例."""
    from axile.executor.ctp.ctp_execute import CTPExecutor

    executor = CTPExecutor.__new__(CTPExecutor)
    executor.channel_type = TradeChannel("ctp")
    executor.logger = MagicMock()
    executor.trader = mock_ctp_trader
    executor.md_client = MagicMock()
    executor.execution_start_time = None
    return executor
