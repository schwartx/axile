"""精确目录负查询保留分类，不经过组合函数或回收健康 worker。"""

import asyncio
import multiprocessing
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.instrument_catalog import (
    CatalogError,
    InstrumentCatalog,
    catalog_deadline,
    catalog_remaining,
)
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


@contextmanager
def real_catalog_api(monkeypatch, query):
    """真实 manager／目录／IPC／HTTP，只有 SDK 查询来源是替身。"""
    parent, child = multiprocessing.Pipe()
    manager = WorkerBackendManager()
    session = CatalogSession(parent, manager.instrument_catalog)
    provider = RemoteCatalogProvider(child)
    provider.bind(query, channel="ctp")
    handle = SimpleNamespace(catalog=session)
    manager._workers[1] = handle
    monkeypatch.setattr(manager, "_check_read_session", lambda account: None)
    monkeypatch.setattr(manager, "_is_handle_healthy", lambda value: value is handle)
    monkeypatch.setattr(routes, "get_worker_backend_manager", lambda: manager)

    class Database:
        async def get(self, model, key):
            return SimpleNamespace(snapshot=lambda: SimpleNamespace(id=key))

    async def database():
        yield Database()

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app) as api:
            yield api, manager, provider, handle
    finally:
        manager._workers.clear()
        provider.stop()
        session.stop()
        manager.close()


@pytest.mark.parametrize(
    "symbols,rows,code,status",
    [
        ([ROW.key, "SHFE.missing"], [ROW], "CATALOG.NOT_FOUND", 404),
        ([ROW.symbol], [ROW, replace(ROW, exchange="DCE")], "CATALOG.AMBIGUOUS", 409),
    ],
)
def test_partial_and_ambiguous_matches_keep_classification_through_real_ipc_http(
    monkeypatch, symbols, rows, code, status
):
    with real_catalog_api(monkeypatch, lambda requested, timeout: rows if requested == symbols else [ROW]) as (
        api,
        manager,
        provider,
        handle,
    ):
        response = api.post("/instrument-catalog/query", json={"account_id": 1, "symbols": symbols})
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code
        assert response.json()["detail"]["causes"][0]["reason_code"] == code
        assert api.post("/instrument-catalog/query", json={"account_id": 1, "symbols": [ROW.key]}).status_code == 200
        assert manager._workers[1] is handle and not provider.failed


@pytest.mark.parametrize("require_specs", [False, True])
@pytest.mark.parametrize("use_cache", [False, True])
def test_batch_timeout_stops_source_and_releases_query_slot_without_recycling_worker(
    monkeypatch, require_specs, use_cache
):
    finished = threading.Event()
    query_lock = threading.Lock()
    requested = []
    budgets = []

    def query(symbols, timeout):
        if symbols == [ROW.key]:
            assert query_lock.acquire(timeout=0.5)
            query_lock.release()
            return [ROW]
        assert timeout is not None and timeout <= 0.15
        deadline = catalog_deadline(timeout)
        budgets.append(deadline)
        with query_lock:
            try:
                for symbol in symbols:
                    catalog_remaining(deadline)
                    requested.append(symbol)
                    get_default_clock().sleep(0.02)
            finally:
                finished.set()
        pytest.fail("50-symbol source must stop at the original deadline")

    with real_catalog_api(monkeypatch, query) as (api, manager, provider, handle):
        response = api.post(
            "/instrument-catalog/query",
            json={
                "account_id": 1,
                "symbols": [f"SHFE.test{index}" for index in range(50)],
                "timeout_seconds": 0.15,
                "require_specs": require_specs,
                "use_cache": use_cache,
            },
        )
        assert response.status_code == 408, response.text
        assert response.json()["detail"]["code"] == "CATALOG.TIMEOUT"
        assert finished.wait(1), "query source retained its slot after the HTTP timeout"
        assert 0 < len(requested) < 50
        assert len(budgets) == 1
        assert api.post("/instrument-catalog/query", json={"account_id": 1, "symbols": [ROW.key]}).status_code == 200
        assert manager._workers[1] is handle and not provider.failed


