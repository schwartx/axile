"""验证展示字段的业务含义、空值与柜台原始身份，不连接真实柜台。"""

from types import SimpleNamespace

from axile.executor.ctp.validation_details import account_details, order_details, position_details


def test_account_details_keep_all_currencies_and_missing_values():
    details = account_details(
        [
            SimpleNamespace(AccountID="full-account", CurrencyID="CNY", Balance=0, Available=0, Password="secret"),
            SimpleNamespace(AccountID="full-account", CurrencyID="USD", Balance=12.5, Available=float("nan")),
        ]
    )
    values = {row["key"]: row["value"] for row in details["rows"]}
    assert values["记录 1 · 资金账号"] == "full-account"
    assert values["记录 1 · 账户权益"] == 0
    assert values["记录 2 · 币种"] == "USD"
    assert values["记录 2 · 可用资金"] is None
    assert values["记录 1 · 当前保证金"] is None
    assert "secret" not in str(details)


def test_position_snapshot_does_not_mislabel_yesterday_settlement_quantity():
    table = position_details(
        [
            SimpleNamespace(
                InstrumentID="ag2612",
                PosiDirection="2",
                PositionDate="2",
                HedgeFlag="1",
                Position=4,
                TodayPosition=1,
                YdPosition=9,
            ),
            SimpleNamespace(
                InstrumentID="ag2612", PosiDirection="2", PositionDate="1", HedgeFlag="3", Position=2, TodayPosition=2
            ),
        ]
    )
    keys = [column["key"] for column in table["columns"]]
    assert table["rows"][0][keys.index("RemainingYesterday")] == 3
    assert table["rows"][1][keys.index("RemainingYesterday")] == 0
    assert table["rows"][0][keys.index("HedgeFlag")] == "投机"
    assert table["rows"][1][keys.index("HedgeFlag")] == "套保"
    assert table["summary"] == "2 条持仓记录"


def test_order_details_keep_identity_rejection_and_unknown_enums():
    table = order_details(
        [
            SimpleNamespace(
                OrderSysID="000123",
                OrderRef="0005",
                FrontID=0,
                SessionID=1,
                OrderStatus="a",
                OrderSubmitStatus="4",
                StatusMsg="拒绝原因",
                Direction="future-code",
                CombOffsetFlag="3",
                LimitPrice=1.7976931348623157e308,
                VolumeTraded=0,
            )
        ]
    )
    values = dict(zip((column["key"] for column in table["columns"]), table["rows"][0], strict=True))
    assert values["OrderSysID"] == "000123"
    assert values["OrderSubmitStatus"] == "报单被拒绝"
    assert values["Direction"] == "future-code"
    assert values["CombOffsetFlag"] == "平今"
    assert values["LimitPrice"] is None
    assert values["FrontID"] == 0
