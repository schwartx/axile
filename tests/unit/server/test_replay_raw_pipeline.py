"""V2 raw storage, frozen cursor, generation and actual-send settlement."""

import asyncio
import contextlib
import gc
import json
import sqlite3
import struct
import weakref
import zlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server._native import _core
from axile.server.api.routes import execution_replay as routes
from axile.server.api.routes.execution_replay import _HeldResponse
from axile.server.execution_replay import reader


@pytest.fixture
def recording(tmp_path, monkeypatch):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    for seq in range(1, 101):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": 100 + seq // 2,
                    "topic": "/quote/S",
                    "data": {"symbol": "S", "execution_id": "E", "bids": [[seq, 1]], "book_valid": True},
                }
            )
        )
    writer.poll(2_000_000)
    writer.finish()
    clock = [0.0]
    owner = reader.Readers(lambda: clock[0])
    token = {**owner.open(7, "E", tmp_path), "root_seq": 1, "attempt": 1}
    clock[0] = 12
    monkeypatch.setattr(reader, "readers", owner)
    return tmp_path, owner, token


def test_unrecorded_execution_returns_an_empty_catalog_without_creating_an_index(tmp_path, monkeypatch):
    path = tmp_path / "not-created"
    clock = [0.0]
    owner = reader.Readers(lambda: clock[0])
    token = {**owner.open(7, "E", path), "root_seq": 1, "attempt": 1}
    clock[0] = 12
    monkeypatch.setattr(reader, "readers", owner)
    manifest, release, _ = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()
    assert manifest["recordings"] == [] and manifest["schema_version"] == 2
    assert list(path.glob("account-*")) == []


def test_native_context_budget_precedes_json_parsing_or_file_creation(tmp_path):
    with pytest.raises(_core.NativeError, match="REPLAY_BUDGET"):
        _core.ExecutionReplayWriter(str(tmp_path), 7, "R", "x" * (256 * 1024 + 1))
    assert list(tmp_path.iterdir()) == []


def test_display_blocks_keep_full_payload_and_frozen_source_context(recording):
    path, _, token = recording
    manifest, release, _ = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()
    source = manifest["recordings"][0]
    assert source["context"] == {"schema_version": 2}
    view = {
        **token,
        "operation": "display-blocks",
        "recording": "R",
        "through_seq": source["seq"],
        "commit_id": source["commit_id"],
        "index_generation": manifest["index_generation"],
        "stream": source["streams"][0]["id"],
    }
    page, release, _ = reader.read(path, 7, "E", view)
    release()
    assert page["next"] is None and len(page["blocks"]) == 1
    assert (page["recording_id"], page["through_seq"], page["commit_id"]) == ("R", 100, source["commit_id"])
    block, release, _ = reader.read(path, 7, "E", {**view, "operation": "block", "block": page["blocks"][0]["id"]})
    release()
    events = [json.loads(line) for line in zlib.decompress(block).splitlines()]
    assert [event["capture_seq"] for event in events] == list(range(1, 101))
    assert events[-1]["data"]["bids"] == [[100, 1]]
    for invalid in ({"through_seq": 99}, {"commit_id": "0" * 64}, {"stream": True}, {"after_seq": -1}):
        with pytest.raises(reader.ReadError):
            reader.read(path, 7, "E", {**view, **invalid})


def test_sqlite_interruption_preserves_the_budget_or_cancel_reason(recording):
    path, _, _ = recording

    def canceled():
        raise reader.ReadError("REPLAY_ROOT_INDEX_BUDGET", 413)

    with pytest.raises(reader.ReadError, match="REPLAY_ROOT_INDEX_BUDGET") as error:
        with reader.index(path, 7, canceled) as (db, _, _):
            db.one("layout")
    assert error.value.status == 413


def test_display_block_pages_do_not_include_blocks_after_the_frozen_commit(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    for seq in range(1, 34):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": seq,
                    "topic": "/quote/S",
                    "data": {"symbol": "S", "execution_id": "E"},
                }
            )
        )
        writer.seal()
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        manifest = reader.metadata(db, {"operation": "manifest", "execution_id": "E"})
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R",
                "capture_seq": 34,
                "time_us": 34,
                "topic": "/quote/S",
                "data": {"symbol": "S", "execution_id": "E"},
            }
        )
    )
    writer.finish()
    source = manifest["recordings"][0]
    view = {
        "operation": "display-blocks",
        "execution_id": "E",
        "recording": "R",
        "through_seq": source["seq"],
        "commit_id": source["commit_id"],
        "index_generation": manifest["index_generation"],
        "stream": source["streams"][0]["id"],
    }
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        first = reader.metadata(db, view)
        last = reader.metadata(db, {**view, "after_seq": first["next"]})
    assert len(first["blocks"]) == 32 and first["next"] == 33
    assert [row["first_seq"] for row in last["blocks"]] == [33] and last["next"] is None


