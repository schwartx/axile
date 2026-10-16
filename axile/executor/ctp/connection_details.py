"""CTP 正式连接与诊断共用的阶段详情。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from axile.executor.ctp.validation_details import (
    account_details,
    instrument_details,
    login_details,
    order_details,
    position_details,
    trade_details,
)

if TYPE_CHECKING:
    from axile.executor.ctp.ctp_execute import CTPExecutor


def settlement_details(record: dict[str, object]) -> dict[str, object]:
    """将柜台确认记录转换为通用详情行；原始账号完整保留。"""
    fields = (
        ("确认日期", "confirm_date"),
        ("确认时间", "confirm_time"),
        ("结算编号", "settlement_id"),
        ("经纪公司代码", "broker_id"),
        ("投资者代码", "investor_id"),
        ("资金账号", "account_id"),
        ("币种", "currency_id"),
    )
    return {
        "title": "结算确认记录",
        "rows": [
            {"key": "确认方式", "value": "当日已有确认" if record.get("source") == "existing" else "本次连接自动确认"},
            *({"key": label, "value": record.get(key)} for label, key in fields),
        ],
    }


def step_details(executor: CTPExecutor, step: str) -> dict[str, object] | None:
    """只为有可读业务数据的阶段构造详情。"""
    if step == "settlement" and executor.settlement_confirmation is not None:
        return settlement_details(executor.settlement_confirmation)
    if step == "instruments":
        return instrument_details(
            executor._instruments,
            queried=executor._catalog_queried,
            scope="螺纹钢（SHFE · rb）" if executor._validation_catalog else "账户相关合约",
        )
    builders = {
        "trader_login": login_details,
        "query_orders": order_details,
        "query_trades": trade_details,
        "query_positions": position_details,
        "query_account": account_details,
    }
    if step in builders and step in executor.connection_records:
        return builders[step](executor.connection_records[step])
    return None
