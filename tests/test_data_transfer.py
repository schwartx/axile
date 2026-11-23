"""传送的累积、去重、回执和破损包边界。"""

import asyncio
import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zipfile import ZipFile

import pytest

from axile.server import data_transfer as transfer
from axile.server._native import _core


@pytest.fixture
def peers(tmp_path, monkeypatch):
    source = tmp_path / "source" / "axile.db"
    target = tmp_path / "target" / "axile.db"
    for p in (source, target):
        p.parent.mkdir()
        _core.migration_upgrade(str(p), "{}")
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    return source, target


def seed(path, ident=1):
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT OR IGNORE INTO account(id,name,market,trade_channel,brokerage,created_at_us,updated_at_us) VALUES(1,'same owner','crypto','binance','binance',0,0)"
        )
        c.execute("INSERT OR IGNORE INTO account_settings VALUES(1,'{}','{}','{\"is_started\":true}','{}','{}')")
        c.execute(
            "INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at_us) VALUES(?,1,?,1,?)",
            (ident, f"exec-{ident}", ident),
        )


def test_asset_snapshot_fact_links_import_and_reimport(peers):
    source, target = peers
    seed(source)
    for path, revision in ((source, "source"), (target, "receiver")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE axon_alembic_version(version_num TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO axon_alembic_version VALUES(?)", (revision,))
    request = json.dumps({"action": "import", "source_id": "peer", "snapshot": str(source)})
    for total in (50, 55):
        _core.asset_snapshot_write(
            str(source),
            json.dumps(
                {
                    "account_id": 1,
                    "created_at": datetime.now(UTC).isoformat(),
                    "account_assets": {"total_asset": total, "positions": []},
                }
            ),
        )
        for _ in range(2):
            _core.transfer_data(str(target), request)
        with sqlite3.connect(target) as connection:
            assert connection.execute(
                "SELECT a.total_asset FROM account_asset_snapshot s "
                "JOIN business_asset_snapshot a ON a.asset_id=s.asset_fact_id AND a.account_snapshot_id=s.id "
                "ORDER BY s.id DESC LIMIT 1"
            ).fetchone() == (total,)
            assert connection.execute("SELECT version_num FROM axon_alembic_version").fetchone() == ("receiver",)


def raw_manifest(database, execution):
    from axile.server.execution_replay.reader import index, metadata

    with index(database.parent / "replay", 1, lambda: None) as (c, _, _):
        return metadata(c, {"operation": "manifest", "execution_id": execution})


def raw_record(database, recording, stamp, **context):
    writer = _core.ExecutionReplayWriter(
        str(database.parent / "replay"), 1, recording, json.dumps({"schema_version": 2, **context})
    )
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": recording,
                "capture_seq": 1,
                "time_us": stamp,
                "topic": "/execution",
                "data": {"execution_id": "exec-1", "phase": "start"},
            }
        )
    )
    writer.finish()


def test_prepared_download_import_acknowledgement_and_cancel_routes(peers, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes.transfer import router

    source, target = peers
    seed(source)
    raw_record(source, "route-source", 1)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        prepared = client.post("/api/v1/transfer/packages", json={})
        assert prepared.status_code == 200
        batch = prepared.json()["batch"]
        response = client.get(f"/api/v1/transfer/packages/{batch}")
        assert response.status_code == 200 and len(response.content) == prepared.json()["size"]
        assert "attachment" in response.headers["content-disposition"]
        assert client.get("/api/v1/transfer/packages/invalid").status_code == 422
        monkeypatch.setattr(transfer, "database_path", lambda: str(target))
        imported = client.post("/api/v1/transfer/import", files={"file": ("data.axile", response.content)})
        assert imported.status_code == 200 and raw_manifest(target, "exec-1")["recordings"]
        monkeypatch.setattr(transfer, "database_path", lambda: str(source))
        acknowledged = client.post(
            "/api/v1/transfer/acknowledge", json={"receipt": imported.json(), "plan": {"replay_days": 1}}
        )
        assert acknowledged.status_code == 200 and acknowledged.json()["removed_recordings"] == 1
        raw_record(source, "retained", 2)
        batch = client.post("/api/v1/transfer/packages", json={}).json()["batch"]
        assert client.delete(f"/api/v1/transfer/packages/{batch}").json()["canceled"]
        assert client.get(f"/api/v1/transfer/packages/{batch}").status_code == 404
        assert (source.parent / "replay/account-1/raw-retained.mcap").exists()


def test_raw_transitive_content_prefixes_rebuild_locally_and_pin_referenced_history(peers, monkeypatch):
    source, target = peers
    seed(source)
    raw_record(source, "old", 1)
    with sqlite3.connect(source.parent / "replay/account-1/.replay-v2.sqlite") as c:
        commit = c.execute("SELECT commit_id FROM recordings WHERE id='old'").fetchone()[0]
    stamp = int(datetime.now(UTC).timestamp() * 1_000_000)
    raw_record(source, "current", stamp, content_refs=[{"recording": "old", "commit_id": commit}])
    package, manifest = transfer.export_package(transfer.TransferPlan(start=datetime.now(UTC) - timedelta(hours=1)))
    assert "replay/account-1/raw-old.mcap" in manifest["files"]
    assert not any(".sqlite" in n for n in manifest["files"] if n.startswith("replay/"))
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    generation = raw_manifest(target, "exec-1")["index_generation"]
    transfer.import_package(package)
    assert raw_manifest(target, "exec-1")["index_generation"] == generation
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    assert transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))["removed_recordings"] == 0
    assert (source.parent / "replay/account-1/raw-old.mcap").is_file()


