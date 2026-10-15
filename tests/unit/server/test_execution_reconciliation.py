"""``build_symbol_reconciliation`` 与账户快照溯源标的单元测试.

覆盖逐只对账的到位、欠量、清仓、做空和前仓缺失分支，以及账户级权益与来源标。
"""

import pytest

from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.execution.execution_summaries import build_symbol_reconciliation
from axile.server.execution.worker_backend.worker import (
    _capture_before_account_snapshot as capture_before_worker,
)


class _RaisingExecutor:
    """``get_account_assets`` 抛错的执行器桩，用于验证降级不中断。"""

    def get_account_assets(self) -> UnifiedAccountAssets:
        raise RuntimeError("boom")


class _AssetsExecutor:
    """返回真实账户快照的执行器桩。"""

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(available_cash=1.0, total_asset=1.0, market_value=0.0, positions=[])


class _SessionRecoveryExecutor:
    def get_account_assets(self) -> UnifiedAccountAssets:
        raise CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)


def _long(symbol: str, volume: float) -> dict[str, object]:
    """构造一条多头持仓字典。"""
    return {"symbol": symbol, "volume": volume, "direction": "多头"}


def _short(symbol: str, volume: float) -> dict[str, object]:
    """构造一条空头持仓字典。"""
    return {"symbol": symbol, "volume": volume, "direction": "空头"}


def _order(direction: str, filled: float, avg_price: float = 0.0) -> dict[str, object]:
    """构造一条带成交量与均价的订单字典。"""
    return {"direction": direction, "filled_volume": filled, "avg_price": avg_price}


@pytest.mark.parametrize("direction,sign", [("BUY", 1), ("SELL", -1)])
def test_foreign_quote_fill_value_uses_frozen_account_rate_and_tca_keeps_native_price(
    direction: str, sign: int
) -> None:
    conversion = {"price_currency": "HKD", "value_currency": "CNY", "rate": 1.02}
    order = {**_order(direction, 3.9, 1.5164), "order_id": "o1", "volume": 3.9, "value_conversion": conversion}
    result = {
        "account_assets": {"currency": "CNY", "positions": [], "extra": {"asset_indices": {"HKD": {"index": 3}}}},
        "symbol_results": {
            "00700.HK": {
                "orders": [order],
                "sizing": {"value_conversion": conversion},
                "first_tick": {"bid_price": 1.5163, "ask_price": 1.5165},
                "trades": [{"order_id": "o1", "trade_value": 5.91396, "value_conversion": conversion}],
            }
        },
    }
    row = build_symbol_reconciliation(result, {"positions": []})["symbols"][0]
    assert row["filled_value"] == pytest.approx(sign * 5.91396 * 1.02)
    assert row["filled_native_value"] == pytest.approx(sign * 5.91396)
    assert row["value_currency"] == "CNY" and row["native_currency"] == "HKD"
    assert row["avg_price"] == pytest.approx(1.5164)
    assert row["tca"]["slippage_bps"] == pytest.approx(0, abs=1e-9)
    trade = row["orders"][0]["trades"][0]
    assert trade["value"] == pytest.approx(5.91396 * 1.02)
    assert trade["native_value"] == pytest.approx(5.91396)


def test_two_phase_fill_values_keep_each_orders_planning_rate() -> None:
    conversions = [
        {"price_currency": "HKD", "value_currency": "CNY", "rate": 1.01},
        {"price_currency": "HKD", "value_currency": "CNY", "rate": 1.03},
    ]
    result = {
        "account_assets": {"currency": "CNY", "positions": []},
        "symbol_results": {
            "00700.HK": {
                "orders": [{**_order("SELL", 1, 2), "value_conversion": conversion} for conversion in conversions],
                "sizing": {"value_conversion": conversions[-1]},
            }
        },
    }
    row = build_symbol_reconciliation(result, {"positions": []})["symbols"][0]
    assert row["filled_value"] == pytest.approx(-2 * 1.01 - 2 * 1.03)
    assert row["filled_native_value"] == -4
    assert row["avg_price"] == 2


