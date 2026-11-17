"""验证期货平仓计划拒绝含糊快照并保留交易所差异。"""

import pytest

from axile.executor.futures_order_intent import is_close_intent, plan_futures_close_orders
from axile.executor.models.unified_order import OrderDirection


def _assets(symbol, side, today, yesterday):
    from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets

    prefix = side.lower()
    return UnifiedAccountAssets(
        available_cash=0,
        total_asset=0,
        market_value=0,
        positions=[
            Position(
                symbol=symbol,
                direction=PositionDirection.LONG if side == "LONG" else PositionDirection.SHORT,
                volume=today + yesterday,
                available_volume=today + yesterday,
                market_value=0,
                extra={f"{prefix}_td": today, f"{prefix}_yd": yesterday},
            )
        ],
    )


@pytest.mark.parametrize("exchange", ["SHFE", "INE"])
@pytest.mark.parametrize(
    "today,yesterday,volume,expected",
    [
        (3, 0, 2, [(2, {"offset_flag": "close_today"})]),
        (0, 3, 2, [(2, {"offset_flag": "close_yesterday"})]),
        (3, 2, 4, [(2, {"offset_flag": "close_yesterday"}), (2, {"offset_flag": "close_today"})]),
    ],
)
def test_close_plan_uses_remaining_today_and_yesterday(exchange, today, yesterday, volume, expected):
    assets = _assets("rb2610", "LONG", today, yesterday)
    assert plan_futures_close_orders("rb2610", OrderDirection.SELL, volume, assets, exchange) == expected


@pytest.mark.parametrize("exchange", ["DCE", "CZCE", "CFFEX", "GFEX"])
def test_other_exchanges_keep_generic_close(exchange):
    assets = _assets("symbol", "LONG", 2, 1)
    assert plan_futures_close_orders("symbol", OrderDirection.SELL, 3, assets, exchange) == [
        (3, {"offset_flag": "close"})
    ]


@pytest.mark.parametrize("problem", ["missing", "inconsistent", "frozen", "insufficient"])
def test_ambiguous_position_rejected_before_submission(problem):
    assets = _assets("rb2610", "LONG", 2, 1)
    position = assets.positions[0]
    if problem == "missing":
        position.extra.clear()
    elif problem == "inconsistent":
        position.extra["long_td"] = 5
    elif problem == "frozen":
        position.available_volume = 1
    with pytest.raises(ValueError):
        plan_futures_close_orders("rb2610", OrderDirection.SELL, 4 if problem == "insufficient" else 3, assets, "SHFE")


def test_invalid_position_side_rejected():
    with pytest.raises(ValueError, match="position_side"):
        is_close_intent(OrderDirection.BUY, "INVALID")


def test_execution_session_forwards_close_plan():
    from unittest.mock import Mock

    from axile.executor.execution_session import ExecutionSession

    owner = Mock()
    assets = _assets("rb2610", "LONG", 2, 1)
    owner.plan_close_orders.side_effect = lambda symbol, direction, volume, snapshot: plan_futures_close_orders(
        symbol, direction, volume, snapshot, "SHFE"
    )
    session = ExecutionSession(owner=owner, symbol="rb2610")
    assert session.plan_close_orders(OrderDirection.SELL, 3, assets) == [
        (1, {"offset_flag": "close_yesterday"}),
        (2, {"offset_flag": "close_today"}),
    ]
    owner.plan_close_orders.assert_called_once_with("rb2610", OrderDirection.SELL, 3, assets)


@pytest.mark.parametrize(
    "volume,offset,protect,error",
    [
        (2, "close", False, None),
        (3, "close", False, "可用"),
        (2, "close", True, "出现今仓"),
        (2, "open", False, "冻结"),
    ],
)
def test_frozen_close_checks_available_lots_and_preserves_today_protection(volume, offset, protect, error):
    from axile.executor.futures_order_intent import validate_futures_order

    assets = _assets("IF2612", "LONG", 6, 0)
    assets.positions[0].available_volume = 2
    if error:
        with pytest.raises(ValueError, match=error):
            validate_futures_order("IF2612", OrderDirection.SELL, volume, assets, offset=offset, protect_today=protect)
    else:
        validate_futures_order("IF2612", OrderDirection.SELL, volume, assets, offset=offset, protect_today=protect)


@pytest.mark.parametrize("available", [-1, 7, float("nan")])
def test_clear_still_rejects_invalid_available_position(available):
    from axile.executor.futures_order_intent import validate_futures_order

    assets = _assets("IF2612", "LONG", 6, 0)
    assets.positions[0].available_volume = available
    with pytest.raises(ValueError, match="可用持仓数量无效"):
        validate_futures_order("IF2612", OrderDirection.SELL, 1, assets, offset="close", protect_today=False)