def test_read_lease_and_active_writer_defer_cleanup_and_previous_ordering_does_not_pin(peers, monkeypatch):
    source, target = peers
    seed(source)
    raw_record(source, "old", 1)
    raw_record(source, "current", int(datetime.now(UTC).timestamp() * 1_000_000), previous_recording_id="old")
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    lease = _core.ReplayReadLease(str(source.parent / "replay"), 1, "old")
    assert transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))["removed_recordings"] == 0
    del lease
    live = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "live", '{"schema_version":2}')
    assert transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))["removed_recordings"] == 0
    live.finish()
    assert transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))["removed_recordings"] == 1
    assert not (source.parent / "replay/account-1/raw-old.mcap").exists()
    assert (source.parent / "replay/account-1/raw-current.mcap").exists()


def test_cancel_removes_only_pending_package_and_tombstone_cleanup_can_resume(peers, monkeypatch):
    source, target = peers
    seed(source)
    raw_record(source, "old", 1)
    package, manifest = transfer.export_package(transfer.TransferPlan())
    transfer._operation("cancel_package", batch=manifest["batch"])
    assert not package.exists() and (source.parent / "replay/account-1/raw-old.mcap").exists()
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    raw = source.parent / "replay/account-1/raw-old.mcap"
    raw.rename(raw.with_suffix(".mcap.deleted"))
    assert transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))["removed_recordings"] == 1
    assert not raw.exists() and not raw.with_suffix(".mcap.deleted").exists()


def test_rebuild_recovers_uncommitted_cleanup_and_finishes_committed_cleanup(peers):
    source, _ = peers
    seed(source)
    raw_record(source, "restore", 1)
    account = source.parent / "replay/account-1"
    raw = account / "raw-restore.mcap"
    original = raw.read_bytes()
    raw.rename(raw.with_suffix(".mcap.deleted"))
    _core.execution_replay_rebuild(str(source.parent / "replay"), 1)
    assert raw.read_bytes() == original and raw_manifest(source, "exec-1")["recordings"]
    # A completed index transaction is the positive deletion decision; no data is resurrected.
    with sqlite3.connect(account / ".replay-v2.sqlite") as db:
        db.execute("DELETE FROM recordings WHERE id='restore'")
    raw.rename(raw.with_suffix(".mcap.deleted"))
    _core.execution_replay_rebuild(str(source.parent / "replay"), 1)
    assert not raw.exists() and not raw.with_suffix(".mcap.deleted").exists()
    assert not raw_manifest(source, "exec-1")["recordings"]


def test_roundtrip_accumulates_and_preserves_source_cleanup(peers, monkeypatch):
    source, target = peers
    seed(source)
    seed(source, 2)
    package, manifest = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    transfer.import_package(package)
    with sqlite3.connect(target) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (2,)
        assert json.loads(c.execute("select schedule from account_settings").fetchone()[0])["is_started"] is False
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    transfer.acknowledge(receipt, transfer.TransferPlan(execution_days=1))
    with sqlite3.connect(source) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (1,)
    seed(source, 3)
    with sqlite3.connect(source) as c:
        c.execute("UPDATE account SET name='source update' WHERE id=1")
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    transfer.import_package(package)
    with sqlite3.connect(target) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (3,)
        assert c.execute("SELECT name FROM account WHERE id=1").fetchone() == ("source update",)


