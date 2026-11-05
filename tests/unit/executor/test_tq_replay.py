"""天勤真实执行器入口与隔离 SDK 夹具的回放契约，不连接账户。"""

import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from axile.channels import get_channel
from axile.executor import replay as recording
from axile.executor.models.unified_input import TQAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.tq import TQExecutor, replay
from axile.executor.tq.runtime import snapshot_entity
from axile.executor.tq.tq_execute import TQTradingTimeCheck, TQTradingTimeStatus
from axile.server.execution_replay.contracts import freeze_data
from axile.server.execution_replay.recorder import ReplayRecorder
from axile.server.execution_replay.service import replay_manifest, replay_window
from tests.unit.executor.tq.test_executor import FakeApi


@pytest.fixture
def executor(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    owner = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    try:
        yield owner, api
    finally:
        owner.close()


def _observe(owner):
    observations = []
    owner._replay_account_id = 7
    owner._replay_sync_generation = 1
    recording.set_replay_observer(owner, lambda topic, data: observations.append((topic, deepcopy(data))))
    return observations


def test_native_unknowns_and_secrets_are_not_replaced_by_business_defaults():
    owner = SimpleNamespace(account_config=SimpleNamespace(account_mode="sim"))
    data = replay.order(owner, {"direction": "bad", "status": "new", "limit_price": float("nan"), "password": "secret"})
    assert data["direction"] is None and data["price"] is None and data["status"] == "unknown"
    assert data["source_time_us"] is None and data["confirmed_book"] is None
    assert data["attributes"] == {"account_mode": "sim", "native_status": "new"}
    assert "secret" not in repr(data)
    assert replay.order(owner, {"status": "FINISHED", "volume_left": 2})["status"] == "unknown"


def test_actual_quote_entry_preserves_only_observed_depth(executor):
    owner, api = executor
    observations = _observe(owner)
    api.quote.update(bid_price2=float("nan"), bid_volume2=0, ask_price2=0, ask_volume2=0)
    owner.get_market_data(["rb2610"])
    quote = next(data for topic, data in observations if topic == "/quote/rb2610")
    assert quote["bids"] == [{"price": 3199, "quantity": 2}]
    assert quote["available_depth"] == {"bid": 1, "ask": 1}
    assert quote["intermediate_states"] == "observed_only"
    assert not any(topic.startswith("/market_trade/") for topic, _ in observations)
    assert api.quote_calls == 1


def test_actual_query_and_runtime_trade_have_same_stable_identity(executor):
    owner, api = executor
    observations = _observe(owner)
    owner._require_runtime().call(lambda _api: owner._require_runtime()._emit("trade", api.trade))
    owner.query_recent_trades()
    trades = [data for topic, data in observations if topic == "/trade"]
    assert len(trades) == 2
    assert trades[0]["trade_key"] == trades[1]["trade_key"]
    assert trades[0]["identity_quality"] == "stable"
    assert replay.trade(SimpleNamespace(), api.trade)["trade_key"] is None


def test_exchange_trade_id_enrichment_does_not_double_count_native_checkpoint(tmp_path):
    from tqsdk.diff import _merge_diff
    from tqsdk.entity import Entity
    from tqsdk.objs import Trade

    root = Entity()
    root._instance_entity([])
    prototype = {"trades": {"@": Trade(None)}}
    row = {
        "trade_id": "sdk-trade-1",
        "order_id": "o1",
        "exchange_id": "SHFE",
        "instrument_id": "rb2610",
        "direction": "BUY",
        "offset": "OPEN",
        "price": 3200.0,
        "volume": 1,
        "trade_date_time": 1_700_000_000_000_000_000,
    }
    _merge_diff(root, {"trades": {"sdk-trade-1": row}}, prototype, persist=False)
    first = snapshot_entity(root["trades"]["sdk-trade-1"])
    _merge_diff(root, {"trades": {"sdk-trade-1": {"exchange_trade_id": "exchange-fill-1"}}}, prototype, persist=False)
    second = snapshot_entity(root["trades"]["sdk-trade-1"])
    context = get_channel("tq").descriptor.replay.model_dump(mode="json")
    context["stream_defaults"] = context.pop("streams")
    owner = SimpleNamespace(account_config=SimpleNamespace(account_mode="kq"))
    now = [1_700_000_000_000_000]
    recorder = ReplayRecorder(7, tmp_path, {"channel": "tq", **context}, now_us=lambda: now[0])
    try:
        recorder.attach(owner)
        recorder.configure(True, 1)
        _wait(lambda: recorder.status()["state"] in {"recording", "error"})
        assert recorder.status()["state"] == "recording", recorder.status()
        identity = recorder.status()["recording_id"]
        recorder.begin_execution("E", ["rb2610"])
        for snapshot in (first, second):
            now[0] += 1
            observation = replay.trade(owner, snapshot)
            recording.observe(owner, "/trade", {**observation, "execution_id": "E", "origin": "execution"})
        now[0] += 1
        recorder.end_execution("E")
        assert recorder.close()["state"] == "disabled", recorder.status()
        result = replay_window(
            tmp_path, 7, "E", {"recording_id": identity, "start_us": now[0] + 1, "end_us": now[0] + 2}
        )
        accounting = result["state_before_start"]["trade_accounting"]
        assert accounting["own_seen_keys"] == ["tq:7:sdk-trade-1"]
        assert len(accounting["own_records"]) == 1
        assert accounting["execution_totals"]["E"]["by_symbol"]["rb2610"] == {
            "quantity": 1.0,
            "notional": 3200.0,
            "complete": True,
        }
    finally:
        recorder.close()


def test_exchange_trade_id_without_sdk_identity_is_not_a_strong_fallback():
    owner = SimpleNamespace(_replay_account_id=7)
    data = replay.trade(owner, {"exchange_trade_id": "exchange-fill-1", "instrument_id": "rb2610"})
    assert data["trade_id"] == "exchange-fill-1"
    assert data["identity_quality"] == "weak" and data["trade_key"] is None


@pytest.mark.parametrize("enabled", [False, True])
def test_queued_native_cancel_keeps_entry_receipt_when_observer_changes(executor, monkeypatch, enabled):
    owner, api = executor
    old = _observe(owner) if enabled else []
    if not enabled:
        recording.set_replay_observer(owner, None)
    runtime = owner._require_runtime()
    original_call = runtime.call
    new = []

    def switch(operation, **kwargs):
        recording.set_replay_observer(owner, lambda topic, data: new.append((topic, data)))
        return original_call(operation, **kwargs)

    monkeypatch.setattr(runtime, "call", switch)
    assert owner._cancel_order_impl("rb2610", "o1")
    assert api.canceled == ["o1"]
    assert new == []
    assert [data["phase"] for topic, data in old if topic == "/command"] == (
        ["native_request", "native_returned"] if enabled else []
    )


def test_native_submit_receipt_crosses_owner_thread_and_rejects_secrets(executor, monkeypatch):
    owner, api = executor
    observations = _observe(owner)
    monkeypatch.setattr(
        owner, "_check_tq_symbol_trading_time", lambda *_args: TQTradingTimeCheck(TQTradingTimeStatus.OPEN)
    )
    with recording.replay_command(owner, "place_order", {"symbol": "rb2610"}) as identity:
        owner._place_order_impl("rb2610", OrderDirection.BUY, OrderType.LIMIT, 2, 3200, offset_flag="0")
    commands = [
        data for topic, data in observations if topic == "/command" and data.get("source") == "tq.native_request"
    ]
    assert [data["phase"] for data in commands] == ["native_request", "native_returned"]
    assert all(data["command_id"] == identity for data in commands)
    assert commands[0]["actual_parameters"] == api.insert_args
    assert "secret" not in repr(observations)


def _wait(predicate):
    wake = threading.Event()
    for _ in range(300):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


def test_real_recorder_native_mcap_includes_modes_queries_and_resynchronization(executor, tmp_path):
    owner, api = executor
    declaration = get_channel("tq").descriptor.replay.model_dump(mode="json")
    assert declaration["supported"]
    declaration["stream_defaults"] = declaration.pop("streams")
    context = {"schema_version": 1, "channel": "tq", **declaration}
    now = [1_793_404_800_000_000]
    recorder = ReplayRecorder(7, tmp_path, context, now_us=lambda: now[0])
    recorder.attach(owner)
    recorder.configure(True, 1)
    _wait(lambda: recorder.status()["state"] in {"recording", "error"})
    assert recorder.status()["state"] == "recording", recorder.status()
    identity = recorder.status()["recording_id"]
    try:
        recorder.begin_execution("TQ-E", ["rb2610"])
        now[0] += 10
        owner._require_runtime().call(lambda native: native.order.update(is_online=True))
        owner.query_recent_orders()
        owner.get_market_data(["rb2610"])
        owner.query_recent_trades()
        owner.get_account_assets()
        runtime = owner._require_runtime()
        generation = owner._replay_sync_generation
        now[0] += 10

        def notify(code):
            def update(native):
                native._data = {"notify": {str(code): {"code": code, "conn_id": "td"}}}
                runtime._pump_changes(native)

            runtime.call(update)

        notify(2019112911)
        assert not runtime.synchronized and owner._replay_sync_generation > generation
        now[0] += 10
        notify(2019112902)
        assert runtime.synchronized
        owner.query_recent_orders()
        now[0] += 10
        assert recorder.close()["state"] == "disabled", recorder.status()
        assert list(tmp_path.rglob("*.mcap"))
        manifest = replay_manifest(tmp_path, 7, "TQ-E")
        assert "demo" in json.dumps(manifest)
        result = replay_window(
            tmp_path, 7, "TQ-E", {"recording_id": identity, "start_us": now[0] - 100, "end_us": now[0] + 1}
        )
        serialized = json.dumps(result)
        assert "tq.order_set" in serialized and "tq.trade_query" in serialized
        assert "kq" in serialized and "secret" not in serialized
        assert recorder._session is None
    finally:
        recorder.close()


@pytest.mark.parametrize("mode,environment", [("live", "live"), ("kq", "demo"), ("sim", "demo")])
def test_modes_are_frozen_without_reading_credentials(mode, environment):
    config = SimpleNamespace(account_mode=mode, tq_password="secret")
    owner = SimpleNamespace(account_config=config, _config=lambda: config)
    assert TQExecutor.replay_context_metadata(owner) == {"environment": environment, "account_mode": mode}
    assert replay.order(owner, {})["attributes"] == {"account_mode": mode}


@pytest.mark.parametrize("status,left,error", [("FINISHED", 0, False), ("FINISHED", 2, False), ("ALIVE", 2, True)])
def test_terminal_order_never_retains_online_confirmation(status, left, error):
    data = replay.order(
        SimpleNamespace(),
        {"status": status, "is_online": True, "volume_orign": 2, "volume_left": left, "is_error": error},
    )
    assert data["confirmed_book"] is False


def test_channel_whitelist_accepts_all_normalized_observation_types(executor):
    owner, api = executor
    context = get_channel("tq").descriptor.replay.model_dump(mode="json")
    for topic, data in (
        ("/order", replay.order(owner, api.order)),
        ("/trade", replay.trade(owner, api.trade)),
        ("/quote/rb2610", replay.quote(owner, api.quote)),
        ("/account_observation", replay.account(owner, {}, {})),
    ):
        assert freeze_data(topic, data, context) == data


def test_queued_query_never_borrows_new_recording_token(executor, monkeypatch):
    owner, _api = executor
    old = _observe(owner)
    runtime = owner._require_runtime()
    original_call = runtime.call
    calls = []
    owner._replay_recorder = SimpleNamespace(begin_query=lambda scope: calls.append(scope))

    def switch(operation, **kwargs):
        recording.set_replay_observer(owner, lambda *_args: None)
        return original_call(operation, **kwargs)

    monkeypatch.setattr(runtime, "call", switch)
    owner._query_order_rows()
    assert calls == []
    assert old[-1][1]["query_basis"] is None


def test_runtime_normalization_failure_does_not_prevent_business_event(executor, monkeypatch):
    owner, _api = executor
    runtime = owner._require_runtime()
    delivered = []
    runtime.add_listener(lambda kind, row: delivered.append(kind))
    monkeypatch.setattr(replay, "event", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad observation")))
    runtime.call(lambda _api: runtime._emit("test", {}))
    assert "test" in delivered


def test_native_failure_preserves_error_and_does_not_record_message(executor, monkeypatch):
    owner, api = executor
    observations = _observe(owner)

    def fail(_order_id):
        raise ValueError("secret")

    monkeypatch.setattr(api, "cancel_order", fail)
    with pytest.raises(RuntimeError):
        owner._cancel_order_impl("rb2610", "o1")
    assert observations[-1][1]["phase"] == "native_error"
    assert observations[-1][1]["error_type"] == "ValueError"
    assert "secret" not in repr(observations)


def test_quote_only_symbol_retains_its_namespace():
    assert replay.symbol({"symbol": "KQ.m@SHFE.rb"}) == "KQ.m@SHFE.rb"
    assert replay.symbol({"symbol": "SHFE.rb2610"}) == "rb2610"
