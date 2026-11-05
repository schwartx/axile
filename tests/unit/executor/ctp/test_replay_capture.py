"""回放旁路保留源事实且不改变 CTP 交易/回调行为。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.executor.ctp import replay
from axile.executor.replay import set_replay_observer
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker


@pytest.fixture
def broker(monkeypatch):
    driver = ScriptedBroker(monkeypatch)
    driver.start()
    driver.executor._replay_account_id = 17
    yield driver
    driver.executor.close()


def test_disabled_does_not_read_or_normalize_source(monkeypatch):
    owner = SimpleNamespace()
    build = []
    replay.capture(owner, "/order", lambda: build.append(True))
    assert build == []


def test_unknown_native_fields_do_not_inherit_business_defaults():
    data = replay.order(SimpleNamespace(Direction="bad", OrderStatus="bad", LimitPrice=float("nan"), Password="secret"))
    assert data["direction"] is None
    assert data["status"] == "unknown"
    assert data["price"] is None
    assert data["source_time_us"] is None
    assert data["field_quality"]["Direction"] == "invalid"
    assert data["field_quality"]["VolumeTotalOriginal"] == "missing"
    assert data["attributes"]["native_direction"] == "bad"
    assert data["attributes"]["native_status"] == "bad"
    assert "secret" not in repr(data)


@pytest.mark.parametrize(
    "flag", [td.THOST_FTDC_OF_ForceClose, td.THOST_FTDC_OF_ForceOff, td.THOST_FTDC_OF_LocalForceClose]
)
def test_force_close_keeps_native_subtype_and_common_close_semantics(broker, flag):
    row = SimpleNamespace(**(vars(broker.trade) | {"Direction": td.THOST_FTDC_D_Sell, "OffsetFlag": flag}))
    data = replay.trade(broker.executor, row, source="ctp.trade_return")
    assert data["direction"] == "SELL" and data["offset"] == "CLOSE"
    assert data["field_quality"]["OffsetFlag"] == "known"
    assert data["attributes"] == {"native_direction": td.THOST_FTDC_D_Sell, "native_offset_flag": flag}


def test_unmapped_enums_keep_exact_scalars_without_guessing_normalized_values():
    data = replay.order({"Direction": 0, "OrderStatus": "unmapped", "CombOffsetFlag": "01", "Password": "not-recorded"})
    assert data["direction"] == "BUY" and data["status"] == "unknown" and data["offset"] is None
    assert data["attributes"] == {
        "native_direction": 0,
        "native_status": "unmapped",
        "native_comb_offset_flag": "01",
    }
    assert data["field_quality"]["CombOffsetFlag"] == "invalid"
    assert "not-recorded" not in repr(data)


def test_unknown_position_direction_is_retained_without_claiming_a_complete_position_scope():
    data = replay.account_observation(
        [SimpleNamespace(Balance=100)],
        [SimpleNamespace(InstrumentID="ag2612", PosiDirection="new-code", Position=4)],
        source="ctp.account_query",
    )
    row = data["positions"][0]
    assert row["position_side"] is None and row["field_quality"]["PosiDirection"] == "invalid"
    assert row["attributes"]["native_position_direction"] == "new-code"
    assert data["scope_complete"] is False


def test_duplicate_trade_observations_do_not_duplicate_business_callbacks(broker):
    executor = broker.executor
    observations, delivered = [], []
    set_replay_observer(executor, lambda topic, data: observations.append((topic, deepcopy(data))))
    executor.register_trade_callback(delivered.append)
    executor._on_trade(broker.trade)
    executor._on_trade(broker.trade)
    raw = [data for topic, data in observations if topic == "/trade" and data["source"] == "ctp.trade_return"]
    assert len(raw) == 2
    assert {data["trade_key"] for data in raw} == {"ctp:17:20260909:SHFE:ag2612:T1"}
    assert len(delivered) == 1


def test_late_mapping_is_appended_and_does_not_rewrite_first_observation(broker):
    executor = broker.executor
    observations, delivered = [], []
    set_replay_observer(executor, lambda topic, data: observations.append((topic, deepcopy(data))))
    executor.register_trade_callback(delivered.append)
    row = SimpleNamespace(**(vars(broker.trade) | {"TradeID": "late", "OrderSysID": "late-order"}))
    executor._on_trade(row)
    first = deepcopy(observations[0][1])
    assert first["association"] == "pending" and first["order_id"] is None
    executor._on_order(SimpleNamespace(**(vars(broker.order) | {"OrderRef": "11", "OrderSysID": "late-order"})))
    mapping = [data for topic, data in observations if topic == "/trade" and data["source"] == "ctp.trade_mapping"]
    assert mapping[-1]["trade_key"] == first["trade_key"]
    assert mapping[-1]["order_id"] == "20260909:7:1:11"
    assert observations[0][1] == first
    assert len(delivered) == 1


def test_weak_identity_cannot_get_a_strong_trade_key(broker):
    executor = broker.executor
    del executor._replay_account_id
    data = replay.trade(executor, broker.trade, source="test")
    assert data["identity_quality"] == "weak"
    assert data["trade_key"] is None


def test_snapshot_depth_does_not_turn_missing_levels_into_zero_liquidity(broker):
    quote = SimpleNamespace(
        InstrumentID="ag2612",
        BidPrice1=9000,
        BidVolume1=3,
        AskPrice1=9001,
        AskVolume1=5,
        BidPrice2=1.7976931348623157e308,
        BidVolume2=0,
    )
    data = replay.quote(broker.executor, quote)
    assert data["bids"] == [{"price": 9000.0, "quantity": 3.0}]
    assert data["available_depth"] == {"bid": 1, "ask": 1}
    assert data["valid_until_us"] == data["received_time_us"] + int(
        broker.executor.account_config.quote_max_age_seconds * 1_000_000
    )
    assert data["book_valid"]
    assert data["field_quality"]["BidPrice2"] == "invalid"


def test_observer_failure_cannot_change_trade_delivery(broker):
    def broken(_topic, _data):
        raise RuntimeError("recording unavailable")

    delivered = []
    executor = broker.executor
    set_replay_observer(executor, broken)
    executor.register_trade_callback(delivered.append)
    executor._on_trade(broker.trade)
    assert len(delivered) == 1


def test_account_observation_preserves_freeze_without_assuming_business_alignment():
    data = replay.account_observation(
        [SimpleNamespace(Balance=100, Available=80)],
        [SimpleNamespace(InstrumentID="ag2612", PosiDirection="2", Position=4, TodayPosition=2, LongFrozen=1)],
        source="ctp.account_query",
    )
    assert data["scope_complete"]
    assert data["position_basis"] == {"status": "unverified", "proof": None}
    position = data["positions"][0]
    assert position["quantity"] == 4
    assert position["long_frozen"] == 1
    assert position["short_frozen"] is None


def test_position_today_history_rows_are_summed_without_missing_values_becoming_zero():
    data = replay.account_observation(
        [SimpleNamespace(Balance=100)],
        [
            SimpleNamespace(InstrumentID="ag2612", PosiDirection="2", Position=3, TodayPosition=3, LongFrozen=1),
            SimpleNamespace(InstrumentID="ag2612", PosiDirection="2", Position=4, TodayPosition=0),
        ],
        source="ctp.account_query",
    )
    assert len(data["positions"]) == 1
    position = data["positions"][0]
    assert position["quantity"] == 7
    assert position["today_quantity"] == 3
    assert position["long_frozen"] is None


def test_quote_night_resolution_keeps_raw_source_and_does_not_modify_owner(broker):
    executor = broker.executor
    executor._trading_day = "20260914"
    executor._instruments["ag2612"] = SimpleNamespace(ProductClass="1", ExchangeID="SHFE", ProductID="ag")
    row = SimpleNamespace(
        InstrumentID="ag2612",
        TradingDay="20260914",
        ActionDay="20260914",
        UpdateTime="21:01:02",
        UpdateMillisec=123,
        BidPrice1=9000,
        BidVolume1=1,
        AskPrice1=9001,
        AskVolume1=2,
    )
    previous = set(executor._quote_time_logged)
    data = replay.quote(executor, row)
    assert data["source_times"]["date"] == "20260914"
    assert data["source_times"]["resolved_time"].startswith("2026-09-11T21:01:02.123")
    assert data["source_time_quality"] == "normalized"
    assert executor._quote_time_logged == previous


def test_invalid_position_identity_cannot_clear_the_confirmed_account_scope():
    data = replay.account_observation(
        [SimpleNamespace(Balance=100)],
        [
            SimpleNamespace(Position=3, PosiDirection="2"),
            SimpleNamespace(Position=4, PosiDirection="2"),
            SimpleNamespace(InstrumentID="ag2612", PosiDirection="bad", Position=1),
            SimpleNamespace(InstrumentID="ag2612", PosiDirection="2"),
        ],
        source="ctp.account_query",
    )
    assert data["scope_complete"] is False
    assert len(data["positions"]) == 4
    unknown_symbols = [row for row in data["positions"] if row["symbol"] is None]
    assert [row["quantity"] for row in unknown_symbols] == [3, 4]


def test_normalization_failure_marks_connection_unavailable_without_raising():
    observed = []
    owner = SimpleNamespace()
    set_replay_observer(owner, lambda topic, data: observed.append((topic, data)))

    def broken():
        raise ValueError("invalid native frame")

    replay.capture(owner, "/order", broken)
    assert observed[-1][1]["available"] is False
    assert observed[-1][1]["reason"] == "normalization_failed"


def test_source_observations_fit_the_real_freeze_boundary(broker):
    from axile.channels.builtins import _ctp_plugin
    from axile.server.execution_replay.contracts import freeze_data

    capability = _ctp_plugin().descriptor.replay
    context = {"attributes": capability.attributes, "native_identity": capability.native_identity}
    observations = [
        ("/order", replay.order(broker.order)),
        ("/trade", replay.trade(broker.executor, broker.trade, source="ctp.trade_return")),
        ("/order_set", replay.order_set([broker.order], None, synchronized=True)),
        (
            "/account_observation",
            replay.account_observation([SimpleNamespace(Balance=100, Available=80)], [], source="ctp.account_query"),
        ),
        (
            "/command",
            replay.command(broker.executor, "ReqOrderInsert", broker.order, 7, phase="native_return", result=-3),
        ),
        ("/quote/ag2612", replay.quote(broker.executor, SimpleNamespace(InstrumentID="ag2612"))),
    ]
    for topic, data in observations:
        assert freeze_data(topic, data, context) == data
    command = next(data for topic, data in observations if topic == "/command")
    assert command["request_id"] == 7
    assert command["native_identity"]["request_id"] == "7"


def test_normal_account_query_keeps_the_same_requests(broker, monkeypatch):
    executor = broker.executor
    observed, requested = [], []
    set_replay_observer(executor, lambda topic, data: observed.append((topic, data)))

    def query(name, _request):
        requested.append(name)
        return [SimpleNamespace(Balance=100, Available=80)] if name == "ReqQryTradingAccount" else []

    monkeypatch.setattr(executor, "_query", query)
    monkeypatch.setattr(executor, "_position_valuation_quotes", lambda _rows: ({}, {}))
    result = executor.get_account_assets()
    assert requested == ["ReqQryTradingAccount", "ReqQryInvestorPosition"]
    assert result.available_cash == 80
    assert observed[-1][0] == "/account_observation"
    assert observed[-1][1]["position_basis"] == {"status": "unverified", "proof": None}


def test_order_query_carries_the_capture_prefix_without_more_requests(broker, monkeypatch):
    from axile.executor import replay as common

    executor = broker.executor
    token = {"sync_generation": 3, "query_basis": {"recording_id": "a", "start_seq": 2, "request_revision": 9}}
    observed, requests, scopes = [], [], []
    set_replay_observer(executor, lambda topic, data: observed.append((topic, data)))
    monkeypatch.setattr(common, "begin_query", lambda _owner, scope: scopes.append(scope) or token)
    monkeypatch.setattr(executor, "_query", lambda name, _req: requests.append(name) or [broker.order])
    assert executor._get_pending_orders_impl("ag2612") == []
    event = next(data for topic, data in observed if topic == "/order_set")
    assert scopes == [["ag2612"]]
    assert requests == ["ReqQryOrder"]
    assert event["query_basis"] == token["query_basis"]
    assert event["sync_generation"] == 3 and event["scope_complete"]


@pytest.mark.parametrize("exit_kind", ["invalidate", "close"])
def test_ctp_connection_exits_invalidate_the_recorded_generation(broker, tmp_path, exit_kind):
    from axile.channels.builtins import _ctp_plugin
    from axile.server.execution_replay.recorder import ReplayRecorder
    from tests.unit.server.test_execution_replay_recorder import FakeWriter

    writer = FakeWriter()
    context = _ctp_plugin().descriptor.replay.model_dump(mode="json")
    recorder = ReplayRecorder(17, tmp_path, context, writer_factory=lambda *_: writer)
    executor = broker.executor
    recorder.attach(executor)
    recorder.configure(True, 1)
    from tests.unit.server.test_execution_replay_native import wait_for

    wait_for(lambda: recorder.status()["state"] == "recording")
    before = recorder._sync_generation
    try:
        if exit_kind == "close":
            executor.close()
        else:
            executor._invalidate_connection("ambiguous native order identity")
        assert recorder._sync_generation == before + 1
        assert executor._replay_sync_generation == before + 1
    finally:
        recorder.close()
    observations = [message["data"] for message in writer.messages if message["topic"] == "/connection"]
    assert observations
    assert all(observations[-1][field] is False for field in ("connected", "ready", "synchronized", "available"))
    assert observations[-1]["sync_generation"] == before + 1