def test_corrupt_package_and_forged_receipt_never_clean_source(peers, monkeypatch, tmp_path):
    source, target = peers
    seed(source)
    package, manifest = transfer.export_package(transfer.TransferPlan())
    with pytest.raises(ValueError):
        transfer.acknowledge(
            {"batch": manifest["batch"], "source_id": manifest["source_id"], "manifest_sha256": "forged"},
            transfer.TransferPlan(execution_days=1),
        )
    corrupt = tmp_path / "corrupt.axile"
    with ZipFile(package) as src, ZipFile(corrupt, "w") as dest:
        for n in src.namelist():
            dest.writestr(n, b"bad" if n == "database.sqlite3" else src.read(n))
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    with pytest.raises(ValueError):
        transfer.import_package(corrupt)
    with sqlite3.connect(source) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (1,)


def test_range_and_path_validation(peers):
    source, _ = peers
    seed(source)
    _, m = transfer.export_package(transfer.TransferPlan(start=datetime.now(UTC) - timedelta(days=1)))
    assert m["executions"] == []


def test_other_origin_rejected_without_changes(peers, monkeypatch):
    source, target = peers
    seed(source)
    package, manifest = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    transfer.import_package(package)
    with sqlite3.connect(target) as c:
        c.execute("update transfer_origin set source='other'")
    with pytest.raises(_core.NativeError):
        transfer.import_package(package)
    with sqlite3.connect(target) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (1,)


def test_replay_roundtrip_and_acknowledged_cleanup(peers, monkeypatch):
    """只有完整接收后的已关闭录制可清理，回放能在接收端重建索引。"""
    source, target = peers
    seed(source)
    writer = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "closed", '{"schema_version":2}')
    for seq, stamp in enumerate((1, 100, 200), 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": "closed",
                    "capture_seq": seq,
                    "time_us": stamp,
                    "topic": "/execution",
                    "data": {"execution_id": "exec-1", "phase": "started"},
                }
            )
        )
    writer.finish()
    package, manifest = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    for name in manifest["files"]:
        if name.endswith(".mcap"):
            assert (target.parent / name).read_bytes() == (source.parent / name).read_bytes()
    assert raw_manifest(target, "exec-1")["recordings"]
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    result = transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))
    assert result["removed_recordings"] > 0
    assert not list((source.parent / "replay").rglob("*.mcap"))
    assert list((target.parent / "replay").rglob("*.mcap"))