def test_ipc_dispatch_queue_does_not_restart_query_timeout(monkeypatch):
    from axile.server.execution.worker_backend import catalog as ipc

    now = [100.0]
    monkeypatch.setattr("axile.executor.instrument_catalog.clock_monotonic", lambda: now[0])
    monkeypatch.setattr(ipc, "clock_monotonic", lambda: now[0])
    calls = []
    sent = []
    pipe = object.__new__(RemoteCatalogProvider)
    pipe._lock = threading.Lock()
    pipe.epoch = "current"
    pipe.query = lambda symbols, timeout: calls.append(timeout) or [ROW]
    pipe._send = sent.append
    # 消息在 100 时创建、截止 105；调度到来源线程时已用去四秒。
    message = {"id": "one", "op": "query", "epoch": "current", "symbols": [ROW.key], "timeout": 5, "deadline": 105}
    now[0] = 104
    pipe._handle(message)
    assert calls == [1]
    now[0] = 106
    pipe._handle({**message, "id": "expired"})
    assert calls == [1], "expired request must not start source work"
    assert sent[-1]["reason_code"] == "CATALOG.TIMEOUT"


def test_native_timeout_keeps_timeout_classification_across_ipc(monkeypatch):
    def timeout(symbols, seconds):
        raise TimeoutError("native query budget expired")

    with real_catalog_api(monkeypatch, timeout) as (api, _, _, _):
        response = api.post("/instrument-catalog/query", json={"account_id": 1, "symbols": [ROW.key]})
        assert response.status_code == 408
        assert response.json()["detail"]["causes"][0]["reason_code"] == "CATALOG.TIMEOUT"


def test_uncached_followup_exposes_broken_catalog_pipe_even_with_cached_normal_contract(monkeypatch):
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        return [] if symbols == ["SHFE.missing"] else [ROW]

    with real_catalog_api(monkeypatch, query) as (api, manager, provider, handle):
        normal = {"account_id": 1, "symbols": [ROW.key]}
        assert api.post("/instrument-catalog/query", json=normal).status_code == 200
        negative = api.post(
            "/instrument-catalog/query", json={**normal, "symbols": ["SHFE.missing"], "use_cache": False}
        )
        assert negative.status_code == 404
        assert calls == [[ROW.key], ["SHFE.missing"]]
        original_source = handle.catalog.source
        attempted = []

        def broken_request(*args, **kwargs):
            attempted.append(args)
            raise CatalogError("catalog IPC unavailable", "CATALOG.UNAVAILABLE")

        monkeypatch.setattr(handle.catalog, "_request", broken_request)
        # 正常缓存入口仍成功，不能作为原目录通道健康的证明。
        assert api.post("/instrument-catalog/query", json=normal).status_code == 200
        assert not attempted
        response = api.post("/instrument-catalog/query", json={**normal, "use_cache": False})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "CATALOG.UNAVAILABLE"
        assert response.json()["detail"]["causes"][0]["message"] == "catalog IPC unavailable"
        assert len(attempted) == 1
        assert manager._workers[1] is handle and handle.catalog.source == original_source
        assert not provider.failed
        assert manager.instrument_catalog.snapshot()[ROW.key] == ROW


def test_uncached_query_uses_original_source_and_does_not_replace_shared_cache(monkeypatch):
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        return [ROW if len(calls) == 1 else replace(ROW, tick=2)]

    with real_catalog_api(monkeypatch, query) as (api, manager, _, handle):
        normal = {"account_id": 1, "symbols": [ROW.key]}
        assert api.post("/instrument-catalog/query", json=normal).json()["data"][ROW.key]["tick"] == 1
        # 不允许用别的可用账户来源替代原会话。
        manager.instrument_catalog.register("other", lambda *args: pytest.fail("wrong source"), channel="ctp")
        handle.catalog.configure(channel="ctp", enabled=False, last_execution="")
        response = api.post("/instrument-catalog/query", json={**normal, "use_cache": False})
        assert response.status_code == 200
        assert response.json()["data"][ROW.key]["tick"] == 2
        assert calls == [[ROW.key], [ROW.key]]
        assert manager.instrument_catalog.snapshot()[ROW.key] == ROW