def test_manifest_does_not_publish_a_stream_before_its_first_committed_observation(tmp_path):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R",
                "capture_seq": 1,
                "time_us": 1,
                "topic": "/execution",
                "data": {"execution_id": "E"},
            }
        )
    )
    writer.seal()
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R",
                "capture_seq": 2,
                "time_us": 2,
                "topic": "/quote/new",
                "data": {"symbol": "new"},
            }
        )
    )
    with reader.index(tmp_path, 7, lambda: None) as (db, _, _):
        manifest = reader.metadata(db, {"operation": "manifest", "execution_id": "E"})
        assert [s["topic"] for s in manifest["recordings"][0]["streams"]] == ["/execution"]
    writer.finish()


def test_single_payload_index_locations_and_exact_tie_cutoff(recording):
    path, owner, token = recording
    manifest, release, check = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()
    release()
    assert owner.sessions[token["session"]].root.active == 0
    view = {
        **token,
        "operation": "plan",
        "index_generation": manifest["index_generation"],
        "recording": "R",
        "through_seq": 100,
        "commit_id": manifest["recordings"][0]["commit_id"],
        "stream": 1,
        "time_us": 130,
        "capture_seq": 60,
    }
    plan, release, check = reader.read(path, 7, "E", view)
    release()
    assert plan["selected"]["seq"] == 60
    assert plan["evidence"][0]["seq"] == 60
    assert len(plan["evidence"]) == 32
    block, release, check = reader.read(
        path, 7, "E", {**view, "operation": "block", "block": plan["selected"]["block"]}
    )
    assert owner.sessions[token["session"]].root.active == 1
    release()
    events = [json.loads(line) for line in zlib.decompress(block).splitlines()]
    assert any(event["capture_seq"] == 60 for event in events)
    with sqlite3.connect(path / "account-7/.replay-v2.sqlite") as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "meta",
            "recordings",
            "executions",
            "streams",
            "blocks",
            "location_pages",
            "objects",
            "proof_members",
            "commits",
        } <= tables
        assert not tables & {"projections", "checkpoints", "market_facts", "observations"}
        assert db.execute("SELECT sum(last_seq-first_seq+1) FROM location_pages").fetchone()[0] == 100
        queries = db.execute(
            "EXPLAIN QUERY PLAN SELECT first_seq FROM location_pages WHERE recording='R' AND (first_time,first_seq)<=(130,60) ORDER BY first_time DESC,first_seq DESC LIMIT 1"
        ).fetchall()
        assert any("location_time" in r[3] for r in queries)
    owner.control(7, "E", {**token, "operation": "seal"})
    owner.control(7, "E", {**token, "operation": "ack"})
    with pytest.raises(reader.ReadError, match="REPLAY_ROOT_RETIRED"):
        reader.read(path, 7, "E", view)


def test_generation_checked_before_block_identity_and_canceled_request_before_index(recording, monkeypatch):
    path, owner, token = recording
    with pytest.raises(reader.ReadError, match="REPLAY_INDEX_GENERATION"):
        reader.read(path, 7, "E", {**token, "operation": "block", "index_generation": "old", "block": ["bad"]})
    owner.control(7, "E", {**token, "operation": "cancel"})

    def unexpected(*args):
        pytest.fail("late request reached SQLite")

    monkeypatch.setattr(reader, "index", unexpected)
    with pytest.raises(reader.ReadError, match="REPLAY_ATTEMPT_RETIRED"):
        reader.read(path, 7, "E", {**token, "operation": "plan"})


