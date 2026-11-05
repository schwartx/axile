"""账户录制持续运行时的身份和文件边界。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from axile.server._native import _core
from axile.server.execution_replay import recorder as module
from axile.server.execution_replay.contracts import freeze_data
from axile.server.execution_replay.service import replay_window
from tests.unit.server.test_execution_replay_native import TIME, finished, window
from tests.unit.server.test_execution_replay_native import make_recording as make_recording
from tests.unit.server.test_execution_replay_recorder import running as running


def test_evicted_hot_order_identity_is_restored_by_native_writer(make_recording, monkeypatch):
    recorder, now, root, recording_id = make_recording()
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    recorder.begin_execution("E", ["X"])
    for order_id in ("O1", "O2", "O3"):
        now[0] += 1
        recorder.observe(
            "/order",
            {"symbol": "X", "order_id": order_id, "status": "canceled", "execution_id": "E", "origin": "execution"},
        )
    assert ("X", "O1") not in recorder._order_executions
    now[0] += 1
    recorder.observe(
        "/trade",
        {"symbol": "X", "order_id": "O1", "trade_key": "T1", "identity_quality": "stable", "price": 100, "quantity": 2},
    )
    assert recorder.status()["state"] == "recording"
    finished(recorder)
    replay = window(root, "E", recording_id, TIME, now[0] + 1)
    trade = next(message["data"] for message in replay["messages"] if message["topic"] == "/trade")
    assert trade["execution_id"] == "E"
    assert trade["origin"] == "execution"


def test_old_terminal_history_cannot_evict_a_live_order_query_fence(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    query = recorder.begin_query(["X"])
    recorder.observe("/order", {"symbol": "X", "order_id": "LIVE", "status": "working"})
    for identity in ("O1", "O2", "O3"):
        recorder.observe("/order", {"symbol": "X", "order_id": identity, "status": "canceled"})
    assert ("X", "LIVE") in recorder._order_changes
    recorder.observe(
        "/order_set",
        {
            **query,
            "scope": {"kind": "symbols", "symbols": ["X"]},
            "scope_complete": True,
            "synchronized": True,
            "orders": [],
        },
    )
    assert ("X", "LIVE") in recorder._orders
    assert recorder.status()["state"] == "recording"


def test_live_orders_do_not_stop_recording_when_the_recent_cache_is_full(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    for order_id in ("O1", "O2", "O3"):
        recorder.observe("/order", {"symbol": "X", "order_id": order_id, "status": "working"})
    assert recorder.status()["state"] == "recording"
    assert all(("X", order_id) in recorder._orders for order_id in ("O1", "O2", "O3"))


def test_seed_and_concurrent_queries_are_not_limited_by_recent_cache(running, monkeypatch):
    recorder, owner, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    owner.replay_seed_observations = lambda: iter(
        ("/order", {"symbol": "X", "order_id": f"O{index}", "status": "working"}) for index in range(3)
    )
    recorder._seed_cached_observations(recorder._session)
    tokens = [recorder.begin_query(["A", "B", "C"]) for _ in range(3)]
    assert all(token is not None for token in tokens)
    assert len(recorder._orders) == 3
    assert recorder.status()["state"] == "recording"


def test_process_crash_exposes_only_the_committed_mcap_prefix(tmp_path):
    script = """
import json, os, sys
from axile.server._native import _core
w = _core.ExecutionReplayWriter(sys.argv[1], 7, 'crashed', '{"schema_version":1}')
def append(seq, topic, data):
    w.append(json.dumps({'schema_version':1, 'recording_id':'crashed', 'capture_seq':seq,
        'time_us':int(sys.argv[2])+seq, 'topic':topic, 'data':data}))
append(1, '/execution', {'execution_id':'E', 'phase':'started', 'symbol':'X'})
w.seal()
append(2, '/order', {'symbol':'X', 'order_id':'uncommitted'})
os._exit(0)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[3])}
    subprocess.run([sys.executable, "-c", script, str(tmp_path), str(TIME)], env=environment, check=True, timeout=10)

    replay = window(tmp_path, "E", "crashed", TIME, TIME + 3)
    assert [message["capture_seq"] for message in replay["messages"]] == [1]
    assert replay["coverage"] == [{"start_us": TIME + 1, "end_us": TIME + 2}]
    assert list(tmp_path.rglob("*.mcap.partial"))