def test_reconciliation_reached_target_exactly() -> None:
    """买入到位：after≈target 且 drift≈0，reached 为真、attained≈1。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [_long("ag2612", 4.366)], "source": "real"},
        "symbol_results": {
            "ag2612": {"target_volume": 4.366, "orders": [_order("BUY", 4.366)], "status": "SUCCEEDED"},
        },
    }
    before = {"total_asset": 1000.0, "positions": [], "source": "real"}

    recon = build_symbol_reconciliation(result, before)

    row = recon["symbols"][0]
    assert row["symbol"] == "ag2612"
    assert row["target"] == 4.366
    assert row["filled"] == 4.366
    assert row["before"] == 0.0
    assert row["after"] == 4.366
    assert row["moved"] == 4.366
    assert abs(row["drift"]) < 1e-9
    assert abs(row["attained_ratio"] - 1.0) < 1e-9
    assert row["reached"] is True


def test_reconciliation_undershoot_counts_preexisting_position() -> None:
    """欠量到位：已有底仓 + 部分成交，after 由前仓叠加，attained<1、reached 为假。"""
    result = {
        "account_assets": {"total_asset": 5149.0, "positions": [_long("rb2610", 0.1011)], "source": "real"},
        "symbol_results": {
            "rb2610": {"target_volume": 0.1243, "orders": [_order("BUY", 0.0675)], "status": "SUCCEEDED"},
        },
    }
    before = {"total_asset": 5149.0, "positions": [_long("rb2610", 0.0336)], "source": "real"}

    row = build_symbol_reconciliation(result, before)["symbols"][0]

    assert row["before"] == 0.0336
    assert row["after"] == 0.1011
    assert abs(row["moved"] - 0.0675) < 1e-9
    assert abs(row["filled"] - 0.0675) < 1e-9
    assert abs(row["drift"]) < 1e-9
    assert 0.80 < row["attained_ratio"] < 0.82
    assert row["reached"] is False


def test_reconciliation_fill_value_and_avg_price() -> None:
    """成交金额与均价：多单加权、卖出成交额记负号。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [_long("ag2612", 4.366)], "source": "real"},
        "symbol_results": {
            "ag2612": {
                "target_volume": 4.366,
                "orders": [_order("BUY", 2.0, 1800.0), _order("BUY", 2.366, 1750.0)],
                "status": "SUCCEEDED",
            },
        },
    }
    row = build_symbol_reconciliation(result, {"total_asset": 1000.0, "positions": [], "source": "real"})["symbols"][0]

    assert abs(row["filled_value"] - (2.0 * 1800.0 + 2.366 * 1750.0)) < 1e-6
    assert abs(row["avg_price"] - row["filled_value"] / 4.366) < 1e-6

    sell_result = {
        "account_assets": {"total_asset": 1000.0, "positions": [], "source": "real"},
        "symbol_results": {
            "m2609": {"target_volume": 0, "orders": [_order("SELL", 2.34, 566.27)], "status": "SUCCEEDED"},
        },
    }
    sell_row = build_symbol_reconciliation(sell_result, None)["symbols"][0]
    assert sell_row["filled_value"] < 0
    assert abs(sell_row["avg_price"] - 566.27) < 1e-6


