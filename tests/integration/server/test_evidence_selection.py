"""离线验证资产控制上下文、同日批次选择及计数时间证据。"""

import asyncio
import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel, select

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.api.deps import get_db
from axile.server.api.routes import account_evidence
from axile.server.db.models import Account, AccountControlCounterDelta, AccountControlEvent, ExecuteRecord
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.db.models.execution import ExecutionArtifact, ExecutionEvent
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.evidence.models import EvidenceLimits, ExportBudget
from axile.server.execution import execution_account_control
from axile.server.execution.worker_backend import worker, worker_state
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest

pytestmark = pytest.mark.slow
DAY = "/account/1/evidence?start_date=2026-10-15&end_date=2026-10-15"
STAMP = int(datetime(2026, 10, 15, 1, tzinfo=UTC).timestamp() * 1000)


def account(identifier):
    return Account(
        id=identifier,
        name=f"test-{identifier}",
        market="期货",
        trade_channel="ctp",
        account_control_preset="ctp",
        account_config={},
        is_started=False,
        cron_expr="",
        brokerage="ctp",
        weight_precision=0.01,
        algorithm={"method": "SINGLE-MAKER"},
        feishu_key=None,
    )


@pytest.fixture
def evidence_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def initialize():
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with factory() as session:
            session.add_all([account(1), account(2)])
            await session.commit()

    asyncio.run(initialize())
    yield factory
    asyncio.run(engine.dispose())


def seed(factory, *rows):
    async def write():
        async with factory() as session:
            session.add_all(rows)
            await session.commit()

    asyncio.run(write())


def client(factory):
    app = FastAPI()
    app.include_router(account_evidence.router, prefix="/account")

    async def database():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = database
    return TestClient(app)


def manifest(response):
    assert response.status_code == 200, response.text
    with ZipFile(io.BytesIO(response.content)) as archive:
        return json.loads(archive.read("manifest.json"))


def event(key="exec-1", **overrides):
    values = dict(
        account_id=1,
        execution_id=key,
        control_date="2026-10-15",
        channel="ctp",
        seq=1,
        event_uid=f"event-{key}",
        operation="place_order",
        decision="allowed",
        counted=True,
        outcome="submitted",
        occurred_at_ms=STAMP,
        context_kind="execution",
        source="algorithm",
    )
    values.update(overrides)
    return AccountControlEvent(**values)


def counter(key="exec-1", **overrides):
    values = dict(
        account_id=1,
        execution_id=key,
        control_date="2026-10-15",
        bucket_type="day",
        bucket_start="2026-10-15T00:00:00+08:00",
        scope_type="account",
        operation="place_order",
        delta_count=1,
        delta_uid=f"counter-{key}",
        context_kind="execution",
        source="algorithm",
    )
    values.update(overrides)
    return AccountControlCounterDelta(**values)


def complete_execution(key="exec-1", **overrides):
    values = dict(
        account_id=1,
        execution_id=key,
        kind="rebalance",
        trigger_source="manual",
        status="SUCCEEDED",
        created_at="2026-10-15T09:00:00+08:00",
        finished_at="2026-10-15T09:00:10+08:00",
        channel="ctp",
        algorithm="SINGLE-MAKER",
    )
    values.update(overrides)
    rows = [
        ExecutionIntent(**values),
        ExecuteRecord(
            account_id=values["account_id"],
            execution_id=key,
            is_success=1,
            created_at=values["created_at"],
            raw_result={},
        ),
        ExecutionEvent(
            account_id=values["account_id"],
            execution_id=key,
            channel="ctp",
            algorithm="SINGLE-MAKER",
            seq=1,
            event_uid=f"execution-{key}",
            event_type="execution_started",
            status="INFO",
            reason_family="SYSTEM",
            reason_code="COMMON.EXECUTION_STARTED",
            ts_local_created=values["created_at"],
        ),
    ]
    for kind in (
        "environment_snapshot",
        "account_snapshot_before",
        "standard_input",
        "target_snapshot",
        "trade_rules_snapshot",
        "execution_summary",
        "account_snapshot",
    ):
        content = {"account_assets": {"positions": [], "source": "real"}} if "account_snapshot" in kind else {}
        rows.append(ExecutionArtifact(execution_id=key, artifact_type=kind, content=content))
    return rows


