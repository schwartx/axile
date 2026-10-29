"""统一订单的成交关联、剩余数量及零数量保护。"""

from datetime import datetime

import pytest

from axile.executor.models.unified_order import (
    TradeRecord,
    UnifiedOrder,
)


class TestTradeRecord:
    """测试成交记录."""

    def test_create_trade_record(self) -> None:
        """测试创建成交记录."""
        trade = TradeRecord.create(
            trade_id="T001",
            symbol="rb2610",
            order_id="ORDER-1",
            trade_volume=0.1,
            trade_price=30000.0,
        )

        assert trade.trade_id == "T001"
        assert trade.symbol == "rb2610"
        assert trade.order_id == "ORDER-1"
        assert trade.trade_volume == 0.1
        assert trade.trade_price == 30000.0
        assert trade.trade_value == 3000.0  # 0.1 * 30000.0
        assert trade.trade_time is not None


class TestUnifiedOrderMethods:
    """测试统一订单方法."""

    def test_remaining_volume(self) -> None:
        """测试获取剩余未成交数量."""
        order = UnifiedOrder.create("O11", "rb2610", "BUY", "LIMIT", 1.0, 30000, filled_volume=0.3)

        assert order.remaining_volume == 0.7

        # 完全成交
        order_filled = UnifiedOrder.create("O12", "rb2610", "BUY", "LIMIT", 0.5, 30000, filled_volume=0.5)
        assert order_filled.remaining_volume == 0.0

        # 超成交（保护）
        order_overfilled = UnifiedOrder.create("O13", "rb2610", "BUY", "LIMIT", 0.5, 30000, filled_volume=0.6)
        assert order_overfilled.remaining_volume == 0.0

    def test_filled_ratio(self) -> None:
        """测试获取成交比例."""
        # 部分成交
        order = UnifiedOrder.create("O14", "rb2610", "BUY", "LIMIT", 1.0, 30000, filled_volume=0.3)
        assert order.filled_ratio == 0.3

        # 完全成交
        order_filled = UnifiedOrder.create("O15", "rb2610", "BUY", "LIMIT", 0.5, 30000, filled_volume=0.5)
        assert order_filled.filled_ratio == 1.0

        # 零成交
        order_zero = UnifiedOrder.create("O16", "rb2610", "BUY", "LIMIT", 1.0, 30000)
        assert order_zero.filled_ratio == 0.0

        # 零委托量（保护）
        order_zero_volume = UnifiedOrder.create("O17", "rb2610", "BUY", "LIMIT", 0.0, 30000)
        assert order_zero_volume.filled_ratio == 0.0


@pytest.mark.parametrize("construct", [False, True])
@pytest.mark.parametrize("updated", ["2026-10-15T18:38:33.939061", "2026-10-15T10:38:33.939061Z"])
def test_order_update_time_survives_construction_and_json_roundtrip(construct, updated):
    payload = {
        "order_id": "O-time",
        "symbol": "rb2610",
        "direction": "BUY",
        "order_type": "LIMIT",
        "volume": 1.0,
        "price": 1.0,
        "status": "已撤销",
        "update_time": updated,
        "extra": {},
    }
    order = UnifiedOrder.model_construct(**payload) if construct else UnifiedOrder.model_validate(payload)
    for _ in range(2):
        assert order.update_time == updated
        assert order.update_timestamp == datetime.fromisoformat(updated).timestamp()
        dumped = order.model_dump(mode="json")
        assert "update_timestamp" not in dumped
        assert dumped["extra"] == {}
        order = UnifiedOrder.model_validate_json(order.model_dump_json())


def test_order_legacy_timestamp_and_explicit_time_are_preserved():
    order = UnifiedOrder.create("O-time", "rb2610", "BUY", "LIMIT", 1, update_time="2026-10-15T18:38:33")
    assert UnifiedOrder.model_validate(order.model_dump()).update_time == order.update_time
    payload = order.model_dump()
    payload.pop("update_time")
    payload["extra"] = {}
    payload["update_timestamp"] = 1.0
    restored = UnifiedOrder.model_validate(payload)
    assert restored.update_time == datetime.fromtimestamp(1).isoformat()
    assert UnifiedOrder.model_validate(restored.model_dump()).update_timestamp == 1.0
    payload["extra"] = {"_update_time_str": "2026-10-15T18:38:33"}
    assert UnifiedOrder.model_validate(payload).update_time == "2026-10-15T18:38:33"
    payload["update_time"] = "2026-10-16T18:38:33"
    assert UnifiedOrder.model_validate(payload).update_time == "2026-10-16T18:38:33"


class TestUnifiedOrderTrades:
    """测试订单与成交职责分离."""

    def test_trade_record_keeps_order_link_at_top_level(self) -> None:
        """成交模型应独立携带订单关联键。"""
        trade = TradeRecord.create(
            trade_id="T003",
            symbol="ag2612",
            order_id="ORDER-ag2612-1",
            trade_volume=0.3,
            trade_price=2000.0,
        )

        assert trade.symbol == "ag2612"
        assert trade.order_id == "ORDER-ag2612-1"


class TestUnifiedOrderEdgeCases:
    """测试统一订单边界情况."""

    def test_zero_volume_order(self) -> None:
        """测试零委托量订单."""
        order = UnifiedOrder.create(
            "E001",
            "rb2610",
            "BUY",
            "LIMIT",
            0.0,
            30000.0,
        )

        # 虽然委托量为0，但应该能正常创建
        assert order.volume == 0.0
        assert order.remaining_volume == 0.0
        # 保护：避免除以零
        assert order.filled_ratio == 0.0
