"""真实 CTP 查询结果只能沿原查询证明进入回放。"""

import threading
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td
from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording

from axile.channels.builtins import _ctp_plugin
from axile.common.serialization import to_payload
from axile.executor.ctp import replay as replay_capture
from axile.server.execution_replay.recorder import ReplayRecorder
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker

SYMBOL = "ag2612"
ORDER_ID = "20260909:7:1:10"
ORDER_KEY = f'["{SYMBOL}","{ORDER_ID}"]'


def working_order(recording, **changes):
    return SimpleNamespace(
        **(
            vars(recording.broker.order)
            | {
                "OrderStatus": td.THOST_FTDC_OST_NoTradeQueueing,
                "VolumeTotalOriginal": 4,
                "VolumeTraded": 0,
                "LimitPrice": 9000,
            }
            | changes
        )
    )


def configure_query(recording, row):
    def response(_request, rid):
        recording.broker.trader_spi.OnRspQryOrder(row, None, rid, True)
        return 0

    recording.broker.trader.ReqQryOrder.side_effect = response


def query(recording, entry):
    executor = recording.executor
    if entry == "pending":
        return executor.get_pending_orders(SYMBOL)
    if entry == "recent":
        return executor.query_recent_orders(SYMBOL)
    return executor.reconcile_terminal_order(SYMBOL, ORDER_ID)


def wait_until(predicate):
    wake = threading.Event()
    for _ in range(300):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


@pytest.fixture
def starting_recording(monkeypatch, tmp_path):
    broker = ScriptedBroker(monkeypatch)
    broker.rows["ReqQryOrder"] = [working_order(SimpleNamespace(broker=broker))]
    broker.rows["ReqQryTrade"] = []
    context = to_payload(_ctp_plugin().descriptor.replay, mode="json")
    context["stream_defaults"] = context.pop("streams")
    context["channel"] = "ctp"
    now = [1_793_404_800_000_000]
    root = tmp_path / "replay"
    recorder = ReplayRecorder(17, root, context, now_us=lambda: now[0])
    recorder.attach(broker.executor)
    recorder.configure(True, 1)
    recorder.begin_execution("E-early", [SYMBOL])
    yield SimpleNamespace(
        broker=broker,
        executor=broker.executor,
        recorder=recorder,
        root=root,
        now=now,
        recording_id=recorder.status()["recording_id"],
    )
    recorder.close()
    broker.executor.close()


@pytest.mark.parametrize("entry", ["pending", "recent", "terminal"])
def test_query_error_releases_unpublished_proof_without_changing_error(recording, monkeypatch, entry):
    failure = RuntimeError("isolated query failure")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(recording.executor, "_query", fail)
    with pytest.raises(RuntimeError) as caught:
        query(recording, entry)
    assert caught.value is failure
    assert recording.recorder._session.queries == {}
    assert recording.recorder.status()["state"] != "error"


def test_capture_keeps_the_query_token_generation_instead_of_the_current_generation():
    observations = []
    owner = SimpleNamespace(
        _replay_observer=lambda topic, data: observations.append((topic, data)), _replay_sync_generation=8
    )
    replay_capture.capture(owner, "/order_set", lambda: {"sync_generation": 3})
    assert observations == [("/order_set", {"sync_generation": 3})]
