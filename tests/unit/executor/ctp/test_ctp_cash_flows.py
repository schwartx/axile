"""银期流水转换只保留已确认的方向、金额、时间和流水身份。"""

from types import SimpleNamespace

import pytest

pytest.importorskip("openctp_ctp")

from axile.executor.ctp.cash_flows import transfer_to_cash_flow


@pytest.fixture
def row(monkeypatch):
    """无需柜台或 SDK 二进制，仍逐字段校验有效标志契约。"""
    import sys

    td = SimpleNamespace(THOST_FTDC_AVAF_Valid="1", THOST_FTDC_AVAF_Invalid="0", THOST_FTDC_AVAF_Repeal="2")
    monkeypatch.setitem(sys.modules, "openctp_ctp", SimpleNamespace(thosttraderapi=td))
    return SimpleNamespace(
        ErrorID=0,
        AvailabilityFlag="1",
        TradeCode="202001",
        CurrencyID="CNY",
        TradeDate="20261112",
        TradeTime="09:30:00",
        PlateSerial=12,
        TradeAmount=500,
        BankAccount="must-not-leak",
        Password="must-not-leak",
    )


def test_transfer_direction_time_identity_and_private_fields(row):
    flow = transfer_to_cash_flow(row)
    assert flow.direction == "deposit"
    assert flow.amount == 500
    assert flow.occurred_at.isoformat() == "2026-11-12T09:30:00+08:00"
    assert flow.external_id == "ctp:20261112:12"
    assert "must-not-leak" not in flow.model_dump_json()
    row.TradeCode = "102001"
    assert transfer_to_cash_flow(row).direction == "deposit"
    for code in ("102002", "202002"):
        row.TradeCode = code
        assert transfer_to_cash_flow(row).direction == "withdraw"
    row.AvailabilityFlag = "2"
    assert transfer_to_cash_flow(row).cancelled


def test_invalid_record_unknown_currency_and_direction(row):
    row.ErrorID = 1
    assert transfer_to_cash_flow(row) is None
    row.ErrorID = 0
    row.CurrencyID = "USD"
    with pytest.raises(ValueError, match="币种"):
        transfer_to_cash_flow(row)
    row.CurrencyID = "CNY"
    row.TradeCode = "unknown"
    with pytest.raises(ValueError, match="方向"):
        transfer_to_cash_flow(row)
