"""将 CTP 查询快照整理为渠道无关的详情；仅输出显式声明的展示字段。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

_DIRECTION = {"0": "买入", "1": "卖出"}
_OFFSET = {"0": "开仓", "1": "平仓", "2": "强平", "3": "平今", "4": "平昨", "5": "强减", "6": "本地强平"}
_ORDER_STATUS = {
    "0": "全部成交",
    "1": "部分成交，仍在队列",
    "2": "部分成交，已离队",
    "3": "未成交，仍在队列",
    "4": "未成交，已离队",
    "5": "已撤单",
    "a": "未知",
    "b": "尚未触发",
    "c": "已触发",
}
_PRODUCT = {"1": "期货", "2": "期货期权", "3": "组合", "4": "即期", "5": "期转现", "6": "现货期权"}
_POSITION_DIRECTION = {"1": "净持仓", "2": "多头", "3": "空头"}
_HEDGE = {"1": "投机", "2": "套利", "3": "套保", "5": "做市商"}


def _value(row: object, field: str) -> object:
    """兼容回调副本与只读合约记录，并清洗柜台的无效浮点哨兵值。"""
    value = row.get(field) if isinstance(row, Mapping) else getattr(row, field, None)
    if isinstance(value, float) and (not math.isfinite(value) or abs(value) >= 1.7976931348623157e308):
        return None
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def _label(value: object, labels: dict[str, str]) -> object:
    """未知枚举保留柜台原值，避免误标为其他已知状态。"""
    return labels.get(str(value), value)


def _key_values(title: str, row: object, fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return {"title": title, "rows": [{"key": label, "value": _value(row, field)} for label, field in fields]}


def login_details(row: object) -> dict[str, object]:
    """保留实际登录响应，缺失值不以配置或本地时间补齐。"""
    return _key_values(
        "交易账户登录信息",
        row,
        (
            ("交易日", "TradingDay"),
            ("登录时间", "LoginTime"),
            ("经纪公司代码", "BrokerID"),
            ("用户代码", "UserID"),
            ("柜台名称", "SystemName"),
            ("前置编号", "FrontID"),
            ("会话编号", "SessionID"),
            ("最大报单引用", "MaxOrderRef"),
        ),
    )


def account_details(accounts: list[object]) -> dict[str, object]:
    """完整展示各币种资金记录，不把多条账户资金静默合并或丢弃。"""
    fields = (
        ("资金账号", "AccountID"),
        ("经纪公司代码", "BrokerID"),
        ("交易日", "TradingDay"),
        ("币种", "CurrencyID"),
        ("账户权益", "Balance"),
        ("可用资金", "Available"),
        ("当前保证金", "CurrMargin"),
        ("冻结保证金", "FrozenMargin"),
        ("冻结资金", "FrozenCash"),
        ("冻结手续费", "FrozenCommission"),
        ("手续费", "Commission"),
        ("平仓盈亏", "CloseProfit"),
        ("持仓盈亏", "PositionProfit"),
        ("当日入金", "Deposit"),
        ("当日出金", "Withdraw"),
        ("上次结算准备金", "PreBalance"),
    )
    rows = []
    for index, account in enumerate(accounts):
        details = _key_values("资金查询记录", account, fields)
        for row in details["rows"]:
            if len(accounts) > 1:
                row["key"] = f"记录 {index + 1} · {row['key']}"
            rows.append(row)
    return {"title": "资金查询记录", "rows": rows}


def _table(
    title: str,
    columns: tuple[tuple[str, str, str], ...],
    rows: Iterable[object],
    *,
    summary: str,
    translations: dict[str, dict[str, str]] | None = None,
    description: str | None = None,
) -> dict[str, object]:
    values = []
    for row in rows:
        cells = []
        for key, _label_text, _kind in columns:
            value = _value(row, key)
            if translations and key in translations:
                value = _label(value, translations[key])
            cells.append(value)
        values.append(cells)
    return {
        "kind": "table",
        "title": title,
        "summary": summary,
        "description": description,
        "columns": [
            {
                "key": key,
                "label": label,
                "type": kind,
                "filterable": key
                in {
                    "ExchangeID",
                    "ProductClass",
                    "Direction",
                    "CombOffsetFlag",
                    "OffsetFlag",
                    "OrderStatus",
                    "OrderSubmitStatus",
                    "PosiDirection",
                    "PositionDate",
                    "HedgeFlag",
                },
            }
            for key, label, kind in columns
        ],
        "rows": values,
    }


def instrument_details(instruments: Mapping[str, object], *, queried: bool) -> dict[str, object]:
    """展示当前交易日目录，无论目录来自缓存还是本次柜台查询。"""
    return _table(
        "合约目录",
        (
            ("InstrumentID", "合约代码", "text"),
            ("InstrumentName", "名称", "text"),
            ("ExchangeID", "交易所", "text"),
            ("ProductClass", "产品类型", "text"),
            ("VolumeMultiple", "合约乘数", "number"),
            ("PriceTick", "最小变动价位", "number"),
            ("ExpireDate", "到期日", "date"),
        ),
        instruments.values(),
        summary=f"{len(instruments)} 个合约",
        translations={"ProductClass": _PRODUCT},
        description=f"目录来源：{'本次柜台查询' if queried else '缓存'}",
    )


def order_details(orders: list[object]) -> dict[str, object]:
    """报单提交状态与订单状态分别展示，以保留拒绝提交的证据。"""
    return _table(
        "订单查询记录",
        (
            ("InstrumentID", "合约", "text"),
            ("ExchangeID", "交易所", "text"),
            ("Direction", "方向", "text"),
            ("CombOffsetFlag", "开平", "text"),
            ("LimitPrice", "委托价格", "number"),
            ("VolumeTotalOriginal", "委托手数", "number"),
            ("VolumeTraded", "成交手数", "number"),
            ("VolumeTotal", "剩余手数", "number"),
            ("OrderStatus", "订单状态", "text"),
            ("OrderSubmitStatus", "提交状态", "text"),
            ("StatusMsg", "状态说明", "text"),
            ("InsertDate", "报单日期", "date"),
            ("InsertTime", "报单时间", "time"),
            ("OrderSysID", "交易所订单编号", "text"),
            ("OrderRef", "报单引用", "text"),
            ("FrontID", "前置编号", "number"),
            ("SessionID", "会话编号", "number"),
        ),
        orders,
        summary=f"{len(orders)} 条订单",
        translations={
            "Direction": _DIRECTION,
            "CombOffsetFlag": _OFFSET,
            "OrderStatus": _ORDER_STATUS,
            "OrderSubmitStatus": {
                "0": "已经提交",
                "1": "撤单已提交",
                "2": "修改已提交",
                "3": "已经接受",
                "4": "报单被拒绝",
                "5": "撤单被拒绝",
                "6": "修改被拒绝",
            },
        },
    )


def trade_details(trades: list[object]) -> dict[str, object]:
    """展示完成订单关联校验的柜台成交快照。"""
    return _table(
        "成交查询记录",
        (
            ("TradeDate", "成交日期", "date"),
            ("TradeTime", "成交时间", "time"),
            ("InstrumentID", "合约", "text"),
            ("ExchangeID", "交易所", "text"),
            ("Direction", "方向", "text"),
            ("OffsetFlag", "开平", "text"),
            ("Price", "成交价格", "number"),
            ("Volume", "成交手数", "number"),
            ("TradeID", "成交编号", "text"),
            ("OrderSysID", "交易所订单编号", "text"),
        ),
        trades,
        summary=f"{len(trades)} 条成交",
        translations={"Direction": _DIRECTION, "OffsetFlag": _OFFSET},
    )


def position_details(positions: list[object]) -> dict[str, object]:
    """保留今昨、投保等原始分行维度，不把原始条数称作合约数。"""
    rows = []
    for position in positions:
        row = {
            field: _value(position, field)
            for field in (
                "InstrumentID",
                "ExchangeID",
                "PosiDirection",
                "PositionDate",
                "HedgeFlag",
                "Position",
                "TodayPosition",
                "LongFrozen",
                "ShortFrozen",
            )
        }
        volume, today = row["Position"], row["TodayPosition"]
        if (
            isinstance(volume, (int, float))
            and not isinstance(volume, bool)
            and isinstance(today, (int, float))
            and not isinstance(today, bool)
            and 0 <= today <= volume
        ):
            row["RemainingYesterday"] = volume - today
        else:
            row["RemainingYesterday"] = None
        rows.append(row)
    return _table(
        "持仓查询记录",
        (
            ("InstrumentID", "合约", "text"),
            ("ExchangeID", "交易所", "text"),
            ("PosiDirection", "方向", "text"),
            ("PositionDate", "持仓日期", "text"),
            ("HedgeFlag", "投保标志", "text"),
            ("Position", "持仓手数", "number"),
            ("TodayPosition", "今仓", "number"),
            ("RemainingYesterday", "剩余昨仓", "number"),
            ("LongFrozen", "多头冻结", "number"),
            ("ShortFrozen", "空头冻结", "number"),
        ),
        rows,
        summary=f"{len(positions)} 条持仓记录",
        translations={
            "PosiDirection": _POSITION_DIRECTION,
            "PositionDate": {"1": "今日持仓", "2": "历史持仓"},
            "HedgeFlag": _HEDGE,
        },
        description="原始查询记录，按柜台的今昨仓、投保等维度分行，尚未汇总或估值。剩余昨仓按持仓手数减今仓计算。",
    )
