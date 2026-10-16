"""精确目录负查询保留分类，不经过组合函数或回收健康 worker。"""

import asyncio
import multiprocessing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.instrument_catalog import CatalogError, InstrumentCatalog
from axile.executor.instrument_catalog_records import Instrument
from axile.server.api.deps import get_db
from axile.server.api.routes import instrument_catalog as routes
from axile.server.execution.worker_backend.catalog import CatalogSession, RemoteCatalogProvider
from axile.server.execution.worker_backend.manager import WorkerBackendManager

pytestmark = pytest.mark.slow
ROW = Instrument("SHFE", "rb2701", "FUTURE", 10, 1)


@pytest.mark.parametrize(
    "codes,expected",
    [
        (["CATALOG.NOT_FOUND"], "CATALOG.NOT_FOUND"),
        (["CATALOG.NOT_FOUND", "CATALOG.NOT_FOUND"], "CATALOG.NOT_FOUND"),
        (["CATALOG.NOT_FOUND", "CATALOG.TIMEOUT"], "CATALOG.MIXED_FAILURE"),
    ],
)
def test_catalog_preserves_all_source_causes(codes, expected):
    catalog = InstrumentCatalog()

    def source(code):
        def query(*args):
            raise CatalogError("lookup failed", code)

        return query

    try:
        for index, code in enumerate(codes):
            catalog.register(str(index), source(code))
        with pytest.raises(CatalogError) as caught:
            catalog.ensure(["SHFE.missing"])
        assert caught.value.reason_code == expected
        assert [cause["reason_code"] for cause in caught.value.causes] == codes
    finally:
        catalog.close()


def test_not_found_and_causes_cross_ipc_without_invalidating_the_source():
    parent, child = multiprocessing.Pipe()
    catalog = InstrumentCatalog()
    session = CatalogSession(parent, catalog)
    provider = RemoteCatalogProvider(child)
    provider.bind(lambda symbols, timeout: [] if symbols == ["SHFE.missing"] else [ROW])
    try:
        with pytest.raises(CatalogError) as caught:
            provider.ensure(["SHFE.missing"])
        assert caught.value.reason_code == "CATALOG.NOT_FOUND"
        assert caught.value.causes[0]["reason_code"] == "CATALOG.NOT_FOUND"
        assert not provider.failed
        assert provider.ensure([ROW.key])[ROW.key] == ROW
    finally:
        provider.stop()
        session.stop()
        catalog.close()


def test_exact_lookup_uses_selected_paused_session_and_keeps_worker_on_not_found(monkeypatch):
    manager = WorkerBackendManager()
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        return [] if symbols == ["SHFE.missing"] else [ROW]

    manager.instrument_catalog.register("selected", query, enabled=False)
    handle = SimpleNamespace(catalog=SimpleNamespace(source="selected"))
    manager._workers[1] = handle
    monkeypatch.setattr(manager, "_check_read_session", lambda account: None)
    monkeypatch.setattr(manager, "_is_handle_healthy", lambda value: value is handle)
    account = SimpleNamespace(id=1)
    try:
        with pytest.raises(CatalogError) as caught:
            asyncio.run(manager.query_instruments(account, ["SHFE.missing"]))
        assert caught.value.reason_code == "CATALOG.NOT_FOUND"
        assert manager._workers[1] is handle
        assert asyncio.run(manager.query_instruments(account, [ROW.key]))[ROW.key] == ROW.wire()
        assert calls == [["SHFE.missing"], [ROW.key]]
        assert manager.instrument_catalog.status()["sources"] == 0
    finally:
        manager._workers.clear()
        manager.close()


@pytest.mark.parametrize(
    "code,status", [(None, 200), ("CATALOG.NOT_FOUND", 404), ("CATALOG.TIMEOUT", 408), ("CATALOG.MIXED_FAILURE", 503)]
)
def test_exact_query_http_preserves_structured_result_and_errors(monkeypatch, code, status):
    async def query(account, symbols, **kwargs):
        assert symbols == [ROW.key]
        if code:
            raise CatalogError(
                "query failed", code, causes=[{"reason_code": code, "type": "CatalogError", "message": "query failed"}]
            )
        return {ROW.key: ROW.wire()}

    class Database:
        async def get(self, model, key):
            return SimpleNamespace(snapshot=lambda: SimpleNamespace(id=key))

    async def database():
        yield Database()

    monkeypatch.setattr(routes, "get_worker_backend_manager", lambda: SimpleNamespace(query_instruments=query))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = database
    response = TestClient(app).post("/instrument-catalog/query", json={"account_id": 1, "symbols": [ROW.key]})
    assert response.status_code == status
    if code:
        assert response.json()["detail"]["code"] == code
        assert response.json()["detail"]["causes"][0]["reason_code"] == code
    else:
        assert response.json()["data"][ROW.key]["tick"] == 1
