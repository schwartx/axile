"""原始身份、逐行证明位置及离线重建发布规则。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from axile.server._native import _core
from axile.server.execution_replay import reader


@pytest.fixture
def source(tmp_path, monkeypatch):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    rows = [
        (
            "/command",
            {"command_id": "C", "operation": "place_order", "execution_id": "E", "symbol": "S", "order_id": "O"},
        ),
        ("/trade", {"quantity": 2, "price": 4, "symbol": "S", "order_id": "O"}),
        (
            "/trade",
            {
                "identity_quality": "stable",
                "trade_key": "opaque:T",
                "quantity": 2,
                "price": 4,
                "symbol": "S",
                "related_capture_seq": 2,
            },
        ),
        (
            "/account_observation",
            {
                "scope": {"kind": "account"},
                "positions": [{"symbol": "S", "position_side": "LONG", "quantity": 7}],
                "position_basis": {
                    "status": "aligned",
                    "proof": {
                        "kind": "included_trade_keys",
                        "complete": True,
                        "included_trade_keys": ["opaque:T", "only-in-proof"],
                    },
                },
            },
        ),
        (
            "/account_observation",
            {
                "scope": {"kind": "account"},
                "positions": [{"symbol": "S", "position_side": "LONG", "today_quantity": 3}],
            },
        ),
    ]
    for seq, (topic, data) in enumerate(rows, 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": seq,
                    "topic": topic,
                    "data": data,
                }
            )
        )
    writer.poll(2_000_000)
    writer.finish()
    del writer
    clock = [0.0]
    owner = reader.Readers(lambda: clock[0])
    token = {**owner.open(7, "E", tmp_path), "root_seq": 1, "attempt": 1}
    clock[0] = 12
    monkeypatch.setattr(reader, "readers", owner)
    manifest, release, _ = reader.read(tmp_path, 7, "E", {**token, "operation": "manifest"})
    release()
    view = {
        **token,
        "index_generation": manifest["index_generation"],
        "recording": "R",
        "through_seq": 5,
        "commit_id": manifest["recordings"][0]["commit_id"],
        "cutoff_seq": 5,
    }
    return tmp_path, owner, view


def query(source, operation, **fields):
    path, _, view = source
    result, release, _ = reader.read(path, 7, "E", {**view, "operation": operation, **fields})
    release()
    return result


def test_direct_identity_page_excludes_reference_only_keys_and_keeps_resolution_history(source):
    trades = query(source, "objects", kind="trade")
    assert [subject["key"] for subject in trades["subjects"]] == ["opaque:T"]
    weak = query(source, "objects", kind="weak_trade")["subjects"]
    assert len(weak) == 1 and weak[0]["key"] == "2"
    objects = [weak[0]["id"]]
    streams = query(source, "object-streams", objects=objects)["streams"]
    blocks = query(source, "object-blocks", objects=objects, stream=streams[0])["blocks"]
    assert blocks[0]["first_seq"] == 2 and blocks[0]["last_seq"] == 3
    assert query(source, "objects", kind="trade", cutoff_seq=2)["subjects"] == []


def test_command_mentions_are_direct_cutoff_bound_and_generation_checked_first(source):
    commands = query(source, "commands", order_ids=["O"])
    assert [s["key"] for s in commands["subjects"]] == ["C"]
    assert query(source, "commands", order_ids=["O"], cutoff_seq=0)["subjects"] == []
    assert query(source, "commands", order_ids=["missing"])["next"] is None
    stable = query(source, "objects", kind="trade")["subjects"][0]
    assert [s["key"] for s in query(source, "commands", objects=[stable["id"]])["subjects"]] == ["C"]
    assert query(source, "commands", objects=[stable["id"]], cutoff_seq=2)["subjects"] == []
    with pytest.raises(reader.ReadError, match="REPLAY_OBJECT_BUDGET"):
        query(source, "commands", order_ids=["O"] * 9)
    path, _, view = source
    with reader.index(path, 7, lambda: None) as (db, _, _):
        with pytest.raises(reader.ReadError, match="REPLAY_INDEX_GENERATION"):
            reader.metadata(
                db,
                {
                    **view,
                    "execution_id": "E",
                    "operation": "commands",
                    "index_generation": "retired",
                    "order_ids": [{"invalid": True}],
                },
            )


def test_members_are_raw_positions_and_absence_requires_a_published_proof(source):
    members = query(source, "members", proof_seq=4, row=-1, keys=["opaque:T", "only-in-proof", "absent"])
    assert members["positions"] == [0, 1, None] and members["members_count"] == 2
    stable = query(source, "objects", kind="trade")["subjects"][0]
    assert query(source, "members", proof_seq=4, row=-1, objects=[stable["id"]])["positions"] == [0]
    with pytest.raises(reader.ReadError, match="REPLAY_PROOF_UNPUBLISHED"):
        query(source, "members", proof_seq=5, row=-1, keys=["absent"])


def test_dependency_pages_are_bounded_by_count_bytes_and_frozen_cutoff(source):
    path, _, _ = source
    with sqlite3.connect(path / "account-7/.replay-v2.sqlite") as db:
        db.executemany(
            "INSERT INTO objects(recording,kind,key,first_seq) VALUES ('R','trade',?,?)",
            [(f"{i:04d}" + "x" * 4000, 5) for i in range(400)],
        )
    small = query(source, "objects", kind="trade")
    assert len(small["subjects"]) == 8
    after, identities = [0, 0], []
    for _ in range(100):
        page = query(source, "objects", kind="trade", limit=128, after=after)
        assert 1 <= len(page["ids"]) <= 128
        assert len(page["ids"]) == len(page["keys"])
        assert len(json.dumps(page).encode()) < 80 * 1024
        identities.extend(s[1] for s in page["ids"])
        if page["next"] is None:
            break
        assert page["next"] > after
        after = page["next"]
    assert len(identities) == len(set(identities)) == 401
    assert query(source, "objects", kind="trade", limit=128, cutoff_seq=2)["ids"] == []
    plan = query(source, "objects", kind="trade", limit=128, planning=True)
    assert len(plan["ids"]) == 128 and "keys" not in plan and len(json.dumps(plan)) < 8192
    with pytest.raises(reader.ReadError, match="REPLAY_CURSOR_INVALID"):
        query(source, "objects", kind="trade", limit=129)
    with pytest.raises(reader.ReadError, match="REPLAY_OBJECT_BUDGET"):
        query(source, "objects", kind="position", limit=128)


def test_dense_proof_members_seek_by_object_id_without_scanning_the_set(source):
    path, _, view = source
    with sqlite3.connect(path / "account-7/.replay-v2.sqlite") as db:
        db.executemany(
            "INSERT INTO objects(id,recording,kind,key,first_seq) VALUES (?,'R','trade',?,5)",
            [(i + 100, f"T{i}") for i in range(20_000)],
        )
        db.execute("UPDATE proofs SET members=10002 WHERE seq=4 AND row=-1")
        db.executemany(
            "INSERT INTO proof_members VALUES ('R',4,-1,?,?)",
            [(i + 100, i + 2) for i in range(10_000)],
        )
    ids = list(range(100, 164)) + list(range(20_000, 20_064))
    with reader.index(path, 7, lambda: None) as (db, _, steps):
        result = reader.metadata(
            db, {**view, "execution_id": "E", "operation": "members", "objects": ids, "proof_seq": 4, "row": -1}
        )
        assert result["positions"] == list(range(2, 66)) + [None] * 64
        assert result["members_count"] == 10002
        assert steps[0] < 8_000


def test_rebuild_waits_for_real_owner_and_changes_generation_without_rewriting_raw(source):
    path, owner, view = source
    with pytest.raises(_core.NativeError, match="REPLAY_BUSY"):
        _core.execution_replay_rebuild(str(path), 7)
    # The read owner is explicitly stopped before administrative rebuilding.
    owner._owner = None
    raw = path / "account-7/raw-R.mcap"
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    rebuilt = json.loads(_core.execution_replay_rebuild(str(path), 7))
    assert rebuilt["observations"] == 5 and rebuilt["index_generation"] != view["index_generation"]
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before
    with sqlite3.connect(path / "account-7/.replay-v2.sqlite") as db:
        assert db.execute("SELECT commit_id FROM recordings").fetchone()[0] == view["commit_id"]
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with reader.index(path, 7, lambda: None) as (db, _, _):
        with pytest.raises(reader.ReadError, match="REPLAY_INDEX_GENERATION"):
            reader.metadata(db, {**view, "execution_id": "E", "operation": "members", "proof_seq": ["old-ID"]})


def test_failed_rebuild_preserves_the_published_index(source):
    path, owner, view = source
    owner._owner = None
    with sqlite3.connect(path / "account-7/.replay-v2.sqlite") as db:
        offset = db.execute("SELECT offset FROM blocks ORDER BY id LIMIT 1").fetchone()[0]
    raw = path / "account-7/raw-R.mcap"
    with raw.open("r+b") as file:
        file.seek(offset)
        byte = file.read(1)
        file.seek(offset)
        file.write(bytes([byte[0] ^ 255]))
    with pytest.raises(_core.NativeError):
        _core.execution_replay_rebuild(str(path), 7)
    with sqlite3.connect(path / "account-7/.replay-v2.sqlite") as db:
        assert db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0] == view["index_generation"]
        assert db.execute("SELECT sum(last_seq-first_seq+1) FROM location_pages").fetchone()[0] == 5
    assert list((path / "account-7").glob(".replay-rebuild-*")) == []


def test_stream_discovery_seeks_past_future_blocks_instead_of_scanning_history():
    from axile.server.execution_replay.raw_index import locations

    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript(Path("rust/storage/src/execution_replay_raw.sql").read_text())
        db.execute("INSERT INTO streams VALUES ('R',1,'/trade','future')")
        db.execute("INSERT INTO streams VALUES ('R',2,'/trade','S')")
        db.execute(
            "INSERT INTO blocks(recording,id,stream,offset,length,raw_length,sha256,first_seq,last_seq) VALUES ('R',50000,2,0,1,1,'hash',2,2)"
        )
        db.execute("INSERT INTO objects VALUES (1,'R','trade','T',2)")
        db.executemany(
            "INSERT INTO object_blocks VALUES ('R',1,1,?,?,?)", [(i, i + 100, i + 100) for i in range(20_000)]
        )
        db.execute("INSERT INTO object_blocks VALUES ('R',1,2,50000,2,2)")
        steps = [0]

        def progress():
            steps[0] += 10
            return int(steps[0] > 1000)

        db.set_progress_handler(progress, 10)
        result = locations(
            db,
            {"recording": "R", "operation": "object-streams", "through_seq": 20100, "cutoff_seq": 50, "objects": [1]},
        )
        assert result["streams"] == [2] and steps[0] < 1000


def test_quiet_commit_is_a_distinct_frozen_view_and_cannot_borrow_a_new_watermark(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R",
                "capture_seq": 1,
                "time_us": 1,
                "topic": "/quote/S",
                "data": {"execution_id": "E", "symbol": "S"},
            }
        )
    )
    writer.poll(1_000_001)
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        first = db.execute("SELECT commit_id FROM recordings").fetchone()[0]
    writer.poll(2_000_001)
    writer.finish()
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        request = {
            "operation": "plan",
            "recording": "R",
            "execution_id": "E",
            "through_seq": 1,
            "commit_id": first,
            "index_generation": generation,
            "stream": 1,
            "time_us": 1_000_002,
        }
        with pytest.raises(reader.ReadError, match="REPLAY_TARGET_INVALID"):
            reader.metadata(db, request)


def test_nested_raw_execution_reference_is_in_the_recording_catalog(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R",
                "capture_seq": 1,
                "time_us": 1,
                "topic": "/order_set",
                "data": {"orders": [{"symbol": "S", "order_id": "O", "execution_id": "E"}]},
            }
        )
    )
    writer.finish()
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        assert [r["id"] for r in reader.metadata(db, {"operation": "manifest", "execution_id": "E"})["recordings"]] == [
            "R"
        ]


def test_order_dependencies_exclude_command_and_trade_streams_and_page_raw_versions(tmp_path, monkeypatch):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    seq = 0
    for symbol_index in range(8):
        symbol = f"S{symbol_index}"
        for topic, data in [
            (
                "/command",
                {
                    "command_id": symbol,
                    "operation": "place_order",
                    "symbol": symbol,
                    "order_id": "O",
                    "execution_id": "E",
                },
            ),
            ("/trade", {"identity_quality": "stable", "trade_key": symbol, "symbol": symbol, "order_id": "O"}),
            ("/order", {"symbol": symbol, "order_id": "O", "sync_generation": 1}),
        ]:
            seq += 1
            writer.append(
                json.dumps(
                    {
                        "schema_version": 2,
                        "recording_id": "R",
                        "capture_seq": seq,
                        "time_us": seq,
                        "topic": topic,
                        "data": data,
                    }
                )
            )
    for revision in range(130):
        seq += 1
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": seq,
                    "topic": "/order_set",
                    "data": {
                        "scope": {"kind": "account"},
                        "scope_complete": True,
                        "orders": [],
                        "sync_generation": 1,
                        "query_basis": {"recording_id": "R", "start_seq": 0, "request_revision": revision + 1},
                    },
                }
            )
        )
    writer.finish()
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        manifest = reader.metadata(db, {"operation": "manifest", "execution_id": "E"})
        view = {
            "execution_id": "E",
            "recording": "R",
            "through_seq": seq,
            "cutoff_seq": seq,
            "index_generation": manifest["index_generation"],
            "commit_id": manifest["recordings"][0]["commit_id"],
        }

        def get(operation, **fields):
            return reader.metadata(db, {**view, "operation": operation, **fields})

        objects = get("objects", kind="order")["subjects"]
        ids = [o["id"] for o in objects]
        streams = get("object-streams", objects=ids)["streams"]
        assert len(streams) == 9  # Eight order streams and the shared full-scope stream.
        topics = {
            r[0]
            for r in db.execute(
                "SELECT topic FROM streams WHERE recording='R' AND id IN (" + ",".join("?" for _ in streams) + ")",
                streams,
            )
        }
        assert topics == {"/order", "/order_set"}
        assert len(get("commands", objects=ids)["subjects"]) == 8
        assert len(get("queries", objects=ids)["sequences"]) == 128
        first = get("versions")
        assert len(first["versions"]) == 128
        rest = get("versions", after=first["next"])
        assert len(rest["versions"]) == 10 and rest["next"] is None


def test_block_page_seeks_both_sequence_bounds_without_scanning_future_history():
    from axile.server.execution_replay.raw_index import locations

    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript(Path("rust/storage/src/execution_replay_raw.sql").read_text())
        db.execute("INSERT INTO objects VALUES (1,'R','trade','T',2)")
        db.execute("INSERT INTO streams VALUES ('R',1,'/trade','S')")
        db.executemany(
            "INSERT INTO blocks(recording,id,stream,offset,length,raw_length,sha256,first_seq,last_seq) VALUES ('R',?,1,0,1,1,'hash',?,?)",
            [(i, i, i) for i in range(1, 20_001)],
        )
        db.executemany("INSERT INTO object_blocks VALUES ('R',1,1,?,?,?)", [(i, i, i) for i in range(1, 20_001)])
        steps = [0]

        def progress():
            steps[0] += 10
            return int(steps[0] > 2000)

        db.set_progress_handler(progress, 10)
        page = locations(
            db,
            {
                "recording": "R",
                "operation": "object-blocks",
                "through_seq": 20000,
                "cutoff_seq": 10001,
                "objects": [1],
                "stream": 1,
                "after_seq": 10000,
            },
        )
        assert [b["id"] for b in page["blocks"]] == [10001]
        assert page["next"] is None and steps[0] < 2000


def test_dependency_routes_are_exposed_with_the_same_frozen_context(source, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes import execution_replay as routes

    path, _, view = source

    async def account(_):
        return 7

    monkeypatch.setattr(routes, "_execution_account", account)
    monkeypatch.setattr(routes, "replay_root", lambda: path)
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")
    command = query(source, "objects", kind="command")["subjects"][0]
    with TestClient(app) as client:
        for operation, fields in [
            ("commands", {"order_ids": ["O"]}),
            ("queries", {"objects": [command["id"]]}),
            ("versions", {}),
        ]:
            response = client.post(f"/account/executions/E/replay/raw/{operation}", json={**view, **fields})
            assert response.status_code == 200, response.text
            assert response.json()["index_generation"] == view["index_generation"]


def test_fixed_identity_lookup_uses_primary_keys_in_a_large_dictionary():
    from axile.server.execution_replay.raw_index import locations

    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript(Path("rust/storage/src/execution_replay_raw.sql").read_text())
        db.executemany("INSERT INTO objects VALUES (?,'R','trade',?,1)", [(i, f"T{i}") for i in range(1, 20001)])
        steps = [0]

        def progress():
            steps[0] += 10
            return int(steps[0] > 2000)

        db.set_progress_handler(progress, 10)
        result = locations(
            db,
            {
                "recording": "R",
                "operation": "object-streams",
                "through_seq": 20001,
                "cutoff_seq": 20001,
                "objects": list(range(1, 9)),
            },
        )
        assert result["streams"] == [] and steps[0] < 2000