def test_own_trade_history_does_not_stop_recording_before_file_limit(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "own-history", '{"schema_version":1}')

    def append(sequence, topic, data):
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "recording_id": "own-history",
                    "capture_seq": sequence,
                    "time_us": TIME + sequence,
                    "topic": topic,
                    "data": data,
                }
            )
        )

    append(1, "/execution", {"execution_id": "E", "phase": "started", "symbol": "X"})
    for sequence in range(2, 301):
        append(
            sequence,
            "/trade",
            {
                "symbol": "X",
                "execution_id": "E",
                "order_id": "O",
                "trade_key": f"T{sequence}",
                "identity_quality": "stable",
                "price": 100,
                "quantity": 3,
                "source": "S" * 8_000,
            },
        )
    append(
        301,
        "/trade",
        {
            "symbol": "X",
            "order_id": "O",
            "trade_key": "T2",
            "identity_quality": "stable",
            "price": 100,
            "quantity": 3,
            "direction": "BUY",
        },
    )
    writer.finish()
    assert len(list(tmp_path.rglob("*.mcap"))) == 1
    late = window(tmp_path, "E", "own-history", TIME + 301, TIME + 302)
    assert len(json.dumps(late["state_before_start"]).encode()) > 2 * 1024 * 1024
    assert late["state_before_start"]["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["X"]["quantity"] == 897
    assert late["messages"][0]["data"]["trade_key"] == "T2"
    assert late["messages"][0]["data"]["direction"] == "BUY"


def test_event_larger_than_256_kib_is_recorded_and_read(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "large-event", '{"schema_version":1}')
    events = [
        ("/execution", {"execution_id": "E", "phase": "started", "symbol": "X"}),
        (
            "/order_set",
            {
                "scope": {"kind": "account", "symbols": []},
                "scope_complete": False,
                "synchronized": False,
                "orders": [
                    {"symbol": "X", "order_id": f"O{i}", "status": "unknown", "source": "X" * 600} for i in range(600)
                ],
            },
        ),
    ]
    for sequence, (topic, data) in enumerate(events, 1):
        freeze_data(topic, data, {})
        message = json.dumps(
            {
                "schema_version": 1,
                "recording_id": "large-event",
                "capture_seq": sequence,
                "time_us": TIME + sequence,
                "topic": topic,
                "data": data,
            }
        )
        if sequence == 2:
            assert len(message.encode()) > 256 * 1024
        writer.append(message)
    writer.finish()
    replay = window(tmp_path, "E", "large-event", TIME, TIME + 3)
    assert len(replay["messages"][1]["data"]["orders"]) == 600


def test_order_set_is_not_limited_by_old_array_or_node_counts(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "many-orders", '{"schema_version":1}')
    orders = [
        {
            "symbol": "X",
            "order_id": f"O{i}",
            "status": "unknown",
            "source": "feed",
            "direction": "BUY",
            "price": 100,
            "quantity": 1,
            "filled_quantity": 0,
            "remaining_quantity": 1,
        }
        for i in range(2_100)
    ]
    events = [
        ("/execution", {"execution_id": "E", "phase": "started", "symbol": "X"}),
        (
            "/order_set",
            {
                "scope": {"kind": "account", "symbols": []},
                "scope_complete": False,
                "synchronized": False,
                "orders": orders,
            },
        ),
    ]
    for sequence, (topic, data) in enumerate(events, 1):
        freeze_data(topic, data, {})
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "recording_id": "many-orders",
                    "capture_seq": sequence,
                    "time_us": TIME + sequence,
                    "topic": topic,
                    "data": data,
                }
            )
        )
    writer.finish()
    replay = window(tmp_path, "E", "many-orders", TIME, TIME + 3)
    assert len(replay["messages"][1]["data"]["orders"]) == 2_100


