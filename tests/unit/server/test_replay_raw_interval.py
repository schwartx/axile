"""Frozen interval inputs include late identity origins and strictly bounded book ranges."""

import json
import sqlite3
import zlib

from axile.server._native import _core
from axile.server.execution_replay.reader import metadata


def write_contract(root):
    writer = _core.ExecutionReplayWriter(str(root), 7, "interval-contract", '{"schema_version":2}')
    rows = [
        (
            1,
            "/capture",
            {"scope": {"kind": "account"}, "streams": ["/quote/S", "/market_trade/S", "/trade"], "boundary": "start"},
        ),
        (2, "/execution", {"execution_id": "interval-E", "phase": "start", "symbols": ["S"]}),
        (
            5,
            "/quote/S",
            {"symbol": "S", "book_valid": True, "bids": [[100, 3]], "asks": [[102, 1]], "valid_until_us": 15},
        ),
        (10, "/market_trade/S", {"symbol": "S", "identity_quality": "weak", "price": 100, "quantity": 2}),
        (
            11,
            "/trade",
            {
                "symbol": "S",
                "execution_id": "interval-E",
                "trade_key": "own",
                "identity_quality": "stable",
                "price": 100,
                "quantity": 1,
            },
        ),
        (
            20,
            "/market_trade/S",
            {
                "symbol": "S",
                "identity_quality": "stable",
                "trade_key": "M",
                "related_capture_seq": 4,
                "price": 100,
                "quantity": 2,
            },
        ),
        (
            22,
            "/market_trade/S",
            {"symbol": "S", "identity_quality": "stable", "trade_key": "N", "price": 356, "quantity": 3},
        ),
        (25, "/connection", {"scope": {"kind": "account"}, "connected": False}),
    ]
    for seq, (stamp, topic, data) in enumerate(rows, 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "interval-contract",
                    "capture_seq": seq,
                    "time_us": stamp,
                    "topic": topic,
                    "data": data,
                }
            )
        )
    writer.poll(1_000_030)
    writer.finish()


def test_market_link_origin_is_frozen_by_observation_cutoff_and_book_seed_is_before_start(tmp_path):
    write_contract(tmp_path)
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        db.row_factory = sqlite3.Row
        generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        commit = db.execute("SELECT commit_id FROM recordings").fetchone()[0]
        view = {
            "index_generation": generation,
            "commit_id": commit,
            "recording": "interval-contract",
            "through_seq": 8,
            "execution_id": "interval-E",
            "scope_symbol": "S",
            "interval_start_us": 10,
            "interval_end_us": 30,
            "cutoff_seq": 8,
        }
        latest = metadata(db, {**view, "operation": "market-objects", "kind": "market_trade"})
        assert [s["key"] for s in latest["subjects"]] == ["M", "N"]
        early = metadata(db, {**view, "operation": "market-objects", "kind": "market_trade", "cutoff_seq": 5})
        assert early["subjects"] == []
        links = metadata(db, {**view, "operation": "market-links", "objects": [6]})
        assert links["links"] == [{"seq": 4, "object": 6, "conflict": False}]
        assert metadata(db, {**view, "operation": "market-origin", "objects": [6], "after_seq": 5})["rebuild"]
        catalog = metadata(db, {**view, "operation": "range-blocks", "stream": 1, "catalog": True})
        blocks = [b for p in catalog["initial"] for b in p["blocks"]]
        observed = []
        with (tmp_path / "account-7/raw-interval-contract.mcap").open("rb") as f:
            for block in blocks:
                f.seek(block["offset"])
                observed.extend(json.loads(s) for s in zlib.decompress(f.read(block["length"])).splitlines())
        assert any(e["capture_seq"] == 3 and e["time_us"] == 5 for e in observed)
        assert any(e["capture_seq"] == 1 for e in observed)
        business = metadata(db, {**view, "operation": "business-blocks"})
        topics_by_stream = {
            r["id"]: r["topic"] for r in db.execute("SELECT id,topic FROM streams WHERE recording='interval-contract'")
        }
        assert business["blocks"] and all(
            not topics_by_stream[b["stream"]].startswith(("/quote/", "/market_trade/")) for b in business["blocks"]
        )
        controls = metadata(
            db, {**view, "operation": "range-blocks", "stream": 1, "catalog": True, "controls_only": True}
        )
        topics = {
            r["id"]: r["topic"] for r in db.execute("SELECT id,topic FROM streams WHERE recording='interval-contract'")
        }
        assert controls["streams"] and all(topics[s] in {"/connection", "/capture"} for s in controls["streams"])


