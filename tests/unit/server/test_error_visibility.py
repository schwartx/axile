"""完整错误响应与跨进程证据回归；只使用合成异常和本地 SQLite。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.api.deps import HistoryPagination
from axile.server.api.routes import account_assets, account_execution
from axile.server.api.routes.account_crud import execute_record_detail, list_execute_records
from axile.server.business_facts.repository import store_execution_facts
from axile.server.error_details import exception_details
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from axile.server.execution.worker_backend.protocol import WorkerBackendResponse
from axile.server.execution.worker_backend.worker_responses import _build_error_payload
from tests.unit.server._execution_test_support import build_account

RAW = "起始原因\n" + "长错误" * 300 + "\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因"


def _raised_error():
    try:
        raise RuntimeError(RAW)
    except RuntimeError as exc:
        return exc


def test_worker_error_survives_protocol_output_and_remote_exception():
    error = _build_error_payload(_raised_error())
    assert error.technical_detail == RAW
    assert "Traceback" in error.traceback
    assert RAW in error.traceback
    response = WorkerBackendResponse(request_id="test", kind="error", error=error)
    output = WorkerBackendManager._handle_response(response).model_dump(mode="json")
    assert output["extra"]["worker_error"]["technical_detail"] == RAW
    assert output["extra"]["worker_error"]["traceback"] == error.traceback


@pytest.mark.parametrize("route", [account_execution.execute, account_execution.empty_all_positions])
def test_enqueue_error_returns_original_with_actual_traceback(route, monkeypatch):
    monkeypatch.setattr(account_execution, "_get_account_or_404", AsyncMock(return_value=build_account()))
    failure = _raised_error()
    if route is account_execution.execute:
        monkeypatch.setattr(account_execution, "enqueue_execute_trade", AsyncMock(side_effect=failure))
        call = route(session=SimpleNamespace(), account_id=1)
    else:
        from axile.server.execution import supplements

        monkeypatch.setattr(supplements, "cancel_groups_in_session", AsyncMock())
        monkeypatch.setattr(account_execution, "delete_job", Mock())
        monkeypatch.setattr(account_execution, "reconcile_china_channel_account", AsyncMock())
        monkeypatch.setattr(account_execution, "enqueue_empty_positions", AsyncMock(side_effect=failure))
        call = route(session=SimpleNamespace(add=Mock(), commit=AsyncMock()), sched=SimpleNamespace(), account_id=1)
    with pytest.raises(HTTPException) as captured:
        asyncio.run(call)
    assert captured.value.status_code == 500
    detail = captured.value.detail
    assert detail["technical_detail"] == RAW
    assert RAW in detail["traceback"]


@pytest.mark.parametrize("error, code", [(_raised_error(), 502), (TimeoutError(RAW), 504)])
def test_asset_error_preserves_original_and_http_semantics(error, code, monkeypatch):
    session = SimpleNamespace(close=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(account_assets, "_get_account_or_404", AsyncMock(return_value=build_account()))
    monkeypatch.setattr(account_assets, "query_account_assets", AsyncMock(side_effect=error))
    monkeypatch.setattr(account_assets, "try_register_account_asset_refresh", lambda _id: True)
    released = []
    monkeypatch.setattr(account_assets, "clear_account_asset_refresh", released.append)
    with pytest.raises(HTTPException) as captured:
        asyncio.run(account_assets.refresh_account_assets(session=session, account_id=7))
    assert captured.value.status_code == code
    assert captured.value.detail["technical_detail"] == RAW
    assert RAW in captured.value.detail["traceback"]
    assert released == [7]


def test_remote_traceback_takes_precedence_over_local_wrapper():
    error = _raised_error()
    error.formatted_traceback = "Traceback from worker\n  actual script line\n" + RAW
    detail = exception_details(error)
    assert detail["traceback"] == error.formatted_traceback


def test_exact_record_filters_share_account_scope_count_and_pagination(tmp_path, monkeypatch):
    from axile.server._native import _core
    from axile.server.api.routes import account_crud

    database = tmp_path / "executions.sqlite"
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(database)))
    from axile.server.db.models import ExecuteRecord

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add_all([build_account(id=1), build_account(id=2)])
            records = [ExecuteRecord(id=i, account_id=1, execution_id=f"exec-{i}", is_success=0) for i in range(1, 603)]
            other = ExecuteRecord(id=1000, account_id=2, execution_id="other-account", is_success=0)
            session.add_all([*records, other])
            await session.flush()
            for record in records:
                await store_execution_facts(session, record, raw_input={}, raw_result={"technical_detail": RAW})
            await store_execution_facts(session, other, raw_input={}, raw_result={})
            await session.commit()
            page = HistoryPagination(skip=0, limit=1)
            result = await list_execute_records(1, page, execution_id="exec-1", record_id=1)
            assert result["count"] == 1
            assert [row["id"] for row in result["data"]] == [1]
            assert "result" not in result["data"][0]
            detail = await execute_record_detail(1, 1)
            assert detail["result"]["technical_detail"] == RAW
            for execution_id, record_id in [("exec-1", 2), ("other-account", None), (None, 1000)]:
                result = await list_execute_records(1, page, execution_id=execution_id, record_id=record_id)
                assert result["count"] == 0
                assert result["data"] == []
            skipped = await list_execute_records(1, HistoryPagination(skip=1, limit=1), execution_id="exec-1")
            assert skipped["count"] == 1
            assert skipped["data"] == []
            unfiltered = await list_execute_records(1, page)
            assert unfiltered["count"] == 602
            assert unfiltered["data"][0]["id"] == 602
        await engine.dispose()

    asyncio.run(run())