def test_event_larger_than_one_page_is_reassembled_by_frontend(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "large-event-pages", '{"schema_version":1}')
    events = [
        ("/execution", {"execution_id": "E", "phase": "started", "symbol": "X"}),
        ("/quote/X", {"symbol": "X", "bids": [], "asks": [], "source": "X" * 4_300_000}),
        ("/quote/X", {"symbol": "X", "bids": [], "asks": [], "source": "after"}),
    ]
    for sequence, (topic, data) in enumerate(events, 1):
        message = json.dumps(
            {
                "schema_version": 1,
                "recording_id": "large-event-pages",
                "capture_seq": sequence,
                "time_us": TIME + sequence,
                "topic": topic,
                "data": data,
            }
        )
        if sequence == 2:
            assert len(message.encode()) > 4 * 1024 * 1024
        freeze_data(topic, data, {})
        writer.append(message)
    writer.finish()
    request = {"recording_id": "large-event-pages", "start_us": TIME + 2, "end_us": TIME + 4}
    pages = []
    while True:
        page = replay_window(tmp_path, 7, "E", request)
        pages.append(page)
        if page["complete"]:
            break
        request["cursor"] = page["next_cursor"]
    assert sum("message_fragment" in page for page in pages) > 1
    payload = tmp_path / "event-pages.json"
    payload.write_text(json.dumps({"pages": pages, "start": TIME + 2, "end": TIME + 4}))
    script = """
import { readReplayWindow } from './src/lib/api/replay.ts';
const input = await Bun.file(process.argv[1]).json();
let index = 0;
globalThis.fetch = async () => new Response(JSON.stringify(input.pages[index++]), {headers:{'Content-Type':'application/json'}});
const result = await readReplayWindow('E', {recording_id:'large-event-pages', start_us:input.start, end_us:input.end});
if (result.messages.length !== 2 || result.messages[0].capture_seq !== 2 ||
    result.messages[0].data.source.length !== 4300000 || result.messages[1].capture_seq !== 3 || index !== input.pages.length)
  throw new Error('large replay event did not reconstruct');
"""
    subprocess.run(
        [shutil.which("bun") or "bun", "-e", script, str(payload)],
        cwd=Path(__file__).parents[3] / "ui",
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_large_native_checkpoint_pages_reconstruct_in_frontend(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "large-pages", '{"schema_version":1}')

    def append(sequence, topic, data):
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "recording_id": "large-pages",
                    "capture_seq": sequence,
                    "time_us": TIME + sequence,
                    "topic": topic,
                    "data": data,
                }
            )
        )

    append(1, "/execution", {"execution_id": "E", "phase": "started", "symbol": "X"})
    for sequence in range(2, 1201):
        append(
            sequence,
            "/trade",
            {
                "symbol": "X",
                "execution_id": "E",
                "order_id": "O",
                "trade_key": f"T{sequence}",
                "identity_quality": "stable",
                "price": 100,
                "quantity": 3,
                "source": "S" * 4_000,
            },
        )
    append(1201, "/quote/X", {"symbol": "X", "last_price": 100})
    writer.finish()
    request = {"recording_id": "large-pages", "start_us": TIME + 1201, "end_us": TIME + 1202}
    pages = []
    while True:
        page = replay_window(tmp_path, 7, "E", request)
        pages.append(page)
        if page["complete"]:
            break
        request["cursor"] = page["next_cursor"]
    assert len(pages) > 2
    payload = tmp_path / "pages.json"
    payload.write_text(json.dumps({"pages": pages, "start": TIME + 1201, "end": TIME + 1202}))
    script = """
import { readReplayWindow } from './src/lib/api/replay.ts';
const input = await Bun.file(process.argv[1]).json();
let index = 0;
globalThis.fetch = async () => new Response(JSON.stringify(input.pages[index++]), {headers:{'Content-Type':'application/json'}});
const result = await readReplayWindow('E', {recording_id:'large-pages', start_us:input.start, end_us:input.end});
if (result.state_before_start.trade_accounting.execution_totals.E.by_symbol.X.quantity !== 3597 ||
    result.messages.length !== 1 || result.messages[0].capture_seq !== 1201 || index !== input.pages.length)
  throw new Error('large replay state did not reconstruct');
"""
    subprocess.run(
        [shutil.which("bun") or "bun", "-e", script, str(payload)],
        cwd=Path(__file__).parents[3] / "ui",
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_market_duplicate_matches_between_continuous_and_late_window(make_recording, tmp_path):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", ["X"])
    trade = {"symbol": "X", "trade_key": "T", "identity_quality": "stable", "price": 100, "quantity": 3}
    now[0] += 1
    recorder.observe("/market_trade/X", trade)
    now[0] += 1
    recorder.end_execution("E")
    now[0] += 1
    recorder.observe("/market_trade/X", trade)
    now[0] += 1
    recorder.observe("/execution", {"execution_id": "E", "phase": "observe"})
    finished(recorder)
    full = window(root, "E", recording_id, TIME, now[0] + 1)
    late = window(root, "E", recording_id, TIME + 3, now[0] + 1)
    payload = tmp_path / "market-windows.json"
    payload.write_text(json.dumps({"full": full, "late": late, "cursor": now[0]}))
    script = """
import { projectReplay } from './src/features/replay/model.ts';
const data = await Bun.file(process.argv[1]).json();
const full = projectReplay(data.full, {time_us:data.cursor}).marketTrades;
const late = projectReplay(data.late, {time_us:data.cursor}).marketTrades;
if (JSON.stringify(full) !== JSON.stringify(late) || full.length !== 1) throw new Error('market identity differs');
"""
    subprocess.run(
        [shutil.which("bun") or "bun", "-e", script, str(payload)],
        cwd=Path(__file__).parents[3] / "ui",
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
