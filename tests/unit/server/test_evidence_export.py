"""真实 SQLite 与 HTTP 下载回归：阶段证据、跨日选择、脱敏和取消清理。"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
    ExecutionTaskStatus,
)
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlDecision,
    AccountControlScopeType,
)
from axile.server.api.deps import get_db
from axile.server.api.routes import account_evidence
from axile.server.core.db import register_sqlite_concurrency_pragmas
from axile.server.db.models import Account, AccountControlCounterDelta, AccountControlEvent, ExecuteRecord
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.db.models.execution import ExecutionArtifact, ExecutionEvent
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.evidence import archive as evidence_archive
from axile.server.evidence import collect
from axile.server.evidence.archive import build_archive
from axile.server.evidence.models import (
    EvidenceError,
    EvidenceLimits,
    EvidenceProfile,
    EvidenceSnapshot,
    EvidenceWindow,
    ExportBudget,
)
from axile.server.evidence.privacy import EvidenceRedactor
from axile.server.execution import backend as execution_backend
from axile.server.execution.worker_backend import worker_audit


@pytest.fixture
def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}", poolclass=NullPool)
    register_sqlite_concurrency_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with factory() as session:
            session.add(_account(1))
            session.add(_account(2))
            await session.commit()

    asyncio.run(create())
    yield factory
    asyncio.run(engine.dispose())


def _account(account_id=1):
    return Account(
        id=account_id,
        name=f"private-account-{account_id}",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_control_preset="ctp",
        account_config={"password": "live-credential"},
        is_started=False,
        cron_expr="",
        remark=None,
        brokerage="ctp",
        weight_precision=0.01,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        trade_rules=None,
        forbidden_symbols=None,
        risk_symbols=None,
        feishu_key=None,
    )


def _intent(execution_id="exec-1", **overrides):
    return ExecutionIntent(
        **{
            "account_id": 1,
            "execution_id": execution_id,
            "kind": ExecutionKind.REBALANCE,
            "trigger_source": "manual",
            "status": ExecutionTaskStatus.SUCCEEDED,
            "channel": TradeChannel.CTP,
            "algorithm": "SINGLE-MAKER",
            "created_at": "2026-10-15T09:00:00+08:00",
            "started_at": "2026-10-15T09:00:01+08:00",
            "finished_at": "2026-10-15T09:00:10+08:00",
            **overrides,
        }
    )


def _event(execution_id="exec-1", seq=1, **overrides):
    return ExecutionEvent(
        **{
            "account_id": 1,
            "execution_id": execution_id,
            "channel": TradeChannel.CTP,
            "algorithm": "SINGLE-MAKER",
            "event_uid": f"{execution_id}-{seq}",
            "event_type": ExecutionEventType.EXECUTION_STARTED,
            "status": ExecutionEventStatus.INFO,
            "reason_family": ExecutionReasonFamily.SYSTEM,
            "reason_code": "COMMON.EXECUTION_STARTED",
            "seq": seq,
            "ts_local_created": "2026-10-15T09:00:00+08:00",
            **overrides,
        }
    )


def _control(execution_id="exec-1", seq=1, **overrides):
    return AccountControlEvent(
        **{
            "account_id": 1,
            "execution_id": execution_id,
            "control_date": "2026-10-15",
            "channel": TradeChannel.CTP,
            "seq": seq,
            "event_uid": f"control-{execution_id}-{seq}",
            "operation": "cancel_order",
            "decision": AccountControlDecision.ALLOWED,
            "counted": True,
            "outcome": "submitted",
            "occurred_at_ms": 1792026000000,
            "created_at": "2026-10-15T09:00:00+08:00",
            **overrides,
        }
    )


def _record(execution_id="exec-1", **overrides):
    return ExecuteRecord(
        **{
            "account_id": 1,
            "execution_id": execution_id,
            "is_success": 1,
            "created_at": "2026-10-15T09:00:10+08:00",
            "raw_input": {"account_config": {"password": "buried-password"}, "account_name": "private-account-1"},
            "raw_result": {
                "status": "PARTIAL",
                "outcome": "not_reached",
                "symbol_results": {
                    "rb2610": {
                        "status": "PARTIAL",
                        "target_volume": 2,
                        "orders": [
                            {
                                "order_id": "20261015-1-2-3",
                                "symbol": "rb2610",
                                "direction": "买入",
                                "order_type": "LIMIT",
                                "price": 3500,
                                "volume": 2,
                                "filled_volume": 1,
                                "status": "部分成交",
                                "extra": {
                                    "FrontID": 1,
                                    "SessionID": 2,
                                    "OrderRef": "3",
                                    "OrderSysID": "native-4",
                                    "InvestorID": "investor-private",
                                    "api_secret": "nested-secret",
                                },
                            }
                        ],
                        "trades": [
                            {
                                "trade_id": "trade-1",
                                "order_id": "20261015-1-2-3",
                                "symbol": "rb2610",
                                "trade_volume": 1,
                                "trade_price": 3500,
                                "trade_value": 35000,
                                "trade_time": "2026-10-15T09:00:04+08:00",
                                "extra": {},
                            }
                        ],
                    }
                },
            },
            **overrides,
        }
    )


def _seed(factory, *rows):
    async def seed():
        async with factory() as session:
            session.add_all(rows)
            await session.commit()

    asyncio.run(seed())


def _client(factory, *, raise_server_exceptions=True):
    app = FastAPI()
    app.include_router(account_evidence.router, prefix="/account")

    async def dependency():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = dependency
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _zip(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    return ZipFile(io.BytesIO(response.content))


def _manifest(archive):
    return json.loads(archive.read("manifest.json"))


def _csv(archive, path):
    return list(csv.DictReader(io.StringIO(archive.read(path).decode("utf-8-sig"))))


def test_single_execution_retains_partial_native_evidence_and_redacts(database):
    recon = {
        "account": {"source_after": "real"},
        "symbols": [{"symbol": "rb2610", "before": 0, "target": 2, "after": 1, "filled": 1, "reached": False}],
    }
    _seed(
        database,
        _intent(),
        _record(),
        _event(),
        _control(),
        ExecutionArtifact(
            execution_id="exec-1",
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            content={"reconciliation": recon},
        ),
    )
    client = _client(database)
    archive = _zip(client.get("/account/executions/exec-1/evidence"))
    manifest = _manifest(archive)
    report = manifest["executions"][0]
    assert report["task_status"] == "SUCCEEDED"
    assert report["business_outcome"] == "not_reached"
    assert report["known_pending_order_ids"] == ["20261015-1-2-3"]
    assert report["coverage"]["coverage"] == "incomplete"
    assert report["execution_environment"] is None
    assert _csv(archive, "executions/exec-1/reconciliation.csv")[0]["after"] == "1"
    orders = _csv(archive, "executions/exec-1/orders.csv")
    assert json.loads(orders[0]["extra"])["OrderSysID"] == "native-4"
    assert _csv(archive, "executions/exec-1/trades.csv")[0]["trade_id"] == "trade-1"
    data = b"\n".join(archive.read(name) for name in archive.namelist()).decode()
    for secret in ("buried-password", "nested-secret", "live-credential", "private-account-1", "investor-private"):
        assert secret not in data
    for entry in manifest["files"]:
        raw = archive.read(entry["path"])
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert len(raw) == entry["bytes"]
    assert manifest["account"]["id"] == "account-A"
    assert manifest["redaction"]["counts"]["credential_or_code"] >= 2
    assert "未到位" in archive.read("executions/exec-1/summary.md").decode()


def test_internal_profile_keeps_identity_but_always_excludes_credentials(database):
    record = _record()
    record.raw_result["error"] = (
        "password=plaintext https://example.invalid/?token=url-secret /home/user/private/key.txt"
    )
    _seed(database, _intent(), record)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence?profile=internal"))
    contents = b"\n".join(archive.read(name) for name in archive.namelist()).decode()
    assert "private-account-1" in contents
    assert "investor-private" in contents
    for secret in ("plaintext", "url-secret", "/home/user/private", "buried-password", "nested-secret"):
        assert secret not in contents


def test_queued_execution_has_no_fabricated_fills_or_result(database):
    _seed(database, _intent(status=ExecutionTaskStatus.QUEUED, started_at=None, finished_at=None))
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    report = _manifest(archive)["executions"][0]
    assert report["task_status"] == "QUEUED"
    assert report["business_outcome"] == "unknown"
    assert report["coverage"]["coverage"] == "provisional"
    assert json.loads(archive.read("executions/exec-1/record.json")) is None
    assert _csv(archive, "executions/exec-1/reconciliation.csv") == []
    assert "不能从任务成功推断成交或到位" in archive.read("executions/exec-1/summary.md").decode()


def test_date_export_includes_cross_day_and_legacy_records_but_statistics_stay_in_window(database):
    midnight_ms = int(datetime(2026, 10, 15, 16, tzinfo=UTC).timestamp() * 1000)
    _seed(
        database,
        _intent("cross", created_at="2026-10-15T23:59:00+08:00", finished_at="2026-10-16T00:01:00+08:00"),
        _control("cross", seq=1, occurred_at_ms=midnight_ms - 1000),
        _control(
            "cross", seq=2, operation="cancel_order_ctp", occurred_at_ms=midnight_ms + 1000, control_date="2026-10-16"
        ),
        _intent(
            "queued-old",
            created_at="2026-10-14T10:00:00+08:00",
            status=ExecutionTaskStatus.QUEUED,
            started_at=None,
            finished_at=None,
        ),
        _intent("outside", created_at="2026-10-14T10:00:00+08:00", finished_at="2026-10-14T10:00:01+08:00"),
        _record(None, created_at="2026-10-15T12:00:00"),
        _intent("other-account", account_id=2),
    )
    archive = _zip(
        _client(database).get(
            "/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15&legacy_timezone=Asia/Shanghai"
        )
    )
    manifest = _manifest(archive)
    assert {r["key"] for r in manifest["executions"]} == {"cross", "queued-old", "legacy-record-1"}
    assert manifest["selection"]["end_exclusive"] == "2026-10-15T16:00:00+00:00"
    assert len(archive.read("executions/cross/control-events.jsonl").splitlines()) == 2
    assert [r["operation"] for r in _csv(archive, "control-summary.csv")] == ["cancel_order"]
    legacy = next(r for r in manifest["executions"] if r["execution_id"] is None)
    assert legacy["coverage"]["coverage"] == "incomplete"
    day_two = _zip(_client(database).get("/account/1/evidence?start_date=2026-10-16&end_date=2026-10-16"))
    assert "cross" in {r["key"] for r in _manifest(day_two)["executions"]}
    assert [r["operation"] for r in _csv(day_two, "control-summary.csv")] == ["cancel_order_ctp"]


def test_all_pages_read_in_primary_key_order_including_seq_zero(database):
    _seed(database, _intent(), *[_event(seq=seq) for seq in range(1, 506)], _event(seq=0))
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    rows = [json.loads(line) for line in archive.read("executions/exec-1/events.jsonl").splitlines()]
    assert len(rows) == 506
    assert rows[-1]["seq"] == 0
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


@pytest.mark.parametrize(
    "query",
    [
        "start_date=2026-10-16&end_date=2026-10-15",
        "start_date=2026-10-01&end_date=2026-11-01",
        "start_date=bad&end_date=2026-10-15",
        "start_date=2026-10-15&end_date=2026-10-15&timezone=Unknown/Zone",
        "start_date=2026-10-15&end_date=2026-10-15&legacy_timezone=Unknown/Zone",
        "start_date=2026-10-15&end_date=2026-10-15&profile=raw",
    ],
)
def test_invalid_range_timezone_and_profile(database, query):
    assert _client(database).get("/account/1/evidence?" + query).status_code == 422


def test_not_found_and_cross_account_conflicts(database):
    client = _client(database)
    assert client.get("/account/executions/missing/evidence").status_code == 404
    assert client.get("/account/999/evidence?start_date=2026-10-15&end_date=2026-10-15").status_code == 404
    _seed(database, _intent("conflict"), _event("conflict", account_id=2))
    assert client.get("/account/executions/conflict/evidence").status_code == 409
    assert client.get("/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15").status_code == 409


def test_empty_window_is_valid_and_has_no_fake_rows(database):
    archive = _zip(_client(database).get("/account/1/evidence?start_date=2020-01-01&end_date=2020-01-01"))
    assert _manifest(archive)["executions"] == []
    assert _csv(archive, "executions.csv") == []


def test_limits_fail_whole_export_and_cleanup_files(database, monkeypatch, tmp_path):
    _seed(database, _intent(), _record(), _event())
    factory = evidence_archive.TemporaryDirectory
    created = []

    def temporary(**kwargs):
        directory = factory(dir=tmp_path, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(evidence_archive, "TemporaryDirectory", temporary)
    monkeypatch.setattr(
        account_evidence, "ExportBudget", lambda: ExportBudget(limits=replace(EvidenceLimits(), bytes=8000))
    )
    response = _client(database).get("/account/executions/exec-1/evidence")
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "EVIDENCE_TOO_LARGE"
    assert all(not path.exists() for path in created)


def test_normal_download_removes_temp_archive(database, monkeypatch, tmp_path):
    _seed(database, _intent())
    factory = evidence_archive.TemporaryDirectory
    created = []

    def temporary(**kwargs):
        directory = factory(dir=tmp_path, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(evidence_archive, "TemporaryDirectory", temporary)
    _zip(_client(database).get("/account/executions/exec-1/evidence"))
    assert created and all(not path.exists() for path in created)


def test_explicit_snapshot_does_not_mix_concurrent_commits(database, monkeypatch):
    _seed(database, _intent(), _event())
    original = collect._load_sources

    async def concurrent_write(session, snapshot, budget):
        async with database() as writer:
            writer.add(_event(seq=2))
            await writer.commit()
        await original(session, snapshot, budget)

    monkeypatch.setattr(collect, "_load_sources", concurrent_write)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    assert len(archive.read("executions/exec-1/events.jsonl").splitlines()) == 1

    async def count():
        async with database() as session:
            return len((await session.execute(select(ExecutionEvent))).scalars().all())

    assert asyncio.run(count()) == 2


def test_logs_capture_bounds_owner_and_shareable_projection(database, tmp_path, monkeypatch):
    _seed(database, _intent())
    directory = tmp_path / "logs"
    directory.mkdir()
    lines = [
        {
            "timestamp": "2026-10-15T09:00:00+08:00",
            "context": {"account_id": 1, "execution_id": "exec-1"},
            "message": "password=log-secret",
        },
        {"context": {"account_id": 2, "execution_id": "exec-1"}, "message": "wrong-account"},
        {"context": {"account_id": 2, "execution_id": "other"}, "message": "unrelated"},
    ]
    (directory / "axile.jsonl").write_text("".join(json.dumps(row) + "\n" for row in lines) + "{broken}\n")
    monkeypatch.setattr(account_evidence.settings, "app_log_dir", directory)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence?include_logs=true"))
    rows = [json.loads(line) for line in archive.read("logs.jsonl").splitlines()]
    assert len(rows) == 1
    assert "message" not in rows[0]
    assert "log-secret" not in archive.read("logs.jsonl").decode()
    assert "log_owner_conflict" in _manifest(archive)["logs"]["issues"]
    assert "log_invalid_json" in _manifest(archive)["logs"]["issues"]


def test_archive_failure_and_asgi_disconnect_cleanup(tmp_path):
    snapshot = EvidenceSnapshot({"id": 1, "name": "secret-name", "channel": "ctp"}, {})
    archive = build_archive(snapshot, EvidenceProfile.SHAREABLE, ExportBudget())
    path = archive.path
    response = account_evidence.EvidenceFileResponse(archive)

    async def fail_send(message):
        raise RuntimeError("disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "headers": [], "asgi": {"version": "3.0", "spec_version": "2.4"}},
                receive,
                fail_send,
            )
        )
    assert not path.exists()
    budget = ExportBudget()
    budget.cancelled.set()
    with pytest.raises(EvidenceError, match="取消"):
        build_archive(snapshot, EvidenceProfile.SHAREABLE, budget)


def test_execution_environment_is_frozen_before_trade_and_clear(monkeypatch):
    calls = []
    monkeypatch.setattr(worker_audit, "append_execution_event_sync", lambda **kwargs: None)
    monkeypatch.setattr(worker_audit, "append_execution_artifact_sync", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(worker_audit, "execution_environment", lambda *args: {"axile_version": "at-execution"})
    worker_audit._append_empty_positions_pre_execute_audit(
        account=_account(), execution_id="frozen", algorithm_name="SINGLE-MAKER", executor=object()
    )
    environment_rows = [c for c in calls if c["artifact_type"] == ExecutionArtifactType.ENVIRONMENT_SNAPSHOT]
    assert environment_rows[0]["content"]["axile_version"] == "at-execution"


def test_window_handles_dst_and_offset_aware_timestamps():
    window = EvidenceWindow(date(2026, 11, 1), date(2026, 11, 1), "America/New_York")
    assert (window.end - window.start).total_seconds() == 25 * 3600
    assert window.contains("2026-11-01T01:30:00-04:00") is True
    assert window.contains("2026-11-02T00:00:00-05:00") is False


def test_control_only_execution_and_local_vs_broker_rejection(database):
    _seed(
        database,
        _control(
            "broker-only",
            outcome="broker_rejected",
            metadata_={"return_code": 0, "error_id": 31, "error_stage": "async_callback"},
        ),
        _control("blocked-only", decision=AccountControlDecision.BLOCKED, counted=False, outcome="policy_blocked"),
    )
    archive = _zip(_client(database).get("/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15"))
    reports = {r["key"]: r for r in _manifest(archive)["executions"]}
    assert reports["broker-only"]["problems"][0]["return_code"] == 0
    assert reports["broker-only"]["problems"][0]["error_id"] == 31
    assert reports["blocked-only"]["problems"][0]["reason_code"] == "policy_blocked"
    assert reports["broker-only"]["task_status"] == "UNKNOWN"
    assert "async_callback" in archive.read("executions/broker-only/summary.md").decode()


def test_session_and_counter_only_executions_are_not_lost(database):
    start = int(datetime(2026, 10, 15, 1, tzinfo=UTC).timestamp() * 1000)
    _seed(
        database,
        AccountControlSession(
            instance_id="instance-1",
            execution_id="session-only",
            account_id=1,
            timezone="Asia/Shanghai",
            started_at_ms=start,
            finished_at_ms=start + 1000,
            status="incomplete",
        ),
        AccountControlCounterDelta(
            account_id=1,
            execution_id="counter-only",
            control_date="2026-10-15",
            bucket_type=AccountControlBucketType.DAY,
            bucket_start="2026-10-15T00:00:00",
            scope_type=AccountControlScopeType.ACCOUNT,
            operation="place_order",
            delta_count=2,
            delta_uid="counter-1",
        ),
    )
    archive = _zip(
        _client(database).get(
            "/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15&legacy_timezone=Asia/Shanghai"
        )
    )
    reports = {r["key"]: r for r in _manifest(archive)["executions"]}
    assert set(reports) == {"session-only", "counter-only"}
    assert "control_session_not_cleanly_finished" in reports["session-only"]["coverage"]["issues"]
    assert "control_counter_activity_time_unknown" in reports["counter-only"]["coverage"]["issues"]
    assert _csv(archive, "control-summary.csv") == []
    assert json.loads(archive.read("executions/counter-only/counter-deltas.jsonl"))["delta_count"] == 2
    assert _client(database).get("/account/executions/counter-only/evidence").status_code == 200


def test_repeated_exports_are_read_only_and_late_results_remain_distinct(database):
    _seed(database, _intent(status=ExecutionTaskStatus.RUNNING, finished_at=None))
    client = _client(database)
    first = _manifest(_zip(client.get("/account/executions/exec-1/evidence")))
    second = _manifest(_zip(client.get("/account/executions/exec-1/evidence")))
    assert first["executions"] == second["executions"]
    assert first["export_id"] != second["export_id"]
    _seed(database, _record())
    third = _manifest(_zip(client.get("/account/executions/exec-1/evidence")))
    assert third["executions"][0]["task_status"] == "RUNNING"
    assert third["executions"][0]["business_outcome"] == "not_reached"
    assert first["executions"][0]["business_outcome"] == "unknown"

    async def count():
        async with database() as session:
            return await session.scalar(select(func.count()).select_from(ExecutionIntent))

    assert asyncio.run(count()) == 1


def test_arbitrary_legacy_identifiers_cannot_escape_zip_paths(database):
    malicious = "../outside"
    _seed(database, _record(malicious))
    archive = _zip(_client(database).get("/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15"))
    assert all(".." not in Path(path).parts and not path.startswith("/") for path in archive.namelist())
    report = _manifest(archive)["executions"][0]
    assert report["execution_id"] == malicious
    assert report["path"].startswith("executions/execution-")


def test_csv_formulas_escaped_unknown_fields_excluded_and_nulls_remain_null(database):
    record = _record()
    record.raw_result["symbol_results"]["rb2610"]["orders"][0]["order_id"] = "=HYPERLINK(1)"
    record.raw_result["symbol_results"]["rb2610"]["plugin_private"] = {"opaque": "not-for-sharing"}
    record.raw_result["account_assets"] = {"source": "unavailable", "positions": None}
    _seed(database, _intent(), record)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    assert _csv(archive, "executions/exec-1/orders.csv")[0]["order_id"] == "'=HYPERLINK(1)"
    raw = archive.read("executions/exec-1/record.json").decode()
    assert "not-for-sharing" not in raw
    assert json.loads(raw)["raw_result"]["account_assets"]["positions"] is None
    assert _csv(archive, "executions/exec-1/reconciliation.csv") == []


def test_export_task_cancellation_cleans_finished_thread_archive(database, monkeypatch):
    _seed(database, _intent())
    original = account_evidence.build_archive
    paths = []

    async def driver():
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()

        def finish_after_cancel(snapshot, profile, budget, **kwargs):
            archive = original(snapshot, profile, budget, **kwargs)
            paths.append(archive.path)
            loop.call_soon_threadsafe(ready.set)
            assert budget.cancelled.wait(timeout=5)
            return archive

        monkeypatch.setattr(account_evidence, "build_archive", finish_after_cancel)
        async with database() as session:
            task = asyncio.create_task(
                account_evidence._export(session, EvidenceProfile.SHAREABLE, False, execution_id="exec-1")
            )
            await asyncio.wait_for(ready.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(driver())
    assert paths and all(not path.exists() for path in paths)


def test_generation_timeout_returns_explicit_failure(database, monkeypatch):
    _seed(database, _intent())

    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(account_evidence, "collect_evidence", never_finishes)
    monkeypatch.setattr(
        account_evidence, "ExportBudget", lambda: ExportBudget(limits=replace(EvidenceLimits(), seconds=0.01))
    )
    response = _client(database).get("/account/executions/exec-1/evidence")
    assert response.status_code == 408
    assert response.json()["detail"]["code"] == "EVIDENCE_TIMEOUT"


def test_core_read_failure_does_not_return_success_zip(database, monkeypatch):
    _seed(database, _intent())

    async def fail(*args):
        raise RuntimeError("database read failed")

    monkeypatch.setattr(collect, "_load_sources", fail)
    client = _client(database, raise_server_exceptions=False)
    assert client.get("/account/executions/exec-1/evidence").status_code == 500


def test_thread_backend_freezes_environment_and_metadata_failure_is_nonfatal(monkeypatch):
    calls = []

    async def artifact(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(execution_backend, "append_execution_artifact", artifact)
    monkeypatch.setattr(execution_backend, "execution_environment", lambda *args: {"axile_version": "frozen-inline"})
    asyncio.run(execution_backend._append_environment_snapshot("inline", object(), "ctp", "SINGLE-MAKER"))
    assert calls[0]["content"]["axile_version"] == "frozen-inline"

    def fail(*args):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(execution_backend, "execution_environment", fail)
    asyncio.run(execution_backend._append_environment_snapshot("inline", object(), "ctp", "SINGLE-MAKER"))
    assert len(calls) == 1


def test_redaction_preserves_token_contracts_and_removes_complete_quoted_secrets():
    redactor = EvidenceRedactor(EvidenceProfile.INTERNAL, 1, "private")
    cleaned = redactor.clean(
        {
            "error": "password='secret with spaces' Authorization=\"Bearer complete-private-token\"",
            "symbol_results": {"TOKENUSDT": {"status": "NOOP", "extra": {"api_key": "hidden"}}},
            "curr_target": {"TOKENUSDT": 1},
        }
    )
    assert "secret with spaces" not in cleaned["error"]
    assert "complete-private-token" not in cleaned["error"]
    assert cleaned["curr_target"] == {"TOKENUSDT": 1}
    assert "TOKENUSDT" in cleaned["symbol_results"]
    assert cleaned["symbol_results"]["TOKENUSDT"]["extra"] == {}
    shared = EvidenceRedactor(EvidenceProfile.SHAREABLE, 1, "PARTIAL").clean(
        {
            "status": "PARTIAL",
            "symbol": "PARTIALUSDT",
            "account_name": "PARTIAL",
            "account_id": 1,
            "curr_target": {"TOKENUSDT": 1},
            "actual_policy": {"operations": {"place_order": {"account": {"per_day": {"limit": 500}}}}},
        }
    )
    assert shared["status"] == "PARTIAL"
    assert shared["symbol"] == "PARTIALUSDT"
    assert "account_name" not in shared
    assert shared["account_id"] == "account-A"
    assert shared["curr_target"] == {"TOKENUSDT": 1}
    assert shared["actual_policy"]["operations"]["place_order"]["account"]["per_day"]["limit"] == 500
    aliases = EvidenceRedactor(EvidenceProfile.SHAREABLE, 1, "A").clean(
        [
            {"account_id": 1},
            {"account_id": 2},
            {"account_id": None},
            {"account_id": 2},
        ]
    )
    assert aliases == [
        {"account_id": "account-A"},
        {"account_id": "account-other-1"},
        {"account_id": None},
        {"account_id": "account-other-1"},
    ]


def test_openapi_describes_binary_download(database):
    schema = _client(database).app.openapi()
    operation = schema["paths"]["/account/executions/{execution_id}/evidence"]["get"]
    assert operation["responses"]["200"]["content"]["application/zip"]["schema"]["format"] == "binary"


def test_oversized_json_is_rejected_before_orm_decoding(database):
    _seed(database, _intent(), _record())

    async def driver():
        async with database() as session:
            await session.execute(
                text("UPDATE executerecord SET raw_result=:payload"), {"payload": "invalid-json" * 10000}
            )
            await session.commit()
        async with database() as session:
            with pytest.raises(EvidenceError) as error:
                await collect.collect_evidence(
                    session, ExportBudget(limits=replace(EvidenceLimits(), bytes=2000)), execution_id="exec-1"
                )
            assert error.value.status == 413

    asyncio.run(driver())


def test_trades_without_stable_identity_are_not_silently_deduplicated(database):
    record = _record()
    trade = {"order_id": "order-1", "trade_volume": 1, "trade_price": 3500, "trade_time": "2026-10-15T09:00:00"}
    record.raw_result["symbol_results"]["rb2610"]["trades"] = [dict(trade), dict(trade)]
    _seed(database, _intent(), record)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    assert len(_csv(archive, "executions/exec-1/trades.csv")) == 2
    assert _manifest(archive)["executions"][0]["trades"] == 2


@pytest.mark.parametrize("profile", ["shareable", "internal"])
@pytest.mark.parametrize("nested_symbol", [True, False], ids=["explicit-symbol", "inherited-symbol"])
@pytest.mark.parametrize("selection", ["execution", "date"])
def test_mixed_result_exports_each_order_and_trade_once(database, profile, nested_symbol, selection):
    record = _record()
    group = record.raw_result["symbol_results"]["rb2610"]
    for field in ("orders", "trades"):
        record.raw_result[field] = deepcopy(group[field])
        if not nested_symbol:
            group[field][0].pop("symbol")
    _seed(database, _intent(), record)
    url = "/account/executions/exec-1/evidence" if selection == "execution" else "/account/1/evidence"
    archive = _zip(
        _client(database).get(url, params={"profile": profile, "start_date": "2026-10-15", "end_date": "2026-10-15"})
    )
    manifest = _manifest(archive)
    report = manifest["executions"][0]
    assert report["orders"] == report["trades"] == 1
    assert report["order_snapshots"] == report["trade_snapshots"] == 1
    assert report["record_variants"] == {"orders": [], "trades": []}
    assert report["known_pending_order_ids"] == ["20261015-1-2-3"]
    for field in ("orders", "trades"):
        path = f"executions/exec-1/{field}.csv"
        rows = _csv(archive, path)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "rb2610"
        assert rows[0]["source_path"] == f"record.json#/raw_result/symbol_results/rb2610/{field}/0"
        assert next(entry for entry in manifest["files"] if entry["path"] == path)["rows"] == 1
        assert _csv(archive, "executions.csv")[0][field] == "1"
    raw = json.loads(archive.read("executions/exec-1/record.json"))["raw_result"]
    assert len(raw["orders"]) == len(raw["trades"]) == 1
    assert ("symbol" in raw["symbol_results"]["rb2610"]["trades"][0]) is nested_symbol


def test_mixed_result_keeps_records_only_present_at_top_level(database):
    record = _record()
    group = record.raw_result["symbol_results"]["rb2610"]
    record.raw_result["orders"] = [
        deepcopy(group["orders"][0]),
        {**deepcopy(group["orders"][0]), "symbol": "hc2610", "order_id": "order-2"},
    ]
    record.raw_result["trades"] = [
        deepcopy(group["trades"][0]),
        {**deepcopy(group["trades"][0]), "symbol": "hc2610", "order_id": "order-2", "trade_id": "trade-2"},
    ]
    _seed(database, _intent(), record)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    report = _manifest(archive)["executions"][0]
    assert report["orders"] == report["trades"] == 2
    for field in ("orders", "trades"):
        rows = _csv(archive, f"executions/exec-1/{field}.csv")
        assert [row["symbol"] for row in rows] == ["rb2610", "hc2610"]
        assert rows[1]["source_path"] == f"record.json#/raw_result/{field}/1"


def test_mixed_result_retains_different_snapshots_without_counting_new_records(database):
    record = _record()
    group = record.raw_result["symbol_results"]["rb2610"]
    record.raw_result["orders"] = [{**deepcopy(group["orders"][0]), "filled_volume": 2, "status": "全部成交"}]
    record.raw_result["trades"] = [{**deepcopy(group["trades"][0]), "trade_price": 3501}]
    _seed(database, _intent(), record)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    report = _manifest(archive)["executions"][0]
    assert report["orders"] == report["trades"] == 1
    assert report["order_snapshots"] == report["trade_snapshots"] == 2
    for field, identity in (("orders", "order_id"), ("trades", "trade_id")):
        rows = _csv(archive, f"executions/exec-1/{field}.csv")
        assert len(rows) == 2
        assert rows[0][identity] == rows[1][identity]
        assert len({row["source_path"] for row in rows}) == 2
        variants = report["record_variants"][field]
        assert len(variants) == 1
        assert variants[0][identity] == rows[0][identity]
        assert variants[0]["snapshots"] == 2
    assert "不同快照" in archive.read("executions/exec-1/summary.md").decode()


@pytest.mark.parametrize("result_source", ["record", "artifact"])
def test_flat_result_inherits_contract_from_result_context(database, result_source):
    record = _record()
    result = record.raw_result
    group = result.pop("symbol_results")["rb2610"]
    result["symbol"] = "rb2610"
    for field in ("orders", "trades"):
        result[field] = group[field]
        result[field][0].pop("symbol")
    rows = (
        [record]
        if result_source == "record"
        else [
            ExecutionArtifact(
                execution_id="exec-1", artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY, content=result
            )
        ]
    )
    _seed(database, _intent(), *rows)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    report = _manifest(archive)["executions"][0]
    assert report["orders"] == report["trades"] == 1
    for field in ("orders", "trades"):
        exported = _csv(archive, f"executions/exec-1/{field}.csv")
        assert len(exported) == 1
        assert exported[0]["symbol"] == "rb2610"
        assert exported[0]["source_path"].startswith("record.json#" if result_source == "record" else "artifacts.json#")


def test_mixed_result_without_stable_identity_keeps_every_snapshot(database):
    record = _record()
    group = record.raw_result["symbol_results"]["rb2610"]
    for field, identity in (("orders", "order_id"), ("trades", "trade_id")):
        group[field][0].pop(identity)
        record.raw_result[field] = deepcopy(group[field])
    _seed(database, _intent(), record)
    archive = _zip(_client(database).get("/account/executions/exec-1/evidence"))
    report = _manifest(archive)["executions"][0]
    assert report["orders"] == report["trades"] == 2
    assert report["unidentified_order_snapshots"] == report["unidentified_trade_snapshots"] == 2
    assert len(_csv(archive, "executions/exec-1/orders.csv")) == 2
    assert len(_csv(archive, "executions/exec-1/trades.csv")) == 2
