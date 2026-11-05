"""GM 原生观察规范化：真实缺失、身份和稀疏盘口不能被默认值覆盖。"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from axile.executor import replay as recording
from axile.executor.gm import replay
from axile.server.execution_replay.contracts import freeze_data


def _context():
    from axile.channels import get_channel

    return get_channel("gm").descriptor.replay.model_dump(mode="json")


@pytest.mark.parametrize("factory", [dict, SimpleNamespace])
def test_quote_keeps_only_observed_levels_and_does_not_invent_time_coverage(factory):
    value = factory(
        symbol="SHSE.600000",
        created_at=datetime.fromisoformat("2026-10-30T10:00:00+08:00"),
        price=12.3,
        last_volume=100,
        quotes=[
            factory(bid_p=12.2, bid_v=300, ask_p=12.4, ask_v=200),
            factory(bid_p=0, bid_v=0, ask_p=float("nan"), ask_v=100),
        ],
    )
    data = replay.quote(value)
    assert data["symbol"] == "600000.SH"
    assert data["bids"] == [{"price": 12.2, "quantity": 300}]
    assert data["asks"] == [{"price": 12.4, "quantity": 200}]
    assert data["available_depth"] == {"bid": 1, "ask": 1}
    assert data["book_valid"] is True
    assert data["valid_until_us"] is None
    assert data["volume_kind"] == "last"
    assert freeze_data("/quote/600000.SH", data, _context()) == data


def test_quote_missing_timestamp_and_crossed_book_remain_unconfirmed():
    row = {
        "symbol": "SHSE.600000",
        "created_at": datetime(2026, 10, 30, 10),
        "quotes": [{"bid_p": 12.5, "bid_v": 100, "ask_p": 12.4, "ask_v": 200}],
    }
    data = replay.quote(row, source="gm.current")
    assert data["source_time_us"] is None
    assert data["field_quality"]["created_at"] == "invalid"
    assert data["book_valid"] is False
    assert data["delivery_method"] == "query"


def test_unknown_order_fields_and_actual_zero_are_not_defaulted():
    data = replay.order(
        {
            "symbol": "SZSE.000001",
            "cl_ord_id": "order-1",
            "side": 77,
            "status": 999,
            "volume": 100,
            "filled_volume": 0,
            "price": float("inf"),
        }
    )
    assert data["symbol"] == "000001.SZ"
    assert data["status"] == "unknown" and data["confirmed_book"] is None
    assert data["direction"] is None and data["price"] is None
    assert data["filled_quantity"] == 0 and data["remaining_quantity"] == 100
    assert data["field_quality"]["side"] == "invalid"
    assert freeze_data("/order", data, _context()) == data


def test_trade_without_exec_id_has_no_synthetic_strong_key():
    owner = SimpleNamespace(_replay_account_id=7)
    row = {"symbol": "SHSE.600000", "cl_ord_id": "order-1", "price": 12.3, "volume": 100, "exec_type": 15}
    weak = replay.trade(owner, row)
    assert weak["identity_quality"] == "weak" and weak["trade_key"] is None
    strong = replay.trade(owner, {**row, "exec_id": "trade-1"})
    assert strong["trade_key"] == "gm:7:600000.SH:trade-1"
    assert strong["association"] == "confirmed"
    assert freeze_data("/trade", strong, _context()) == strong


def test_account_observation_keeps_available_frozen_and_empty_query_distinct():
    position = {
        "symbol": "SHSE.600000",
        "side": 1,
        "volume": 1000,
        "volume_today": 200,
        "available_now": 600,
        "available": 800,
        "order_frozen": 400,
    }
    data = replay.account_observation({"nav": 50000, "available": 30000}, [position])
    row = data["positions"][0]
    assert row["quantity"] == 1000 and row["today_quantity"] == 200
    assert row["available_quantity"] == 600 and row["frozen_quantity"] == 400
    assert row["position_basis"]["status"] == "unverified"
    assert freeze_data("/account_observation", data, _context()) == data
    assert replay.account_observation(None, [position])["scope_complete"] is True
    assert replay.account_observation({"nav": 50000}, None)["scope_complete"] is False
    assert replay.account_observation(None, [])["scope_complete"] is True


def test_actual_command_parameters_do_not_copy_credentials_or_reject_text():
    data = replay.command(
        "order_cancel",
        {
            "account_id": "private-account",
            "token": "private-token",
            "wait_cancel_orders": [{"cl_ord_id": "order-1", "account_id": "private-account"}],
        },
        phase="native_request",
        command_id="command-1",
    )
    encoded = json.dumps(data, allow_nan=False)
    assert "private-account" not in encoded and "private-token" not in encoded
    assert data["actual_parameters"] == {"wait_cancel_orders": [{"cl_ord_id": "order-1"}]}
    assert data["order_id"] == "order-1"
    assert freeze_data("/command", data, _context()) == data


def test_capture_explicit_off_receipt_never_borrows_new_segment():
    received = []
    owner = SimpleNamespace(_replay_observer=lambda *args: received.append(args), _replay_sync_generation=4)
    replay.capture(owner, "/order", lambda: replay.order({}), receipt=None)
    assert received == []
    old = recording.capture_receipt(owner)
    owner._replay_observer = lambda *_args: pytest.fail("old callback reached new segment")
    replay.capture(owner, "/order", lambda: replay.order({}), receipt=old)
    assert received[0][1]["sync_generation"] == 4


def test_normalization_failure_remains_on_original_observer():
    received = []
    owner = SimpleNamespace(_replay_observer=lambda *args: received.append(args), _replay_sync_generation=4)

    def fail():
        raise ValueError("sensitive native error text")

    replay.capture(owner, "/order", fail)
    assert received[0][0] == "/connection"
    assert received[0][1]["reason"] == "normalization_failed"
    assert "sensitive" not in json.dumps(received)


@pytest.mark.parametrize(
    "exec_type,is_fill,is_report",
    [
        (15, True, False),
        (4, False, True),
        (5, False, True),
        (8, False, True),
        (18, False, True),
        (19, False, True),
        (16, False, False),
        (17, False, False),
        (0, False, False),
        (None, False, False),
        (999, False, False),
    ],
)
def test_status_reports_do_not_enter_trade_accounting_but_unknown_changes_remain_uncertain(
    exec_type, is_fill, is_report
):
    data = replay.trade(
        SimpleNamespace(_replay_account_id=7),
        {
            "symbol": "SHSE.600000",
            "exec_id": "report-one",
            "exec_type": exec_type,
            "price": 12.3,
            "volume": 100,
        },
    )
    assert data["quantity"] == (100 if is_fill else None)
    assert data["price"] == (12.3 if is_fill else None)
    assert data["attributes"]["price"] == 12.3 and data["attributes"]["volume"] == 100
    received = []
    owner = SimpleNamespace(_replay_observer=lambda *args: received.append(args), _replay_account_id=7)
    replay.capture(owner, "/trade", lambda: data)
    topic, observed = received[0]
    assert topic == ("/command" if is_report else "/trade")
    if is_report:
        assert observed["operation"] == "execution_report" and observed["phase"] == "native_report"
        assert observed["command_id"] is None
    elif not is_fill:
        assert observed["execution_type"] == "unknown"
        assert observed["trade_key"] is None and observed["identity_quality"] == "weak"
    assert freeze_data(topic, observed, _context()) == observed


@pytest.mark.parametrize("factory", [dict, SimpleNamespace])
def test_canonical_alias_quality_does_not_inherit_missing_native_field(factory):
    owner = SimpleNamespace(_replay_account_id=7)
    native = factory(symbol="SHSE.600000", cl_ord_id="order-one", side=1, status=1, order_type=1)
    for data in (replay.order(native), replay.trade(owner, native)):
        assert data["order_id"] == "order-one"
        assert data["field_quality"]["order_id"] == "known"
        assert data["field_quality"]["native_order_id"] == "missing"
        assert data["native_identity"] == {"cl_ord_id": "order-one"}
    position = replay.account_observation(None, [factory(symbol="SHSE.600000", side=1, volume=100)])["positions"][0]
    assert position["position_side"] == "LONG"
    assert position["field_quality"]["position_side"] == "known"
    assert position["field_quality"]["native_position_side"] == "missing"
    quote = replay.quote(factory(symbol="SHSE.600000", last_volume=0))
    assert quote["volume"] == 0 and quote["field_quality"]["volume"] == "known"
    assert quote["field_quality"]["native_volume"] == "missing"


@pytest.mark.parametrize("phase", ["native_request", "native_return"])
def test_cancel_and_submit_response_identity_updates_canonical_quality(phase):
    if phase == "native_request":
        data = replay.command(
            "order_cancel", {"wait_cancel_orders": [{"cl_ord_id": "one"}]}, phase=phase, command_id="C"
        )
    else:
        data = replay.command("order_volume", {}, phase=phase, command_id="C", result=[{"cl_ord_id": "one"}])
    assert data["order_id"] == "one"
    assert data["field_quality"]["order_id"] == "known"
    assert data["field_quality"]["native_order_id"] == "missing"


@pytest.mark.parametrize("order_type,stop_price,confirmed", [(1, 0, True), (1, 12.5, False), (2, 0, False)])
def test_only_plain_confirmed_limit_order_enters_order_book(order_type, stop_price, confirmed):
    data = replay.order({"status": 1, "order_type": order_type, "stop_price": stop_price})
    assert data["confirmed_book"] is confirmed


@pytest.mark.parametrize("bad_index", [0, 1])
def test_quote_only_claims_contiguous_observed_prefix(bad_index):
    quotes = [
        {"bid_p": 12.2, "bid_v": 100, "ask_p": 12.4, "ask_v": 100},
        {"bid_p": 12.1, "bid_v": 100, "ask_p": 12.5, "ask_v": 100},
        {"bid_p": 12.0, "bid_v": 100, "ask_p": 12.6, "ask_v": 100},
    ]
    quotes[bad_index]["bid_p"] = 0
    data = replay.quote({"symbol": "SHSE.600000", "quotes": quotes, "created_at": "2026-10-30T10:00:00+08:00"})
    assert len(data["bids"]) == bad_index
    assert len(data["asks"]) == 3
    assert data["book_valid"] is (bad_index > 0)


@pytest.mark.parametrize("stop_price", [float("nan"), float("inf"), "bad"])
def test_invalid_trigger_price_cannot_confirm_plain_limit_book(stop_price):
    data = replay.order({"status": 1, "order_type": 1, "stop_price": stop_price})
    assert data["confirmed_book"] is False
    assert data["field_quality"]["stop_price"] == "invalid"