@pytest.mark.parametrize("execution_id", [None, "exec-asset"])
@pytest.mark.parametrize("command", ["get_account_assets", "prepare"])
def test_worker_persists_operation_type_without_losing_execution_association(
    evidence_db, monkeypatch, execution_id, command
):
    class AssetsExecutor:
        guard = None

        def set_account_control_guard(self, guard):
            self.guard = guard

        def get_account_control_guard(self):
            return self.guard

        def export_account_control_records(self):
            return self.guard.flush_records()

        def get_account_assets(self):
            self.guard.begin_operation("query_account")
            return UnifiedAccountAssets(available_cash=1, total_asset=1, market_value=0, positions=[])

    executor = AssetsExecutor()
    monkeypatch.setattr(execution_account_control, "SessionLocal", evidence_db)
    monkeypatch.setattr(worker_state, "reuse_resident_connection", lambda *_: executor)

    def prepare_connection(*args, **kwargs):
        kwargs["prepare"](executor)
        executor.get_account_assets()
        return executor

    monkeypatch.setattr(worker_state, "ensure_resident_connection", prepare_connection)
    request = WorkerBackendRequest(
        request_id="assets-request",
        command=command,
        account_payload=account(1).model_dump(mode="json"),
        execution_id=execution_id,
        payload={},
    )
    response = worker._handle_worker_request(request, worker_state._WorkerBackendState())
    assert response.kind == "result", response.error

    async def verify():
        async with evidence_db() as session:
            rows = (await session.execute(select(AccountControlSession))).scalars().all()
            assert len(rows) == 1
            assert rows[0].execution_id == (execution_id or "assets-request")
            assert rows[0].context_kind == ("execution" if execution_id else "account_operation")
            source = "account_assets" if command == "get_account_assets" else "connection_prepare"
            assert rows[0].source == ("algorithm" if execution_id else source)
            assert rows[0].status == "closed"
            events = (await session.execute(select(AccountControlEvent))).scalars().all()
            assert events and all(row.context_kind == rows[0].context_kind for row in events)

    asyncio.run(verify())


def test_many_asset_contexts_do_not_consume_execution_budget(evidence_db):
    seed(
        evidence_db,
        *complete_execution(),
        *[event(f"asset-{i}", context_kind="account_operation", source="account_assets") for i in range(101)],
    )
    data = manifest(client(evidence_db).get(DAY))
    assert [row["key"] for row in data["executions"]] == ["exec-1"]
    assert data["account_operations"]["count"] == 101


@pytest.mark.parametrize("count", [1, 2])
def test_precise_execution_keeps_material_coverage_and_reports_counter_reconciliation(evidence_db, count):
    seed(evidence_db, *complete_execution(), event(), counter(delta_count=count))
    data = manifest(client(evidence_db).get(DAY))["executions"][0]["coverage"]
    assert data["coverage"] == "available"
    assert "control_counter_activity_time_unknown" not in data["issues"]
    assert data["context_time"] == "known"
    assert data["selection_time"] == "recorded_activity"
    assert data["counter_time"]["status"] == ("reconciled" if count == 1 else "unknown")


def test_counter_only_history_keeps_unknown_and_missing_materials(evidence_db):
    seed(evidence_db, counter("history", context_kind="unknown"))
    data = manifest(client(evidence_db).get(DAY))["executions"][0]
    assert data["context_kind"] == "unknown"
    assert data["coverage"]["coverage"] == "incomplete"
    assert "control_counter_activity_time_unknown" in data["coverage"]["issues"]
    assert data["coverage"]["counter_time"]["status"] == "unknown"
    assert data["coverage"]["selection_time"] == "bucket_overlap_assumed"


def test_cross_day_execution_retains_context_but_only_summarizes_selected_day(evidence_db):
    seed(evidence_db, *complete_execution(finished_at="2026-10-16T09:00:00+08:00"), event(), counter())
    data = manifest(client(evidence_db).get(DAY.replace("2026-10-15", "2026-10-16")))["executions"][0]
    assert data["coverage"]["coverage"] == "available"
    assert data["coverage"]["counter_time"]["status"] == "reconciled"


def test_batch_export_splits_same_day_without_scanning_unselected_executions(evidence_db):
    seed(evidence_db, *[row for i in range(101) for row in complete_execution(f"exec-{i}")])
    http = client(evidence_db)
    assert http.get(DAY).status_code == 413
    selected = ["exec-2", "exec-100"]
    data = manifest(http.post("/account/1/evidence", json={"execution_ids": selected}))
    assert [row["key"] for row in data["executions"]] == selected
    assert data["selection"] == {"basis": "explicit_execution_ids", "execution_ids": selected}
    assert data["account_operations"]["count"] == 0


@pytest.mark.parametrize(
    "ids,status",
    [
        ([], 422),
        ([" "], 422),
        (["exec-1", "exec-1"], 422),
        ([f"exec-{i}" for i in range(101)], 422),
        (["missing"], 404),
        (["exec-1", "other-account"], 404),
    ],
)
def test_batch_rejects_invalid_or_foreign_ids_as_a_whole(evidence_db, ids, status):
    seed(evidence_db, *complete_execution(), *complete_execution("other-account", account_id=2))
    response = client(evidence_db).post("/account/1/evidence", json={"execution_ids": ids})
    assert response.status_code == status


def test_batch_preserves_owner_conflict_and_shared_resource_limits(evidence_db, monkeypatch):
    seed(evidence_db, *complete_execution(), *complete_execution("limited"), event("exec-1", account_id=2))
    http = client(evidence_db)
    assert http.post("/account/1/evidence", json={"execution_ids": ["exec-1"]}).status_code == 409
    monkeypatch.setattr(
        account_evidence, "ExportBudget", lambda: ExportBudget(limits=replace(EvidenceLimits(), rows=1))
    )
    assert http.post("/account/1/evidence", json={"execution_ids": ["limited"]}).status_code == 413