def test_later_alias_does_not_hide_an_already_known_stream_at_an_older_cutoff(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "market-clock", '{"schema_version":2}')
    rows = [
        ("/execution", {"execution_id": "E", "phase": "start"}),
        ("/market_trade/S", {"symbol": "S", "trade_key": "M", "identity_quality": "stable", "price": 1, "quantity": 1}),
        ("/market_trade/B", {"symbol": "B", "identity_quality": "weak", "price": 1, "quantity": 1}),
        ("/market_trade/B", {"symbol": "B", "trade_key": "M", "identity_quality": "stable", "price": 1, "quantity": 1}),
        (
            "/market_trade/S",
            {
                "symbol": "S",
                "trade_key": "M",
                "identity_quality": "stable",
                "related_capture_seq": 3,
                "price": 1,
                "quantity": 1,
            },
        ),
    ]
    for seq, (topic, data) in enumerate(rows, 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "market-clock",
                    "capture_seq": seq,
                    "time_us": seq,
                    "topic": topic,
                    "data": data,
                }
            )
        )
        if seq == 4:
            writer.seal()
    writer.finish()
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        db.row_factory = sqlite3.Row
        generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        commit = db.execute("SELECT commit_id FROM recordings").fetchone()[0]
        response = metadata(
            db,
            {
                "operation": "market-streams",
                "execution_id": "E",
                "recording": "market-clock",
                "through_seq": 5,
                "cutoff_seq": 4,
                "commit_id": commit,
                "index_generation": generation,
                "objects": [2],
            },
        )
        source = db.execute("SELECT id FROM streams WHERE topic='/market_trade/B'").fetchone()[0]
        assert source in response["streams"]


def test_position_dependencies_include_connection_and_capture_interruptions(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "position-gaps", '{"schema_version":2}')
    rows = [
        (
            "/account_observation",
            {
                "execution_id": "E",
                "scope": {"kind": "account"},
                "positions": [{"symbol": "S", "position_side": "long", "quantity": 4}],
            },
        ),
        ("/connection", {"scope": {"kind": "account"}, "connected": False}),
        ("/capture", {"symbol": "S", "streams": ["/trade"], "boundary": "stop"}),
    ]
    for seq, (topic, data) in enumerate(rows, 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "position-gaps",
                    "capture_seq": seq,
                    "time_us": seq,
                    "topic": topic,
                    "data": data,
                }
            )
        )
    writer.finish()
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        db.row_factory = sqlite3.Row
        manifest = metadata(db, {"operation": "manifest", "execution_id": "E"})
        view = {
            "index_generation": manifest["index_generation"],
            "commit_id": manifest["recordings"][0]["commit_id"],
            "recording": "position-gaps",
            "through_seq": 3,
            "cutoff_seq": 3,
            "execution_id": "E",
        }
        objects = metadata(db, {**view, "operation": "objects", "kind": "position"})
        assert manifest["recordings"][0]["symbols"] == ["S"]
        catalog = metadata(db, {**view, "operation": "object-streams", "objects": [objects["subjects"][0]["id"]]})
        streams = {r["id"]: r["topic"] for r in db.execute("SELECT id,topic FROM streams")}
        assert {streams[i] for i in catalog["streams"]} == {"/account_observation", "/connection", "/capture"}