def test_symbol_tca_slippage_liquidity_fee_and_tree() -> None:
    """逐只 TCA：卖在卖一=被动+有利滑点、费用取自成交 extra、订单→成交树成形。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [], "source": "real"},
        "symbol_results": {
            "ag2612": {
                "status": "SUCCEEDED",
                "target_volume": 0,
                "first_tick": {"bid_price": 100.0, "ask_price": 100.1, "last_price": 100.05},
                "orders": [
                    {
                        "order_id": "o1",
                        "direction": "OrderDirection.SELL",
                        "order_type": "OrderType.LIMIT",
                        "price": 100.1,
                        "avg_price": 100.1,
                        "volume": 2.0,
                        "filled_volume": 2.0,
                        "status": "已成交",
                        "extra": {"client_order_id": "c1"},
                    }
                ],
                "trades": [
                    {
                        "order_id": "o1",
                        "trade_price": 100.1,
                        "trade_volume": 2.0,
                        "trade_value": 200.2,
                        "trade_time": "2026-07-14T10:00:00",
                        "extra": {"commission": 0.5, "commission_asset": "CNY"},
                    }
                ],
            }
        },
    }
    before = {"total_asset": 1000.0, "positions": [_long("ag2612", 2.0)], "source": "real"}
    row = build_symbol_reconciliation(result, before)["symbols"][0]

    tca = row["tca"]
    assert tca["n_orders"] == 1
    assert tca["n_trades"] == 1
    assert tca["liquidity"] == "passive"
    assert 4.9 < tca["slippage_bps"] < 5.0  # 卖在卖一、高于中间价 → 有利、正号
    assert tca["fee"] == 0.5
    assert tca["fee_asset"] == "CNY"
    assert abs(tca["fill_ratio"] - 1.0) < 1e-9
    assert abs((tca["arrival_mid"] or 0) - 100.05) < 1e-9

    order = row["orders"][0]
    assert order["side"] == "sell"
    assert order["order_type"] == "LIMIT"
    assert order["client_order_id"] == "c1"
    assert len(order["trades"]) == 1
    assert order["trades"][0]["fee"] == 0.5


def test_symbol_tca_counts_zero_trades_when_order_filled_without_trade_records() -> None:
    """订单已成但无成交明细时，TCA 仍按明细笔数为 0，不把 filled_volume 假装成一笔成交."""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [_short("TA701", 3.0)], "source": "real"},
        "symbol_results": {
            "TA701": {
                "status": "SUCCEEDED",
                "target_volume": -3.0,
                "first_tick": {"bid_price": 5517.0, "ask_price": 5518.0, "last_price": 5517.5},
                "orders": [
                    {
                        "order_id": "o1",
                        "direction": "OrderDirection.SELL",
                        "order_type": "OrderType.LIMIT",
                        "price": 5516.0,
                        "avg_price": 5516.0,
                        "volume": 1.0,
                        "filled_volume": 1.0,
                        "status": "已成交",
                    }
                ],
            }
        },
    }
    before = {"total_asset": 1000.0, "positions": [_short("TA701", 2.0)], "source": "real"}
    row = build_symbol_reconciliation(result, before)["symbols"][0]

    assert row["tca"]["n_orders"] == 1
    assert row["tca"]["n_trades"] == 0
    assert abs(row["tca"]["fill_ratio"] - 1.0) < 1e-9
    assert row["orders"][0]["filled_volume"] == 1.0
    assert row["orders"][0]["trades"] == []


def test_reconciliation_close_to_zero_target() -> None:
    """清仓：target≈0，卖出后 after≈0 视为到位。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [], "source": "real"},
        "symbol_results": {
            "m2609": {"target_volume": 0, "orders": [_order("SELL", 2.34)], "status": "SUCCEEDED"},
        },
    }
    before = {"total_asset": 1200.0, "positions": [_long("m2609", 2.34)], "source": "real"}

    row = build_symbol_reconciliation(result, before)["symbols"][0]

    assert row["before"] == 2.34
    assert row["after"] == 0.0
    assert row["filled"] == -2.34
    assert row["reached"] is True


def test_reconciliation_short_position_is_negative() -> None:
    """做空：空头持仓与卖出成交都记负号。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [_short("ag2612", 5.0)], "source": "real"},
        "symbol_results": {
            "ag2612": {"target_volume": -5.0, "orders": [_order("SELL", 5.0)], "status": "SUCCEEDED"},
        },
    }
    before = {"total_asset": 1000.0, "positions": [], "source": "real"}

    row = build_symbol_reconciliation(result, before)["symbols"][0]

    assert row["after"] == -5.0
    assert row["filled"] == -5.0
    assert abs(row["attained_ratio"] - 1.0) < 1e-9
    assert row["reached"] is True


def test_reconciliation_missing_before_marks_unavailable() -> None:
    """前仓不可用：before=None → 账户级 source_before 记 unavailable、逐只 before 记 0。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [_long("ag2612", 1.0)], "source": "real"},
        "symbol_results": {
            "ag2612": {"target_volume": 1.0, "orders": [_order("BUY", 1.0)], "status": "SUCCEEDED"},
        },
    }

    recon = build_symbol_reconciliation(result, None)

    assert recon["account"]["source_before"] == "unavailable"
    assert recon["account"]["equity_before"] is None
    assert recon["account"]["equity_after"] == 1000.0
    assert recon["account"]["source_after"] == "real"
    assert recon["symbols"][0]["before"] is None
    assert recon["symbols"][0]["moved"] is None
    assert recon["symbols"][0]["drift"] is None