def test_offline_import_and_confirmation_api(peers, monkeypatch):
    """离线导入返回回执，交回源端后才能清理。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes.transfer import router

    source, target = peers
    seed(source)
    package, _ = transfer.export_package(transfer.TransferPlan())
    data = package.read_bytes()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    with TestClient(app) as client:
        assert "receive_token" not in client.get("/api/v1/transfer/settings").json()
        assert client.get("/api/v1/transfer/identity").json()["source_id"]
        received = client.post("/api/v1/transfer/import", files={"file": ("data.axile", data)})
        assert received.status_code == 200
        receipt = received.json()
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    with TestClient(app) as client:
        assert client.post("/api/v1/transfer/acknowledge", json={"receipt": receipt, "plan": {}}).status_code == 200


def test_schedule_saved_and_restored(peers):
    from axile.server.core.scheduler import scheduler

    plan = transfer.TransferPlan(source_url="https://example.test:8443", every_hours=2)
    transfer.save_plan(plan)
    transfer.configure_schedule()
    assert scheduler.get_job("data-transfer").func is transfer.scheduled_pull
    assert scheduler.get_job("data-transfer-cleanup").func is transfer.scheduled_cleanup
    transfer.save_plan(transfer.TransferPlan())
    transfer.configure_schedule()
    assert scheduler.get_job("data-transfer") is None
    assert scheduler.get_job("data-transfer-cleanup").func is transfer.scheduled_cleanup


def test_network_failure_keeps_source_batch_without_cleanup(peers, monkeypatch):
    import asyncio

    import httpx

    seed(peers[0])
    package = transfer.prepare_package(transfer.TransferPlan(), "receiver")
    assert transfer.prepare_package(transfer.TransferPlan(), "receiver") == package
    monkeypatch.setattr(transfer, "database_path", lambda: str(peers[1]))

    class Unreachable:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kwargs: Unreachable())
    plan = transfer.TransferPlan(source_url="https://offline.test", execution_days=1)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(transfer.pull(plan))
    assert package.exists()
    assert not (transfer.root() / "pull-pending.json").exists()
    with sqlite3.connect(peers[0]) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (1,)


@pytest.mark.parametrize("failure", [None, "ack_before", "ack_after", "import", "oversize", "self"])
def test_pull_downloads_then_confirms_and_resumes_receipt(peers, monkeypatch, failure):
    import httpx

    source, target = peers
    seed(source)
    seed(source, 2)
    source_id = transfer.settings()["source_id"]
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receiver_id = transfer.settings()["source_id"]
    if failure == "import":
        seed(target, 9)
    if failure == "oversize":
        monkeypatch.setattr(transfer, "MAX_BYTES", 32)
    calls = []
    failed = False

    def remote(request):
        nonlocal failed
        assert request.url.host == "source.test"
        calls.append(request.url.path.rsplit("/", 1)[-1])
        with monkeypatch.context() as context:
            context.setattr(transfer, "database_path", lambda: str(source))
            if calls[-1] == "identity":
                return httpx.Response(200, json={"source_id": receiver_id if failure == "self" else source_id})
            body = json.loads(request.content)
            if calls[-1] == "export":
                package = transfer.prepare_package(transfer.TransferPlan(**body), request.url.params["requester"])
                return httpx.Response(200, content=package.read_bytes())
            assert calls[-1] == "acknowledge"
            if failure == "ack_before" and not failed:
                failed = True
                raise httpx.ConnectError("confirmation not delivered")
            result = transfer.acknowledge(body["receipt"], transfer.TransferPlan(**body["plan"]))
            if failure == "ack_after" and not failed:
                failed = True
                raise httpx.ConnectError("confirmation response lost")
            return httpx.Response(200, json=result)

    client = httpx.AsyncClient
    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(remote)))
    plan = transfer.TransferPlan(source_url="https://source.test", execution_days=1)
    pending = transfer.root() / "pull-pending.json"
    if failure in {"import", "oversize", "self"}:
        with pytest.raises(_core.NativeError if failure == "import" else ValueError):
            asyncio.run(transfer.pull(plan))
        assert calls == ["identity"] + ([] if failure == "self" else ["export"])
        assert not pending.exists()
        assert not list(transfer.root().glob("pull-*"))
        with sqlite3.connect(source) as connection:
            assert connection.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
        with sqlite3.connect(target) as connection:
            assert connection.execute("SELECT count(*) FROM executerecord").fetchone() == (
                (1 if failure == "import" else 0),
            )
        return
    if failure:
        with pytest.raises(httpx.ConnectError):
            asyncio.run(transfer.pull(plan))
        assert pending.exists()
        with sqlite3.connect(source) as connection:
            assert connection.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
        # 修改当前规则也先补交上一次的回执，沿用其来源和保留范围。
        plan = transfer.TransferPlan(source_url="https://different.test", execution_days=30)
    result = asyncio.run(transfer.pull(plan))
    assert result["batch"] and result["cleanup_pending"]
    assert calls == ["identity", "export", "acknowledge"] + (["acknowledge"] if failure else [])
    assert not pending.exists()
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
    with monkeypatch.context() as context:
        context.setattr(transfer, "database_path", lambda: str(source))
        asyncio.run(transfer.scheduled_cleanup())
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT count(*) FROM executerecord").fetchone() == (1,)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)


def test_live_recording_between_sealed_parts_is_not_removed(peers, monkeypatch):
    """会话仍开启时，即使当前没有 partial 文件也不得清理。"""
    source, target = peers
    writer = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "still-open", '{"schema_version":2}')
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "still-open",
                "capture_seq": 1,
                "time_us": 1,
                "topic": "/execution",
                "data": {"execution_id": "old", "phase": "started"},
            }
        )
    )
    writer.seal()
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    result = transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))
    assert result["removed_recordings"] == 0
    assert list((source.parent / "replay").rglob("*.mcap"))
    writer.finish()


@pytest.mark.parametrize("failure", ["code_directory", "future_range"])
def test_failed_exports_remove_only_their_own_staging(peers, monkeypatch, tmp_path, failure):
    from axile.server import app

    source, _ = peers
    seed(source)
    previous, _ = transfer.export_package(transfer.TransferPlan())
    plan = transfer.TransferPlan()
    if failure == "code_directory":
        invalid_directory = tmp_path / "not-a-directory"
        invalid_directory.write_text("invalid")
        monkeypatch.setattr(app, "_algorithm_directories", [invalid_directory])
    else:
        plan = transfer.TransferPlan(end=datetime.now(UTC) + timedelta(days=1))
    for _ in range(2):
        with pytest.raises(ValueError):
            transfer.export_package(plan)
    assert list((transfer.root() / "outbox").iterdir()) == [previous.parent]
    assert previous.is_file()
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (1,)


def test_imported_live_sessions_survive_receiver_recovery(peers, monkeypatch):
    source, target = peers
    seed(source)
    with sqlite3.connect(source) as c:
        c.execute(
            "INSERT INTO account_control_session(id,instance_id,account_id,execution_id,timezone,started_at_us,status) "
            "VALUES(1,'source-live',1,'exec-1','Asia/Shanghai',0,'active')"
        )
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    transfer.import_package(package)
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT status,finished_at_us FROM account_control_session").fetchone() == ("incomplete", None)
    assert _core.control_monitor_recover(str(target), '{"now_ms":1000}') == {"updated": 0}
    transfer.import_package(package)
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT status FROM account_control_session").fetchone() == ("active",)
        c.execute("UPDATE account_control_session SET status='closed',finished_at_us=100 WHERE id=1")
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    transfer.import_package(package)
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT status,finished_at_us FROM account_control_session").fetchone() == ("closed", 100)


@pytest.mark.parametrize("offset", ["+08:00", "+00:00"])
def test_early_morning_range_includes_replay(peers, monkeypatch, offset):
    source, target = peers
    seed(source)
    start = datetime.fromisoformat("2026-11-11T00:00:00+08:00")
    end = start + timedelta(hours=1)
    stamp = int((start + timedelta(minutes=30)).timestamp() * 1_000_000)
    with sqlite3.connect(source) as c:
        c.execute("UPDATE executerecord SET created_at_us=?", (stamp,))
    writer = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "night", '{"schema_version":2}')
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "night",
                "capture_seq": 1,
                "time_us": stamp,
                "topic": "/execution",
                "data": {"execution_id": "exec-1", "phase": "finished"},
            }
        )
    )
    writer.finish()
    tz = datetime.fromisoformat("2026-11-11T00:00:00" + offset).tzinfo
    package, manifest = transfer.export_package(
        transfer.TransferPlan(start=start.astimezone(tz), end=end.astimezone(tz))
    )
    assert manifest["executions"] == ["exec-1"]
    assert any(name.endswith(".mcap") for name in manifest["files"])
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    assert raw_manifest(target, "exec-1")["recordings"]
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    transfer.acknowledge(receipt, transfer.TransferPlan())
    result = transfer._operation(
        "prune_recordings", manifest=manifest, cutoff_us=int((start + timedelta(days=1)).timestamp() * 1_000_000)
    )
    assert result["removed_recordings"] > 0


@pytest.mark.parametrize("cleaned,valid", [(False, True), (True, True), (False, False)])
def test_source_finishes_saved_receipt_before_new_export(peers, monkeypatch, cleaned, valid):
    source, target = peers
    seed(source)
    seed(source, 2)
    plan = transfer.TransferPlan(source_url="https://peer.test", execution_days=1)
    package, manifest = transfer.export_package(plan)
    pending = transfer.root() / "pending.json"
    key = hashlib.sha256(
        json.dumps({"requester": "receiver", **transfer._plan_request(plan)}, sort_keys=True).encode()
    ).hexdigest()
    transfer.write_json(pending, {"key": key, "batch": manifest["batch"]})
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    if cleaned:
        transfer.acknowledge(receipt, plan)
    else:
        transfer.write_json(
            package.parent / "receipt.json", receipt if valid else {**receipt, "manifest_sha256": "invalid"}
        )

    # 模拟数据包已清理、pending 仍未移除时的中断。
    transfer.write_json(pending, {"key": key, "batch": manifest["batch"]})

    def reject_network(**kwargs):
        pytest.fail("已收到确认的批次不应再次访问接收端")

    monkeypatch.setattr(transfer.httpx, "AsyncClient", reject_network)
    if not valid:
        with pytest.raises(ValueError, match="接收确认与本机数据包不符"):
            transfer.prepare_package(plan, "receiver")
        assert pending.exists() and package.exists()
        with sqlite3.connect(source) as c:
            assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
        return
    result = transfer.prepare_package(plan, "receiver")
    assert result != package and result.exists()
    assert json.loads(pending.read_text())["batch"] != manifest["batch"]
    assert not package.exists()
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
    asyncio.run(transfer.scheduled_cleanup())
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (1,)
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)


def test_online_confirmation_releases_next_batch_before_busy_source_cleanup(peers, monkeypatch):
    source, target = peers
    seed(source)
    seed(source, 2)
    plan = transfer.TransferPlan(source_url="https://peer.test", execution_days=1, replay_days=1)
    raw_record(source, "old", 1)
    package = transfer.prepare_package(plan, "receiver")
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    with sqlite3.connect(source) as c:
        c.execute(
            "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,channel,algorithm,"
            "payload,created_at_us) VALUES('ongoing',1,'rebalance','manual','RUNNING','binance','SINGLE-MAKER','{}',3)"
        )

    assert transfer.acknowledge(receipt, plan) == {
        "removed_executions": 0,
        "removed_recordings": 0,
        "cleanup_pending": True,
    }
    assert not package.exists()
    assert transfer.prepare_package(plan, "receiver") != package
    assert transfer.acknowledge(receipt, transfer.TransferPlan())["cleanup_pending"]
    asyncio.run(transfer.scheduled_cleanup())
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
        c.execute("UPDATE execution_intent SET status='SUCCEEDED',finished_at_us=4")
    assert list((source.parent / "replay").rglob("*.mcap"))

    asyncio.run(transfer.scheduled_cleanup())
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT execution_id FROM executerecord").fetchall() == [("exec-2",)]
    assert not list((source.parent / "replay").rglob("*.mcap"))
    assert transfer.acknowledge(receipt, plan)["cleanup_pending"] is False
    asyncio.run(transfer.scheduled_cleanup())


@pytest.mark.parametrize("configured", [False, True])
def test_export_uses_runtime_algorithm_directories(peers, monkeypatch, tmp_path, configured):
    from axile.server import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(app.ALGORITHM_DIRECTORIES_ENV, raising=False)
    monkeypatch.setattr(app.settings, "algorithm_directories", [])
    code = tmp_path / ("custom_code" if configured else "user_algorithms")
    code.mkdir()
    (code / "custom.py").write_text("# exported algorithm\n")
    directories = [code] if configured else app._initial_algorithm_directories()
    monkeypatch.setattr(app, "_algorithm_directories", directories)
    package, manifest = transfer.export_package(transfer.TransferPlan())
    assert "code/0/custom.py" in manifest["files"]
    with ZipFile(package) as archive:
        assert archive.read("code/0/custom.py") == b"# exported algorithm\n"


@pytest.mark.parametrize("local_change", ["new_account", "edited_account", "asset_snapshot", "unique_artifact"])
def test_receiver_changes_are_not_overwritten(peers, monkeypatch, local_change):
    source, target = peers
    seed(source)
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    transfer.import_package(package)
    for path, owner in ((source, "source"), (target, "receiver")):
        with sqlite3.connect(path) as c:
            if local_change == "new_account":
                c.execute(
                    "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at_us,updated_at_us) "
                    "VALUES(2,?,'crypto','binance','binance',0,0)",
                    (owner,),
                )
            elif local_change == "edited_account":
                c.execute("UPDATE account SET name=? WHERE id=1", (owner,))
            elif local_change == "asset_snapshot":
                c.execute(
                    "INSERT INTO account_asset_snapshot(id,account_id,created_at_us,source) VALUES(1,1,0,?)", (owner,)
                )
            else:
                c.execute(
                    "INSERT INTO execution_artifact(id,execution_id,artifact_type,schema_version,content,created_at_us) "
                    "VALUES(?,'exec-1','standard_input',1,?,0)",
                    (2 if owner == "source" else 1, json.dumps({"owner": owner})),
                )
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    seed(source, 2)
    package, _ = transfer.export_package(transfer.TransferPlan())
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    with pytest.raises(_core.NativeError, match="UNIQUE" if local_change == "unique_artifact" else "独立新增或修改"):
        transfer.import_package(package)
    with sqlite3.connect(target) as c:
        if local_change == "unique_artifact":
            assert json.loads(c.execute("SELECT content FROM execution_artifact WHERE id=1").fetchone()[0]) == {
                "owner": "receiver"
            }
            assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (1,)
            return
        table, column, identity = (
            ("account_asset_snapshot", "source", 1)
            if local_change == "asset_snapshot"
            else ("account", "name", 2 if local_change == "new_account" else 1)
        )
        assert c.execute(f"SELECT {column} FROM {table} WHERE id=?", (identity,)).fetchone() == ("receiver",)
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (1,)


def test_code_package_roundtrip(peers, monkeypatch, tmp_path):
    from axile.server import app

    source, target = peers
    seed(source)
    seed(source, 2)
    code = tmp_path / "algorithms"
    code.mkdir()
    filename = "recording-策略😀-000001.py"
    (code / filename).write_text("# custom algorithm\n")
    monkeypatch.setattr(app, "_algorithm_directories", [code])
    package, manifest = transfer.export_package(transfer.TransferPlan())
    with ZipFile(package) as archive:
        for name, info in manifest["files"].items():
            data = archive.read(name)
            assert len(data) == info["size"]
            assert hashlib.sha256(data).hexdigest() == info["sha256"]
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(package)
    installed = transfer.root() / "algorithms" / manifest["source_id"] / manifest["batch"] / "0" / filename
    assert installed.read_bytes() == (code / filename).read_bytes()
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    assert (
        transfer.acknowledge(receipt, transfer.TransferPlan(execution_days=1, replay_days=1))["removed_executions"] == 1
    )
    assert (code / filename).is_file()


@pytest.mark.parametrize("corruption", ["traversal", "duplicate", "budget", "symlink"])
def test_invalid_archive_never_commits_or_issues_receipt(peers, monkeypatch, tmp_path, corruption):
    from zipfile import ZipInfo

    source, target = peers
    seed(source)
    package, manifest = transfer.export_package(transfer.TransferPlan())
    corrupt = tmp_path / "invalid.axile"
    with ZipFile(package) as src, ZipFile(corrupt, "w") as out:
        for name in manifest["files"]:
            out.writestr(name, src.read(name))
        if corruption == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                out.writestr("database.sqlite3", src.read("database.sqlite3"))
        elif corruption == "budget":
            manifest["files"]["database.sqlite3"]["size"] = transfer.MAX_BYTES + 1
        else:
            name = "../outside" if corruption == "traversal" else "replay/account-1/2026-01-01/link.mcap"
            manifest["files"][name] = {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
            info = ZipInfo(name)
            if corruption == "symlink":
                info.create_system = 3
                info.external_attr = 0o120777 << 16
            out.writestr(info, b"x")
        out.writestr("manifest.json", json.dumps(manifest))
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    with pytest.raises(ValueError):
        transfer.import_package(corrupt)
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT count(*) FROM account").fetchone() == (0,)
    assert not list(transfer.root().glob("receipt-*.json"))
    assert not (target.parent / "outside").exists()


def test_online_move_streams_original_and_requires_verified_receipt(peers, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes.transfer import router

    source, target = peers
    seed(source)
    seed(source, 2)
    raw_record(source, "old-recording", 1)
    raw_record(source, "next-recording", 2)
    raw_record(target, "existing-recording", 3)
    raw_record(source, "recent-recording", int(datetime.now(UTC).timestamp() * 1_000_000))
    original = source.parent / "replay/account-1/raw-old-recording.mcap"
    spec = {"execution_days": 1, "replay_days": 1}
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/transfer/move", json=spec)
        assert response.status_code == 200, response.text
        prepared = response.json()
        manifest = prepared["manifest"]
        assert set(manifest["files"]) == {"database.sqlite3", "replay/account-1/raw-old-recording.mcap"}
        assert manifest["executions"] == ["exec-1"]
        url = f"/transfer/move/{manifest['batch']}/replay/account-1/raw-old-recording.mcap"
        suffix = client.get(url, headers={"Range": "bytes=10-"})
        assert suffix.status_code == 206
        assert suffix.content == original.read_bytes()[10:]
        assert {p.name for p in (transfer.root() / "outbox" / manifest["batch"]).iterdir()} == {"manifest.json"}
        contents = client.get(url).content
    staging = target.parent / "direct"
    path = staging / "replay/account-1/raw-old-recording.mcap"
    path.parent.mkdir(parents=True)
    (staging / "database.sqlite3").write_bytes(base64.b64decode(prepared["database"]))
    target_replay = target.parent / "replay"
    existing_lease = _core.ReplayReadLease(str(target_replay), 1, "existing-recording")
    with sqlite3.connect(target_replay / "account-1/.replay-v2.sqlite") as index:
        generation = index.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        index.execute("BEGIN")
        assert index.execute("SELECT id FROM recordings").fetchall() == [("existing-recording",)]
        with monkeypatch.context() as context:
            context.setattr(transfer, "database_path", lambda: str(target))
            path.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
            with pytest.raises(ValueError, match="校验失败"):
                transfer._install_direct(staging, manifest)
            assert not path.exists()
            assert original.exists()
            path.write_bytes(contents)
            receipt = transfer._install_direct(staging, manifest)
        assert index.execute("SELECT id FROM recordings").fetchall() == [("existing-recording",)]
        index.rollback()
        assert index.execute("SELECT value FROM meta WHERE key='generation'").fetchone() == generation
        assert index.execute("SELECT id FROM recordings ORDER BY id").fetchall() == [
            ("existing-recording",),
            ("old-recording",),
        ]
    del existing_lease
    assert original.exists()
    assert raw_manifest(target, "exec-1")["recordings"]
    result = transfer.acknowledge(receipt, transfer.TransferPlan(**spec))
    assert result["cleanup_pending"] and original.exists()
    replay = source.parent / "replay"
    writer = _core.ExecutionReplayWriter(str(replay), 1, "live", '{"schema_version":2}')
    lease = _core.ReplayReadLease(str(replay), 1, "old-recording")
    result = transfer._operation("cleanup_acknowledged", batch=manifest["batch"], source_id=manifest["source_id"])
    assert result["cleanup_pending"] and original.exists()
    following = transfer._operation("prepare_direct", **transfer._plan_request(transfer.TransferPlan(**spec)))[
        "manifest"
    ]
    assert set(following["files"]) == {"database.sqlite3", "replay/account-1/raw-next-recording.mcap"}
    del lease
    result = transfer._operation("cleanup_acknowledged", batch=manifest["batch"], source_id=manifest["source_id"])
    assert result["removed_recordings"] == 1 and not result["cleanup_pending"]
    assert not original.exists()
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "live",
                "capture_seq": 1,
                "time_us": 2,
                "topic": "/execution",
                "data": {"execution_id": "exec-live", "phase": "start"},
            }
        )
    )
    writer.seal()
    with sqlite3.connect(replay / "account-1/.replay-v2.sqlite") as index:
        assert index.execute("SELECT state,seq FROM recordings WHERE id='live'").fetchone() == ("recording", 1)
    writer.finish()
    assert (source.parent / "replay/account-1/raw-recent-recording.mcap").exists()


def test_direct_without_retention_transfers_and_keeps_originals(peers, monkeypatch):
    source, target = peers
    seed(source)
    raw_record(source, "keep", 1)
    original = source.parent / "replay/account-1/raw-keep.mcap"
    plan = transfer.TransferPlan(start=datetime.fromtimestamp(0, UTC), end=datetime.now(UTC))
    request = transfer._plan_request(plan)
    prepared = transfer._operation("prepare_direct", **request)
    manifest = prepared["manifest"]
    assert manifest["executions"] == ["exec-1"]
    staging = target.parent / "direct"
    incoming = staging / "replay/account-1/raw-keep.mcap"
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(original.read_bytes())
    (staging / "database.sqlite3").write_bytes(base64.b64decode(prepared["database"]))
    with monkeypatch.context() as context:
        context.setattr(transfer, "database_path", lambda: str(target))
        receipt = transfer._install_direct(staging, manifest)
    transfer.acknowledge(receipt, plan)
    result = transfer._operation("cleanup_acknowledged", batch=manifest["batch"], source_id=manifest["source_id"])
    assert result == {"removed_executions": 0, "removed_recordings": 0, "cleanup_pending": False}
    assert original.exists()
    assert raw_manifest(target, "exec-1")["recordings"]
    assert transfer._operation("prepare_direct", **request)["manifest"] is None
    other = transfer._operation("prepare_direct", requester="a" * 32, **request)["manifest"]
    assert other["executions"] == ["exec-1"]


def test_direct_old_receipt_keeps_late_event_until_fresh_receipt(peers, monkeypatch):
    source, target = peers
    for ident in (1, 2):
        seed(source, ident)
    plan = transfer.TransferPlan(execution_days=1)
    request = transfer._plan_request(plan)
    staging = target.parent / "direct"
    staging.mkdir()

    def acknowledge_revision():
        prepared = transfer._operation("prepare_direct", **request)
        manifest = prepared["manifest"]
        (staging / "database.sqlite3").write_bytes(base64.b64decode(prepared["database"]))
        with monkeypatch.context() as context:
            context.setattr(transfer, "database_path", lambda: str(target))
            receipt = transfer._install_direct(staging, manifest)
        assert transfer.acknowledge(receipt, plan)["cleanup_pending"]
        return manifest

    def events(database):
        payload = _core.list_execution_events_json(str(database), '{"execution_id":"exec-1"}')
        return [event["event_uid"] for event in json.loads(payload)["data"]]

    original = acknowledge_revision()
    for ident, uid in ((1, "late-event"), (2, "sentinel")):
        event = {
            "execution_id": f"exec-{ident}",
            "account_id": 1,
            "channel": "binance",
            "algorithm": "test",
            "schema_version": 1,
            "event_uid": uid,
            "event_type": "order_terminal",
            "status": "SUCCESS",
            "reason_family": "EXCHANGE",
            "reason_code": "filled",
            "ts_local_created": "2026-11-22T00:00:00Z",
            "ts_persisted": "2026-11-22T00:00:00Z",
            "seq": 1,
            "details": {},
        }
        assert _core.append_execution_event(str(source), json.dumps(event))
    result = transfer._operation("cleanup_acknowledged", batch=original["batch"], source_id=original["source_id"])
    assert result["removed_executions"] == 0
    assert events(source) == ["late-event"]
    fresh = acknowledge_revision()
    assert fresh["execution_digests"] != original["execution_digests"]
    result = transfer._operation("cleanup_acknowledged", batch=fresh["batch"], source_id=fresh["source_id"])
    assert result["removed_executions"] == 1
    assert events(target) == ["late-event"]
    assert events(source) == []
