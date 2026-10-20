"""CTP 持仓汇总与分项的原生展示字段。"""

from axile.executor.position_attributes import native_position_attributes, native_value, sum_native_position_attributes

_ADDITIVE = {
    "today_volume": "TodayPosition",
    "previous_volume": "YdPosition",
    "position_cost": "PositionCost",
    "open_cost": "OpenCost",
    "position_profit": "PositionProfit",
    "close_profit": "CloseProfit",
    "close_profit_by_date": "CloseProfitByDate",
    "close_profit_by_trade": "CloseProfitByTrade",
    "margin": "UseMargin",
    "exchange_margin": "ExchangeMargin",
    "frozen_margin": "FrozenMargin",
    "frozen_cash": "FrozenCash",
    "frozen_commission": "FrozenCommission",
    "commission": "Commission",
    "long_frozen": "LongFrozen",
    "short_frozen": "ShortFrozen",
    "combination_volume": "CombPosition",
    "comb_long_frozen": "CombLongFrozen",
    "comb_short_frozen": "CombShortFrozen",
    "strike_frozen": "StrikeFrozen",
    "strike_frozen_amount": "StrikeFrozenAmount",
    "abandon_frozen": "AbandonFrozen",
    "open_volume": "OpenVolume",
    "close_volume": "CloseVolume",
    "open_amount": "OpenAmount",
    "close_amount": "CloseAmount",
    "tas_volume": "TasPosition",
    "tas_cost": "TasPositionCost",
}


def ctp_position_fields(row: object) -> dict[str, float | str]:
    """保留单条柜台持仓中的业务字段，不带账户凭据。"""
    if native_value(row, "combination_origin"):
        # 拆腿继承的组合金额并非单腿金额，不能冒充该腿的成本、盈亏或保证金。
        return native_position_attributes(row, {"volume": "Position", "today_volume": "TodayPosition"})
    return native_position_attributes(
        row,
        {
            **_ADDITIVE,
            "volume": "Position",
            "settlement_price": "SettlementPrice",
            "previous_settlement_price": "PreSettlementPrice",
            "margin_rate_money": "MarginRateByMoney",
            "margin_rate_volume": "MarginRateByVolume",
        },
        {
            "exchange_id": "ExchangeID",
            "trading_day": "TradingDay",
            "hedge_flag": "HedgeFlag",
            "position_date": "PositionDate",
        },
    )


def ctp_position_totals(rows: list[dict[str, float | str]]) -> dict[str, float | str]:
    """汇总同品种同方向的原生金额；非加法字段仅在分项一致时保留。"""
    result: dict[str, float | str] = dict(sum_native_position_attributes(rows, tuple(_ADDITIVE)))
    for key in (
        "settlement_price",
        "previous_settlement_price",
        "margin_rate_money",
        "margin_rate_volume",
        "exchange_id",
        "trading_day",
    ):
        values = [row.get(key) for row in rows]
        if values and values[0] is not None and all(value == values[0] for value in values):
            result[key] = values[0]
    return result
