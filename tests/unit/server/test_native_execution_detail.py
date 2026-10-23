"""原生执行详情与迁移前完整 HTTP 契约保持一致。"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server._native import _core
from axile.server.api.deps import get_db
from axile.server.api.routes import account_crud
from axile.server.business_facts.repository import read_execution_detail, store_execution_facts
from axile.server.db.models.execution import ExecuteRecord
from tests.unit.server._execution_read_oracle import ExecuteRecordDetailPublic, read_execution_summaries
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_business_facts_tq import tq_historical_payloads


@pytest.mark.parametrize("page,journal", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize(
    "symbol,expected",
    [
        ({"error": "failed"}, {"error": "failed", "outcome": None}),
        ({"outcome": "error"}, {"outcome": "error", "status": "FAILED", "error": "执行失败，具体原因见执行证据"}),
        (
            {"status": "FAILED", "error": "RuntimeError: secret"},
            {"status": "FAILED", "outcome": "error", "error": "执行失败，具体原因见执行证据"},
        ),
        (
            {"status": "PARTIAL", "memory": {"message": "未达到目标"}},
            {"status": "PARTIAL", "outcome": "not_reached", "error": "未达到目标"},
        ),
        ({"outcome": "terminated"}, {"status": None, "outcome": "terminated", "error": "执行已终止"}),
        (
            {"status": "BLOCKED", "error": "当前不在交易时间", "reason_code": "CLOSED"},
            {
                "status": "BLOCKED",
                "error": "当前不在交易时间",
                "outcome": "blocked",
                "reason_code": "COMMON.SESSION.CLOSED",
            },
        ),
    ],
)
def test_execution_response_reuses_complete_legacy_display_contract(page, journal, symbol, expected):
    record = {"id": 1, "symbol_results": {"A": symbol}}
    item = {"kind": "execution", "record": record} if journal else record
    payload = {"data": [item], "count": 1} if page else record
    response = account_crud._execution_response(json.dumps(payload), page=page, journal=journal)
    decoded = json.loads(response.body)
    actual = decoded["data"][0] if page else decoded
    if journal:
        actual = actual["record"]
    assert actual["symbol_results"] == {"A": expected}
    assert "secret" not in response.body.decode()
    assert "memory" not in actual["symbol_results"]["A"]


@pytest.mark.parametrize(
    "source_input,source_result",
    [
        tq_historical_payloads(),
        (None, []),
        (False, "invalid"),
        ({}, {}),
        (
            {"channel_type": "ctp", "curr_target": {}, "last_target": None},
            {"status": "NOOP", "execution_time": 0.0, "symbol_results": {}},
        ),
        (
            {"curr_target": {"rb2610": 1}, "extra": {"zero": 0, "null": None}},
            {
                "status": "FAILED",
                "technical_detail": "错误\n" + "根因" * 300,
                "symbol_results": {
                    "rb2610": {
                        "status": "FAILED",
                        "error": None,
                        "sizing": {"reason_code": "SIZED", "target_quantity": 1},
                        "orders": [{"order_id": "o1", "volume": 1, "filled_volume": 1}],
                        "trades": [{"trade_id": "t1", "trade_volume": 1, "trade_price": 100}],
                    }
                },
            },
        ),
    ],
)
def test_native_detail_matches_repository_and_summary(tmp_path, monkeypatch, source_input, source_result):
    database = tmp_path / "detail.sqlite"
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(database)))

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add_all([build_account(id=1), build_account(id=2)])
                await session.flush()
                record = ExecuteRecord(account_id=1, execution_id="canonical-detail", is_success=0)
                session.add(record)
                await session.flush()
                if isinstance(source_result, dict):
                    await store_execution_facts(session, record, source_input, source_result)
                else:
                    from axile.server.business_facts.head import execution_head
                    from axile.server.business_facts.inputs import encode_inputs
                    from axile.server.business_facts.repository import encode_result_facts

                    record.input_id = "historical-input"
                    input_batch = encode_inputs(
                        record.input_id,
                        source_input,
                        account_id=record.account_id,
                        execution_id=record.execution_id,
                        created_at=record.created_at,
                    )
                    for table, rows in input_batch.items():
                        if rows:
                            await session.execute(table.insert(), rows)
                    result_batch = encode_result_facts(record.id, source_result, is_success=record.is_success)
                    for name, value in result_batch.pop(execution_head)[0].items():
                        if name != "id":
                            setattr(record, name, value)
                    for table, rows in result_batch.items():
                        if rows:
                            await session.execute(table.insert(), rows)
                await session.commit()
                summary = (await read_execution_summaries(session, [record]))[record.id]
                detail = await read_execution_detail(session, record)
                expected = ExecuteRecordDetailPublic.model_validate({**summary.model_dump(), **detail}).model_dump(
                    mode="json"
                )
                record_id = record.id
            app = FastAPI()
            app.include_router(account_crud.router)

            async def reject_session():
                raise AssertionError("native detail must not open a Python database session")

            app.dependency_overrides[get_db] = reject_session

            def reject_sql(*_args):
                raise AssertionError("native detail must not execute Python SQL")

            event.listen(engine.sync_engine, "before_cursor_execute", reject_sql)
            try:
                assert json.loads((await account_crud.execute_record_detail(1, record_id)).body) == expected
                async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                    response = await client.get(f"/execute_records/1/{record_id}")
                    assert response.status_code == 200, response.text
                    assert response.json() == expected
                    assert not {"raw_input", "raw_result"}.intersection(response.json())
                    for account_id, requested_record in [(99, record_id), (1, 99999), (2, record_id)]:
                        response = await client.get(f"/execute_records/{account_id}/{requested_record}")
                        assert response.status_code == 404, response.text
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", reject_sql)
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE executerecord SET input_id='missing-input' WHERE id=:id"),
                    {"id": record_id},
                )
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                response = await client.get(f"/execute_records/1/{record_id}")
                assert response.status_code == 503, response.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "corruption",
    ["foreign_asset", "foreign_input", "foreign_result_input", "event_hash", "capture_hash", "missing_plan"],
)
def test_native_detail_frozen_context_ownership_and_hashes(tmp_path, monkeypatch, corruption):
    from copy import deepcopy

    from sqlalchemy import select

    from axile.server.business_facts.assets import asset_binding
    from axile.server.business_facts.extensions import execution_extension
    from axile.server.business_facts.inputs import execution_input_plan
    from axile.server.business_facts.notification_context import compact_record_notification_context
    from axile.server.db.models.supplement import NotificationEvent
    from tests.unit.server.test_notification_compaction_projections import _EXECUTION, _event, _result

    database = tmp_path / "frozen.sqlite"
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(database)))

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add_all([build_account(id=1), build_account(id=2)])
                await session.flush()
                source_input, _ = tq_historical_payloads()
                # Match the existing frozen-plan fixture's independent quantity/unit states.
                source_input.update(target_quantities={"A": 0, "B": None}, target_units={"A": 2})
                record = ExecuteRecord(account_id=1, execution_id=_EXECUTION, is_success=0)
                other = ExecuteRecord(account_id=2, execution_id="foreign-frozen-owner", is_success=0)
                session.add_all([record, other])
                await session.flush()
                result = _result()
                result["inputs"] = {**deepcopy(source_input), "curr_target": {"A": 0.75}}
                await store_execution_facts(session, record, source_input, result)
                await store_execution_facts(session, other, source_input, _result())
                notification = _event()
                session.add(notification)
                await session.flush()
                assert await compact_record_notification_context(session, record, notification)
                await session.commit()
                summary = (await read_execution_summaries(session, [record]))[record.id]
                expected = ExecuteRecordDetailPublic.model_validate(
                    {**summary.model_dump(), **await read_execution_detail(session, record)}
                ).model_dump(mode="json")
                assert expected["input"]["plan"]["quantities"] == {"A": 0, "B": None}
                assert expected["input"]["plan"]["units"] == {"A": 2}
                assert (
                    expected["result"]["extra"]["server_notification_context"]
                    == _result()["extra"]["server_notification_context"]
                )
                record_id = record.id
                other_input = other.input_id
                borrowed_asset = await session.scalar(
                    select(asset_binding.c.asset_id).where(
                        asset_binding.c.record_id == other.id, asset_binding.c.path == "/account_assets"
                    )
                )
                assert borrowed_asset is not None
                input_id = record.input_id
            app = FastAPI()
            app.include_router(account_crud.router)

            async def reject_session():
                raise AssertionError("native detail must not open a Python database session")

            app.dependency_overrides[get_db] = reject_session
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                response = await client.get(f"/execute_records/1/{record_id}")
                assert response.status_code == 200, response.text
                assert response.json() == expected
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    if corruption == "foreign_asset":
                        await session.execute(
                            asset_binding.update()
                            .where(asset_binding.c.record_id == record_id)
                            .values(asset_id=borrowed_asset)
                        )
                    elif corruption in {"foreign_input", "foreign_result_input"}:
                        field = "input_id" if corruption == "foreign_input" else "result_input_id"
                        await session.execute(
                            text(f"UPDATE executerecord SET {field}=:input WHERE id=:id"),
                            {"input": other_input, "id": record_id},
                        )
                    elif corruption == "event_hash":
                        notification = await session.scalar(
                            select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
                        )
                        context = deepcopy(notification.context)
                        context["account"]["name"] = "tampered"
                        notification.context = context
                        session.add(notification)
                    elif corruption == "capture_hash":
                        extra = deepcopy(
                            await session.scalar(
                                select(execution_extension.c.extra).where(execution_extension.c.record_id == record_id)
                            )
                        )
                        extra["server_notification_context"]["capture_sha256"] = "0" * 64
                        await session.execute(
                            execution_extension.update()
                            .where(execution_extension.c.record_id == record_id)
                            .values(extra=extra)
                        )
                    else:
                        await session.execute(
                            execution_input_plan.delete().where(execution_input_plan.c.input_id == input_id)
                        )
                    await session.commit()
                response = await client.get(f"/execute_records/1/{record_id}")
                assert response.status_code == 503, response.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())
