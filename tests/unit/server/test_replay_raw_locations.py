"""定位页跨语言编码、边界与损坏拒绝；不运行业务投影。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from axile.server._native import _core
from axile.server.execution_replay import reader
from axile.server.execution_replay.raw_locations import _Ints, _page, at_sequence, locate


def sealed(values):
    body = bytearray()
    for value in values:
        while value >= 128:
            body.append((value & 127) | 128)
            value >>= 7
        body.append(value)
    return bytes(body) + hashlib.sha256(body).digest()[:16]


def test_compact_page_roundtrip_handles_reverse_block_jumps_and_exact_integers():
    row = {
        "first_seq": 1,
        "last_seq": 3,
        "first_time": 100,
        "last_time": 9_007_199_254_740_991,
        "positions": sealed([0, 200, 0, 0, 197, 1, 9_007_199_254_740_891, 20, 1023]),
    }
    work = []
    result = _page(row, lambda n, read_bytes=0: work.append(n))
    assert [(r["seq"], r["time"], r["block"], r["line"]) for r in result] == [
        (1, 100, 100, 0),
        (2, 100, 1, 1),
        (3, 9_007_199_254_740_991, 11, 1023),
    ]
    assert sum(work) == len(row["positions"]) + 9


@pytest.mark.parametrize(
    "blob", [b"", b"\x80" * 17, sealed([0, 0, 0]), sealed([0, 2, 1024]), sealed([1, 2, 0]), sealed([0, 2, 0, 0])]
)
def test_bad_page_cannot_be_interpreted_as_an_empty_result(blob):
    row = {"first_seq": 1, "last_seq": 1, "first_time": 1, "last_time": 1, "positions": blob}
    with pytest.raises(reader.ReadError, match="REPLAY_INDEX_LOCATIONS"):
        _page(row, lambda _, read_bytes=0: None)


def test_checksum_is_checked_and_charged_before_parse():
    blob = bytearray(sealed([0, 2, 0]))
    blob[0] ^= 1
    work = []
    with pytest.raises(reader.ReadError, match="REPLAY_INDEX_LOCATIONS"):
        _page(
            {"first_seq": 1, "last_seq": 1, "first_time": 1, "last_time": 1, "positions": bytes(blob)},
            lambda n, read_bytes=0: work.append(n),
        )
    assert work == [len(blob)]
    bad = b"\x80\x00"
    bad += hashlib.sha256(bad).digest()[:16]
    with pytest.raises(reader.ReadError, match="REPLAY_INDEX_LOCATIONS"):
        _Ints(bad, 100, lambda _, read_bytes=0: None).read()


def test_native_interleaved_pages_quiet_commit_and_sparse_stream_selection(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    events = []
    for seq in range(1, 701):
        topic = "/quote/sparse" if seq in [1, 200, 600] else "/quote/busy"
        event = {
            "schema_version": 2,
            "recording_id": "R",
            "capture_seq": seq,
            "time_us": seq // 3,
            "topic": topic,
            "data": {"execution_id": "E", "symbol": topic.rsplit("/", 1)[1], "quantity": seq},
        }
        writer.append(json.dumps(event))
        events.append(event)
        if seq in [1, 2, 3, 4, 5, 100]:
            writer.seal()
    writer.finish()
    with (
        reader.index(tmp_path, 7, lambda: None) as (db, _, _),
        sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as oracle,
    ):
        oracle.row_factory = sqlite3.Row
        streams = {r["topic"]: r["id"] for r in oracle.execute("SELECT id,topic FROM streams WHERE recording='R'")}
        pages = list(oracle.execute("SELECT * FROM location_pages ORDER BY first_seq"))
        assert sum(p["last_seq"] - p["first_seq"] + 1 for p in pages) == 700
        assert all(p["last_seq"] - p["first_seq"] < 256 for p in pages)
        for through, target, tie in [
            (100, 500, 9_007_199_254_740_991),
            (700, 167, 502),
            (700, 1, 4),
            (700, 0, 1),
            (700, 500, 700),
        ]:
            cutoff = [
                e
                for e in events[:through]
                if (e["time_us"], e["capture_seq"]) <= min((target, tie), (events[through - 1]["time_us"], through))
            ]
            selected, evidence = locate(
                db, "R", through, target, tie, streams["/quote/sparse"], lambda _, read_bytes=0: None
            )
            assert [r["seq"] for r in evidence] == [e["capture_seq"] for e in cutoff[-32:][::-1]]
            quotes = [e for e in cutoff if e["topic"] == "/quote/sparse"]
            assert selected["seq"] == quotes[-1]["capture_seq"]
        # The sparse stream is absent from the latest global page and must use its block index.
        chosen, evidence = locate(db, "R", 700, 233, 700, streams["/quote/sparse"], lambda _, read_bytes=0: None)
        assert chosen["seq"] == 600 and all(r["seq"] != 600 for r in evidence)
        block = oracle.execute(
            "SELECT positions FROM blocks WHERE recording='R' AND id=?", [chosen["block"]]
        ).fetchone()[0]
        assert isinstance(block, bytes)
        for seq in [1, 256, 257, 700]:
            assert at_sequence(db, "R", 700, seq, lambda *_: None)["time"] == events[seq - 1]["time_us"]
        assert at_sequence(db, "R", 700, 701, lambda *_: None) is None


def test_every_single_event_commit_still_needs_at_most_32_evidence_pages(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    for seq in range(1, 41):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": 1,
                    "topic": "/quote/S",
                    "data": {"execution_id": "E", "symbol": "S"},
                }
            )
        )
        writer.seal()
    writer.finish()
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        selected, evidence = locate(db, "R", 40, 1, 40, 1, lambda _, read_bytes=0: None)
        assert selected["seq"] == 40 and [r["seq"] for r in evidence] == list(range(40, 8, -1))
    # Valid large blocks change only the optional preview size, never the target cursor.
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        db.execute("UPDATE blocks SET length=?", [600 * 1024])
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        manifest = reader.metadata(db, {"operation": "manifest", "execution_id": "E"})
        plan = reader.metadata(
            db,
            {
                "operation": "plan",
                "execution_id": "E",
                "recording": "R",
                "through_seq": 40,
                "commit_id": manifest["recordings"][0]["commit_id"],
                "index_generation": manifest["index_generation"],
                "time_us": 1,
                "stream": 1,
            },
        )
        assert plan["planned_bytes"] <= 4 * 1024 * 1024
        assert plan["cutoff_seq"] == 40 and plan["selected"]["seq"] == 40
        assert plan["previous"]["seq"] == 39 and plan["next"] is None
        assert len(plan["evidence"]) < 32


def test_location_page_storage_does_not_waste_an_overflow_page_per_blob():
    with sqlite3.connect(":memory:") as db:
        db.executescript(Path("rust/storage/src/execution_replay_raw.sql").read_text())
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        payload = b"x" * (page_size // 4 + 64)
        db.executemany(
            "INSERT INTO location_pages VALUES (?,?,?,?,?,?)",
            [
                ("r" * 32, i * 256 + 1, i * 256 + 256, 1_000_000_000_000, 1_000_000_000_000, payload)
                for i in range(2048)
            ],
        )
        allocated = db.execute("PRAGMA page_count").fetchone()[0] * page_size
        assert allocated < len(payload) * 2048 * 1.8


def test_decoded_index_bytes_remain_charged_even_when_checksum_fails(tmp_path, monkeypatch):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    for seq in range(1, 101):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": seq,
                    "topic": "/quote/S",
                    "data": {"execution_id": "E", "symbol": "S"},
                }
            )
        )
    writer.finish()
    clock = [0.0]
    owner = reader.Readers(lambda: clock[0])
    token = {**owner.open(7, "E", tmp_path), "root_seq": 1, "attempt": 1}
    clock[0] = 12
    monkeypatch.setattr(reader, "readers", owner)
    manifest, release, _ = reader.read(tmp_path, 7, "E", {**token, "operation": "manifest"})
    release()
    view = {
        **token,
        "recording": "R",
        "through_seq": 100,
        "commit_id": manifest["recordings"][0]["commit_id"],
        "index_generation": manifest["index_generation"],
        "operation": "plan",
        "time_us": 100,
        "stream": 1,
    }
    result, release, _ = reader.read(tmp_path, 7, "E", view)
    release()
    assert result["selected"]["seq"] == 100
    assert all("positions" not in block for block in result["blocks"])
    first = owner.control(7, "E", {**token, "operation": "seal"})
    owner.control(7, "E", {**token, "operation": "ack"})
    assert first["index_bytes"] > 0 and first["index_bytes"] == first["session_index_bytes"]
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        blob = bytearray(db.execute("SELECT positions FROM location_pages").fetchone()[0])
        blob[0] ^= 1
        db.execute("UPDATE location_pages SET positions=?", [bytes(blob)])
    before = owner.accounts[7].total_index_bytes
    before_total = owner.accounts[7].total_bytes
    with pytest.raises(reader.ReadError, match="REPLAY_INDEX_LOCATIONS"):
        reader.read(tmp_path, 7, "E", {**view, "root_seq": 2})
    assert owner.accounts[7].active == 0
    assert owner.accounts[7].total_index_bytes - before == len(blob)
    assert owner.accounts[7].total_bytes - before_total == len(blob)