@pytest.mark.parametrize("source", ["unavailable", "assumed", "error"])
@pytest.mark.parametrize("final_volume", [None, 0.0, 2.0])
def test_reconciliation_unavailable_snapshot_uses_only_confirmed_symbol_position(source, final_volume):
    result = {
        "account_assets": {"source": source, "positions": []},
        "symbol_results": {"A": {"status": "PARTIAL", "target_volume": 0, "final_volume": final_volume}},
    }
    row = build_symbol_reconciliation(result, {"source": "real", "positions": []})["symbols"][0]
    assert row["after"] == final_volume
    assert row["reached"] is (None if final_volume is None else final_volume == 0)
    if final_volume is None:
        assert row["attained_ratio"] is None
        assert row["moved"] is None
        assert row["drift"] is None


def test_reconciliation_latest_real_account_position_overrides_earlier_algorithm_position():
    result = {
        "account_assets": {"source": "real", "positions": [_long("A", 3)]},
        "symbol_results": {"A": {"status": "SUCCEEDED", "target_volume": 2, "final_volume": 2}},
    }
    row = build_symbol_reconciliation(result, None)["symbols"][0]
    assert row["after"] == 3
    assert row["final_volume"] == 2
    assert row["reached"] is False
    assert row["attained_ratio"] == 1.5


def test_reconciliation_preserves_custom_simulation_source_without_relabeling_it_real():
    assets = {"source": "dummy", "positions": [_long("A", 2)]}
    result = {"account_assets": assets, "symbol_results": {"A": {"target_volume": 2}}}
    recon = build_symbol_reconciliation(result, assets)
    assert recon["account"]["source_before"] == "dummy"
    assert recon["account"]["source_after"] == "dummy"
    assert recon["symbols"][0]["after"] == 2
    assert recon["symbols"][0]["reached"] is True
    assert recon["symbols"][0]["drift"] == 0


def test_reconciliation_empty_symbol_results() -> None:
    """无逐只结果时仍返回账户块、symbols 为空。"""
    result = {"account_assets": {"total_asset": 1000.0, "positions": [], "source": "assumed"}}

    recon = build_symbol_reconciliation(result, None)

    assert recon["symbols"] == []
    assert recon["account"]["source_after"] == "assumed"


def test_reconciliation_none_target_leaves_ratio_unset() -> None:
    """NOOP 品种 target_volume 缺失：attained_ratio 与 reached 均为 None。"""
    result = {
        "account_assets": {"total_asset": 1000.0, "positions": [_long("ag2612", 1.0)], "source": "real"},
        "symbol_results": {
            "ag2612": {"target_volume": None, "orders": [], "status": "NOOP"},
        },
    }

    row = build_symbol_reconciliation(result, {"total_asset": 1000.0, "positions": [], "source": "real"})["symbols"][0]

    assert row["target"] is None
    assert row["attained_ratio"] is None
    assert row["reached"] is None


def test_reconciliation_preserves_target_sizing_evidence() -> None:
    """逐只对账必须原样保留执行器当时生成的数量换算证据."""
    sizing = {
        "symbol": "m2701",
        "status": "SIZED",
        "reason_code": "COMMON.SIZING.BELOW_MIN_QUANTITY",
        "account_weight": -0.18,
        "equity": 99_973.5875,
        "reference_price": 3_336.0,
        "unit_multiplier": 10.0,
        "unit_notional": 33_360.0,
        "target_notional": 17_995.24575,
        "raw_quantity": -0.539425832,
        "target_quantity": 0.0,
        "quantity_step": 1.0,
    }
    result = {
        "account_assets": {"total_asset": 99_973.5875, "positions": [], "source": "real"},
        "symbol_results": {
            "m2701": {
                "target_volume": 0.0,
                "orders": [],
                "status": "NOOP",
                "sizing": sizing,
            },
        },
    }

    row = build_symbol_reconciliation(result, result["account_assets"])["symbols"][0]

    assert row["sizing"] == sizing


def test_account_assets_default_source_is_real() -> None:
    """``UnifiedAccountAssets`` 默认来源标为 real。"""
    assets = UnifiedAccountAssets(available_cash=1.0, total_asset=1.0, market_value=0.0, positions=[])
    assert assets.source == "real"


def test_worker_before_capture_variants() -> None:
    """worker 路径：无该方法或抛错返回 ``None``，正常读取返回带 ``source`` 的快照。"""
    assert capture_before_worker(object()) is None
    assert capture_before_worker(_RaisingExecutor()) is None

    dump = capture_before_worker(_AssetsExecutor())
    assert dump is not None
    assert dump["source"] == "real"
    assert dump["total_asset"] == 1.0


def test_worker_before_capture_propagates_session_recovery() -> None:
    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        capture_before_worker(_SessionRecoveryExecutor())