def test_bounded_binary_batch_checks_generation_and_accounts_payload_separately(recording):
    path, owner, token = recording
    manifest, release, _ = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()
    view = {
        **token,
        "operation": "blocks",
        "recording": "R",
        "through_seq": 100,
        "index_generation": manifest["index_generation"],
        "commit_id": manifest["recordings"][0]["commit_id"],
    }
    with pytest.raises(reader.ReadError, match="REPLAY_INDEX_GENERATION"):
        reader.read(path, 7, "E", {**view, "index_generation": "old", "blocks": [["invalid"]]})
    with pytest.raises(reader.ReadError, match="REPLAY_BLOCK_BATCH"):
        reader.read(path, 7, "E", {**view, "blocks": [1, 1]})
    payload, release, _ = reader.read(path, 7, "E", {**view, "blocks": [1]})
    assert payload[:4] == b"AR02" and struct.unpack_from("<I", payload, 4)[0] == 1
    identity, length = struct.unpack_from("<II", payload, 8)
    assert identity == 1 and length == len(payload) - 16
    assert len(zlib.decompress(payload[16:]).splitlines()) == 100
    assert owner.sessions[token["session"]].root.active == 1
    release()
    assert owner.sessions[token["session"]].raw_bytes == length


def test_send_owns_credit_until_stopped(recording):
    path, owner, token = recording
    result, release, check = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    task = owner.sessions[token["session"]].root

    async def exercise():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                entered.set()
                await finish.wait()

        response = asyncio.create_task(_HeldResponse(result, release, check)({"type": "http"}, None, send))
        await asyncio.wait_for(entered.wait(), 1)
        receipt = owner.control(7, "E", {**token, "operation": "seal"})
        assert receipt["state"] == "stopping" and task.active == 1
        finish.set()
        with pytest.raises(reader.ReadError, match="REPLAY_READ_CANCELED"):
            await response
        assert task.active == 0
        assert owner.control(7, "E", {**token, "operation": "status"})["state"] == "settled"

    asyncio.run(exercise())


def test_index_snapshot_is_closed_before_raw_file_io(recording, monkeypatch):
    path, _, token = recording
    manifest, release, _ = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()

    original_index, original_pread = reader.index, reader.os.pread
    closed = [False]

    @contextlib.contextmanager
    def tracked(*args):
        with original_index(*args) as current:
            yield current
        closed[0] = True

    def pread(*args):
        assert closed[0], "raw I/O must not pin the SQLite WAL snapshot"
        return original_pread(*args)

    monkeypatch.setattr(reader, "index", tracked)
    monkeypatch.setattr(reader.os, "pread", pread)
    _, release, _ = reader.read(
        path,
        7,
        "E",
        {
            **token,
            "operation": "block",
            "recording": "R",
            "through_seq": 100,
            "commit_id": manifest["recordings"][0]["commit_id"],
            "index_generation": manifest["index_generation"],
            "block": 1,
        },
    )
    release()


