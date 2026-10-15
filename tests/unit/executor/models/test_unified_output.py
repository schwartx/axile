"""UnifiedStandardOutput 当前主路径测试。"""

from axile.common.trade_channel import TradeChannel
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord
from axile.executor.models.unified_output import ExecutionStatus, UnifiedStandardOutput


def _build_account_assets_payload() -> UnifiedAccountAssets:
    """构造一个最小可用的账户资产对象。"""
    return UnifiedAccountAssets.model_validate(
        {
            "available_cash": 100.0,
            "total_asset": 150.0,
            "market_value": 50.0,
            "currency": "CNY",
            "positions": [],
        }
    )


def test_memory_error_does_not_treat_output_as_failed() -> None:
    """memory.error 只作为诊断信息。"""
    output = UnifiedStandardOutput(
        account_assets=UnifiedAccountAssets.model_validate(
            {
                "available_cash": 0.0,
                "total_asset": 0.0,
                "market_value": 0.0,
                "positions": [],
            }
        ),
        inputs=None,
        memory={"error": "debug only"},
        status=ExecutionStatus.SUCCEEDED,
        channel_type=TradeChannel("external"),
    )

    assert output.status == ExecutionStatus.SUCCEEDED
    assert output.success is True
    assert output.error is None


def test_output_aggregates_symbol_trades_separately_from_orders() -> None:
    output = UnifiedStandardOutput(
        account_assets=_build_account_assets_payload(),
        inputs=None,
        memory={},
        status=ExecutionStatus.SUCCEEDED,
        symbol_results={
            "rb2610": AlgorithmResult(
                symbol="rb2610",
                algorithm="TWAP",
                orders=[],
                trades=[
                    TradeRecord(
                        trade_id="trade-1",
                        symbol="rb2610",
                        order_id="order-1",
                        trade_time="2026-03-25T10:00:00",
                        trade_volume=1.0,
                        trade_price=100.0,
                        trade_value=100.0,
                    )
                ],
            )
        },
        channel_type=TradeChannel("external"),
    )

    assert [trade.trade_id for trade in output.trades] == ["trade-1"]
