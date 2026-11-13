"""将 CTP 银期转账流水转换为账户入金与出金。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from axile.executor.models.cash_flow import ChannelCashFlow


def transfer_to_cash_flow(row: object) -> ChannelCashFlow | None:
    """仅接收有效成功记录；未知方向或币种拒绝整个同步。"""
    from openctp_ctp import thosttraderapi as td

    if getattr(row, "ErrorID") != 0:
        return None
    flag = getattr(row, "AvailabilityFlag")
    if flag == td.THOST_FTDC_AVAF_Invalid:
        return None
    if flag not in {td.THOST_FTDC_AVAF_Valid, td.THOST_FTDC_AVAF_Repeal}:
        raise ValueError("CTP 流水有效状态未知")
    direction = {"102001": "deposit", "202001": "deposit", "102002": "withdraw", "202002": "withdraw"}.get(
        str(getattr(row, "TradeCode"))
    )
    if direction is None:
        raise ValueError("CTP 流水方向未知，未写入流水")
    if getattr(row, "CurrencyID") != "CNY":
        raise ValueError("CTP 流水币种与账户币种不一致")
    day = str(getattr(row, "TradeDate"))
    time = datetime.strptime(f"{day} {getattr(row, 'TradeTime')}", "%Y%m%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    )
    serial = int(getattr(row, "PlateSerial"))
    if serial <= 0:
        raise ValueError("CTP 流水缺少可靠流水号")
    return ChannelCashFlow(
        direction=direction,
        amount=getattr(row, "TradeAmount"),
        occurred_at=time,
        external_id=f"ctp:{day}:{serial}",
        cancelled=flag == td.THOST_FTDC_AVAF_Repeal,
    )