def test_compressed_raw_blocks_bypass_http_recompression(recording, monkeypatch):
    from axile.server.compression import CompressionMiddleware

    path, _, token = recording
    manifest, release, _ = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()

    async def account(_):
        return 7

    monkeypatch.setattr(routes, "_execution_account", account)
    monkeypatch.setattr(routes, "replay_root", lambda: path)
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")
    app.add_middleware(CompressionMiddleware, minimum_size=1)
    with TestClient(app) as client:
        response = client.post(
            "/account/executions/E/replay/raw/block",
            headers={"Accept-Encoding": "gzip"},
            json={
                **token,
                "recording": "R",
                "through_seq": 100,
                "index_generation": manifest["index_generation"],
                "commit_id": manifest["recordings"][0]["commit_id"],
                "block": 1,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "identity"
    assert len(zlib.decompress(response.content).splitlines()) == 100


def test_send_timeout_is_idle_progress_not_total_response_time(recording):
    path, owner, token = recording
    _, release, check = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    sent = []

    async def exercise():
        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                await asyncio.sleep(0.75)
                sent.append(len(message["body"]))

        await _HeldResponse(b"x" * (3 * 64 * 1024), release, check)({"type": "http"}, None, send)

    asyncio.run(exercise())
    assert sent == [64 * 1024] * 3
    assert owner.sessions[token["session"]].root.active == 0


@pytest.mark.parametrize("operation,field", [("plan", "stream"), ("block", "block")])
def test_numeric_ids_are_checked_after_generation_before_sqlite_binding(recording, operation, field):
    path, _, token = recording
    manifest, release, check = reader.read(path, 7, "E", {**token, "operation": "manifest"})
    release()
    request = {
        **token,
        "operation": operation,
        "index_generation": manifest["index_generation"],
        "recording": "R",
        "through_seq": 100,
        "commit_id": manifest["recordings"][0]["commit_id"],
        "time_us": 130,
        "capture_seq": 60,
        field: 2**100,
    }
    with pytest.raises(reader.ReadError) as error:
        reader.read(path, 7, "E", request)
    assert error.value.status == 422


def test_raw_index_cannot_follow_a_link_to_another_database(tmp_path):
    account = tmp_path / "account-7"
    account.mkdir()
    target = tmp_path / "unrelated.db"
    target.write_bytes(b"preserve")
    (account / ".replay-v2.sqlite").symlink_to(target)
    with pytest.raises(_core.NativeError, match="REPLAY_PATH"):
        _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    assert target.read_bytes() == b"preserve"


def test_new_reader_waits_for_old_owner_then_full_window(tmp_path):
    clock = [0.0]
    old = reader.Readers(lambda: clock[0])
    old.open(7, "E", tmp_path)
    clock[0] = 20
    replacement = reader.Readers(lambda: clock[0])
    with pytest.raises(reader.ReadError, match="REPLAY_READ_OWNER_UNAVAILABLE"):
        replacement.open(7, "E", tmp_path)
    del old
    token = {**replacement.open(7, "E", tmp_path), "root_seq": 1, "attempt": 1}
    assert token["recovery_wait_seconds"] == 11
    clock[0] = 30
    with pytest.raises(reader.ReadError, match="REPLAY_READ_WAIT"):
        replacement.admit(7, "E", token, 1)
    clock[0] = 31
    session, task = replacement.admit(7, "E", token, 1)
    replacement.release(session, task, 1, 1, 100)


def test_inflight_response_keeps_old_read_owner_alive(tmp_path, monkeypatch):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    writer.finish()
    clock = [0.0]
    old = reader.Readers(lambda: clock[0])
    token = {**old.open(7, "E", tmp_path), "root_seq": 1, "attempt": 1}
    clock[0] = 12
    monkeypatch.setattr(reader, "readers", old)
    _, release, check = reader.read(tmp_path, 7, "E", {**token, "operation": "manifest"})
    replacement = reader.Readers(lambda: clock[0])
    reader.readers = replacement
    reference = weakref.ref(old)
    del old
    gc.collect()
    assert reference() is not None
    with pytest.raises(reader.ReadError, match="REPLAY_READ_OWNER_UNAVAILABLE"):
        replacement.open(7, "E", tmp_path)
    release()
    del release, check
    gc.collect()
    assert reference() is None
    assert replacement.open(7, "E", tmp_path)["recovery_wait_seconds"] == 11


def test_route_uses_execution_owner_and_echoes_context(recording, monkeypatch):
    path, _, token = recording

    async def status(execution):
        return {"account_id": 7} if execution == "E" else None

    monkeypatch.setattr(routes, "get_execution_status", status)
    monkeypatch.setattr(routes, "replay_root", lambda: path)
    app = FastAPI()
    app.include_router(routes.router, prefix="/account")
    with TestClient(app) as client:
        response = client.post("/account/executions/E/replay/raw/manifest", json={**token, "account_id": 999})
        assert response.status_code == 200
        assert response.json()["account_id"] == 7
        assert response.json()["execution_id"] == "E"
        assert client.post("/account/executions/missing/replay/raw/session").status_code == 404


def test_frozen_view_does_not_scan_later_observations_at_same_timestamp(tmp_path, monkeypatch):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "R", '{"schema_version":2}')
    for seq in range(1, 5002):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "R",
                    "capture_seq": seq,
                    "time_us": 100,
                    "topic": "/quote/S",
                    "data": {"execution_id": "E", "symbol": "S"},
                }
            )
        )
        if seq == 1:
            writer.seal()
    writer.finish()
    clock = [0.0]
    owner = reader.Readers(lambda: clock[0])
    token = {**owner.open(7, "E", tmp_path), "root_seq": 1, "attempt": 1}
    monkeypatch.setattr(reader, "readers", owner)
    clock[0] = 12
    manifest, release, check = reader.read(tmp_path, 7, "E", {**token, "operation": "manifest"})
    release()
    with sqlite3.connect(tmp_path / "account-7/.replay-v2.sqlite") as db:
        frozen_commit = db.execute(
            "SELECT id FROM commits WHERE recording='R' AND seq=1 ORDER BY offset LIMIT 1"
        ).fetchone()[0]
    plan, release, check = reader.read(
        tmp_path,
        7,
        "E",
        {
            **token,
            "operation": "plan",
            "recording": "R",
            "through_seq": 1,
            "commit_id": frozen_commit,
            "stream": 1,
            "time_us": 100,
            "index_generation": manifest["index_generation"],
        },
    )
    release()
    assert plan["selected"]["seq"] == 1
    assert len(plan["evidence"]) == 1
    assert owner.sessions[token["session"]].root.index_steps < 5000
