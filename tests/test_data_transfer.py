"""传送的累积、去重、回执和破损包边界。"""

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
    writer = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "closed", '{"schema_version":1}')
    for seq, stamp in enumerate((1, 100, 200), 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
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
    received = _core.execution_replay_manifest(str(target.parent / "replay"), 1, "exec-1", None)
    assert json.loads(received)["recordings"]
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    result = transfer.acknowledge(receipt, transfer.TransferPlan(replay_days=1))
    assert result["removed_recordings"] > 0
    assert not list((source.parent / "replay").rglob("*.mcap"))
    assert list((target.parent / "replay").rglob("*.mcap"))


def test_receiver_token_and_offline_confirmation_api(peers, monkeypatch):
    """离线包和联网接收共用导入，错误授权不能产生回执。"""
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
    token = transfer.settings()["receive_token"]
    with TestClient(app) as client:
        bad = client.post("/api/v1/transfer/receive", files={"file": ("data.axile", data)})
        assert bad.status_code == 403
        received = client.post(
            "/api/v1/transfer/receive", headers={"x-transfer-token": token}, files={"file": ("data.axile", data)}
        )
        assert received.status_code == 200
        receipt = received.json()
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    with TestClient(app) as client:
        assert client.post("/api/v1/transfer/acknowledge", json=receipt).status_code == 200


def test_schedule_saved_and_restored(peers):
    from axile.server.core.scheduler import scheduler

    plan = transfer.TransferPlan(destination="https://example.test:8443", token="test", every_hours=2)
    transfer.save_plan(plan)
    transfer.configure_schedule()
    assert scheduler.get_job("data-transfer") is not None
    transfer.save_plan(transfer.TransferPlan())
    transfer.configure_schedule()
    assert scheduler.get_job("data-transfer") is None


def test_network_failure_reuses_pending_without_cleanup(peers, monkeypatch):
    import asyncio

    import httpx

    seed(peers[0])

    class Unreachable:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kwargs: Unreachable())
    plan = transfer.TransferPlan(destination="https://offline.test", token="token", execution_days=1)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(transfer.send(plan))
    assert len(list((transfer.root() / "outbox").glob("*/data.axile"))) == 1
    with sqlite3.connect(peers[0]) as c:
        assert c.execute("select count(*) from executerecord").fetchone() == (1,)


def test_live_recording_between_sealed_parts_is_not_removed(peers, monkeypatch):
    """会话仍开启时，即使当前没有 partial 文件也不得清理。"""
    source, target = peers
    writer = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "still-open", '{"schema_version":1}')
    writer.append(
        json.dumps(
            {
                "schema_version": 1,
                "recording_id": "still-open",
                "capture_seq": 1,
                "time_us": 1,
                "topic": "/execution",
                "data": {"execution_id": "old", "phase": "started"},
            }
        )
    )
    writer.finish()

    # 模拟两段之间：索引仍标记会话开启，当前没有 partial 文件。
    with sqlite3.connect(source.parent / "replay/account-1/.replay-index.sqlite") as c:
        c.execute("UPDATE recordings SET metadata=json_set(metadata,'$.state','recording')")
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
    writer = _core.ExecutionReplayWriter(str(source.parent / "replay"), 1, "night", '{"schema_version":1}')
    writer.append(
        json.dumps(
            {
                "schema_version": 1,
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
    transfer.import_package(package)
    received = json.loads(_core.execution_replay_manifest(str(target.parent / "replay"), 1, "exec-1", None))
    assert received["availability"] == "available"
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    result = transfer._operation(
        "prune_recordings", manifest=manifest, cutoff_us=int((start + timedelta(days=1)).timestamp() * 1_000_000)
    )
    assert result["removed_recordings"] > 0


@pytest.mark.parametrize("cleaned,valid", [(False, True), (True, True), (False, False)])
def test_send_resumes_saved_receipt_without_network(peers, monkeypatch, cleaned, valid):
    import asyncio

    source, target = peers
    seed(source)
    seed(source, 2)
    plan = transfer.TransferPlan(destination="https://peer.test", token="test", execution_days=1)
    package, manifest = transfer.export_package(plan)
    pending = transfer.root() / "pending.json"
    key = hashlib.sha256(json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
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

    # 兼容旧版本在清理数据包后、删除 pending 前中断的磁盘状态。
    transfer.write_json(pending, {"key": key, "batch": manifest["batch"]})

    def reject_network(**kwargs):
        pytest.fail("已收到确认的批次不应再次访问接收端")

    monkeypatch.setattr(transfer.httpx, "AsyncClient", reject_network)
    if not valid:
        with pytest.raises(ValueError, match="接收确认与本机数据包不符"):
            asyncio.run(transfer.send(plan))
        assert pending.exists() and package.exists()
        with sqlite3.connect(source) as c:
            assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)
        return
    result = asyncio.run(transfer.send(plan))
    assert result["batch"] == manifest["batch"]
    assert not pending.exists()
    assert not package.exists()
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (1,)
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT count(*) FROM executerecord").fetchone() == (2,)


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


def test_python_v1_zip_and_receipt_are_compatible(peers, monkeypatch, tmp_path):
    from axile.server import app

    source, target = peers
    seed(source)
    seed(source, 2)
    code = tmp_path / "algorithms"
    code.mkdir()
    (code / "recording-策略😀-000001.py").write_text("# legacy unicode\n")
    monkeypatch.setattr(app, "_algorithm_directories", [code])
    package, manifest = transfer.export_package(transfer.TransferPlan())
    legacy = tmp_path / "python-v1.axile"
    with ZipFile(package) as src, ZipFile(legacy, "w") as out:
        for name, info in manifest["files"].items():
            data = src.read(name)
            assert len(data) == info["size"]
            assert hashlib.sha256(data).hexdigest() == info["sha256"]
            out.writestr(name, data)
        out.writestr("manifest.json", json.dumps(manifest))
    monkeypatch.setattr(transfer, "database_path", lambda: str(target))
    receipt = transfer.import_package(legacy)
    assert receipt["manifest_sha256"] == hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(transfer, "database_path", lambda: str(source))
    assert (
        transfer.acknowledge(receipt, transfer.TransferPlan(execution_days=1, replay_days=1))["removed_executions"] == 1
    )


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
