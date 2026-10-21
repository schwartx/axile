"""阶段审计与真实原生发布竞争写锁时保留提交和通知语义。"""

import asyncio
import json
import threading

import sqlalchemy as sa

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.server.core.db import register_sqlite_concurrency_pragmas
from axile.server.db.models import ExecutionEvent
from axile.server.execution import audit_batch
from axile.server.execution_audit import build_execution_event
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION, enqueue
from tests.unit.server import test_business_facts_native_pipeline as pipeline

rebuilt_native_binary = pipeline.rebuilt_native_binary


def test_phase_audit_competes_with_native_publication(tmp_path, monkeypatch, rebuilt_native_binary):
    create_engine = pipeline.create_async_engine

    def configured_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        register_sqlite_concurrency_pragmas(engine)
        return engine

    monkeypatch.setattr(pipeline, "create_async_engine", configured_engine)
    notifications = []
    monkeypatch.setattr(
        audit_batch,
        "_notify_live_hub",
        lambda **fields: notifications.append((threading.current_thread().name, fields["execution_id"])),
    )

    async def scenario():
        async with pipeline._database(tmp_path, rebuilt_native_binary, monkeypatch) as (sessions, manager, native, _):
            monkeypatch.setattr(audit_batch, "SessionLocal", sessions)

            async def write_audits():
                total = 0
                for index in range(3):
                    batch = audit_batch.ExecutionAuditBatch(max_entries=3)
                    for offset in range(3):
                        batch.add_event(
                            build_execution_event(
                                execution_id=f"concurrent-audit-{index}",
                                account_id=pipeline.ACCOUNT,
                                channel=TradeChannel.TQ,
                                algorithm="offline",
                                event_type=ExecutionEventType.EXECUTION_STARTED,
                                status=ExecutionEventStatus.INFO,
                                reason_family=ExecutionReasonFamily.SYSTEM,
                                reason_code="COMMON.EXECUTION_STARTED",
                                seq=offset,
                            )
                        )
                    total += await audit_batch.append_execution_audit_batch(batch)
                return total

            _, written = await asyncio.gather(pipeline._publish(sessions, manager, monkeypatch), write_audits())
            async with sessions() as session:
                persisted = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ExecutionEvent)
                    .where(ExecutionEvent.execution_id.like("concurrent-audit-%"))
                )
                await enqueue(session, pipeline.ACCOUNT)
            assert await manager.run_once()
            snapshot = json.loads(
                await native.query(
                    "snapshot",
                    account_id=pipeline.ACCOUNT,
                    range="all",
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                )
            )
            assert written == persisted == len(notifications) == 9
            assert all(name == "MainThread" for name, _ in notifications)
            assert snapshot["status"] == "ready"

    asyncio.run(scenario())
