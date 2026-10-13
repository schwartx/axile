"""真实 spawn 进程下的双向目录查询、来源退出和响应隔离。"""

import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from axile.executor.instrument_catalog import CatalogLimits, InstrumentCatalog
from axile.executor.instrument_catalog_records import Instrument
from axile.server.execution.worker_backend.catalog import (
    CatalogSession,
    RemoteCatalogProvider,
)

pytestmark = pytest.mark.slow
ROW = Instrument("SHFE", "rb2701", "FUTURE", 10, 1)


def _worker(connection, control, queries, tick):
    provider = RemoteCatalogProvider(connection)

    def query(symbols, timeout):
        queries.put(symbols)
        return [Instrument("SHFE", "rb2701", "FUTURE", 10, tick)]

    provider.bind(query)
    control.send("ready")
    try:
        while True:
            op = control.recv()
            if op == "stop":
                return
            control.send(provider.ensure([ROW.key])[ROW.key].tick)
    finally:
        provider.stop()


def test_spawn_workers_share_one_service_catalog_and_reuse_after_owner_exit():
    ctx = multiprocessing.get_context("spawn")
    catalog = InstrumentCatalog()
    queries = ctx.Queue()
    processes, sessions, controls = [], [], []
    try:
        for tick in (1, 2):
            parent, child = ctx.Pipe()
            control, worker_control = ctx.Pipe()
            process = ctx.Process(target=_worker, args=(child, worker_control, queries, tick))
            process.start()
            child.close()
            worker_control.close()
            sessions.append(CatalogSession(parent, catalog))
            processes.append(process)
            controls.append(control)
            assert control.poll(10)
            assert control.recv() == "ready"
        for control in controls:
            control.send("read")
        for control in controls:
            assert control.poll(5)
            assert control.recv() == 1
        assert queries.get(timeout=2) == [ROW.key]
        assert queries.empty()
        processes[0].terminate()
        processes[0].join(5)
        controls[1].send("read")
        assert controls[1].poll(5)
        assert controls[1].recv() == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        for session in sessions:
            session.stop()
        for control in controls:
            control.close()
        queries.close()


def test_late_response_is_discarded_without_desynchronizing_next_rpc():
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    catalog = InstrumentCatalog(CatalogLimits(demand_source=0.02, ensure=0.5))
    session = CatalogSession(parent, catalog)
    provider = RemoteCatalogProvider(child)
    release = threading.Event()

    def query(symbols, timeout):
        release.wait(1)
        return [ROW]

    provider.bind(query)
    try:
        with pytest.raises(Exception, match="超时"):
            provider.ensure([ROW.key], timeout=0.01)
        assert not provider.failed
        release.set()
        # The timed-out RPC response may arrive before or after this request.
        provider.close()
        provider.bind(lambda symbols, timeout: [ROW])
        assert provider.ensure([ROW.key])[ROW.key] == ROW
    finally:
        provider.stop()
        session.stop()


def test_closing_source_unblocks_query_and_switches_to_next_source():
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    catalog = InstrumentCatalog(CatalogLimits(demand_source=1, ensure=2))
    session = CatalogSession(parent, catalog)
    provider = RemoteCatalogProvider(child)
    entered, release = threading.Event(), threading.Event()

    def query(symbols, timeout):
        entered.set()
        release.wait(2)
        return [Instrument("SHFE", "rb2701", "FUTURE", 10, 9)]

    provider.bind(query)
    catalog.register("replacement", lambda symbols, timeout: [ROW])
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(catalog.ensure, [ROW.key])
            assert entered.wait(1)
            provider.stop()
            assert provider.failed
            assert pending.result(2)[ROW.key].tick == 1
        release.set()
        time.sleep(0.02)
        assert catalog.snapshot()[ROW.key].tick == 1
    finally:
        release.set()
        provider.stop()
        session.stop()


def test_query_and_initialization_progress_share_one_pipe_without_deadlock(monkeypatch):
    parent, child = multiprocessing.Pipe()
    catalog = InstrumentCatalog()
    session = CatalogSession(parent, catalog)
    provider = RemoteCatalogProvider(child)
    now = [10.0]
    monkeypatch.setattr("axile.server.execution.worker_backend.catalog.clock_monotonic", lambda: now[0])

    def query(symbols, timeout):
        provider.progress("合约资料")
        return [ROW]

    try:
        provider.request_id = "current"
        provider.begin()
        provider.bind(query)
        now[0] = 20.0
        assert provider.ensure([ROW.key])[ROW.key] == ROW
        assert session.snapshot("current") == (True, 20.0, 10.0, 0.0)
        now[0] = 30.0
        assert provider.ensure([ROW.key])[ROW.key] == ROW
        assert session.snapshot("current")[1] == 20.0
        provider.request_id = "stale"
        provider.progress("迟到进度")
        provider.end()
        assert session.snapshot("current")[0:2] == (True, 20.0)
        provider.request_id = "current"
        provider.end()
        assert session.snapshot("current") == (False, 30.0, 10.0, 30.0)
    finally:
        provider.stop()
        session.stop()


def test_paused_source_is_excluded_and_resume_reuses_logged_in_session():
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    catalog = InstrumentCatalog()
    session = CatalogSession(parent, catalog, enabled=False)
    provider = RemoteCatalogProvider(child)
    calls = []
    provider.bind(lambda symbols, timeout: calls.append(symbols) or [ROW], channel="ctp")
    try:
        assert catalog.status()["sources"] == 0
        with pytest.raises(Exception, match="没有可用"):
            provider.ensure([ROW.key], require_specs=True)
        assert not calls
        session.configure(channel="ctp", enabled=True, last_execution="2026-10-13T21:00:00")
        assert provider.ensure([ROW.key], require_specs=True)[ROW.key] == ROW
        assert calls == [[ROW.key]]
        session.configure(channel="ctp", enabled=False, last_execution="2026-10-13T21:00:00")
        assert catalog.status()["sources"] == 0
        assert catalog.snapshot()[ROW.key] == ROW
    finally:
        provider.stop()
        session.stop()


def test_ctp_specs_request_crosses_ipc_and_skips_code_only_tq_source():
    ctx = multiprocessing.get_context("spawn")
    catalog = InstrumentCatalog()
    pairs = [ctx.Pipe(), ctx.Pipe()]
    sessions = [CatalogSession(parent, catalog) for parent, _ in pairs]
    providers = [RemoteCatalogProvider(child) for _, child in pairs]
    calls = []
    try:
        providers[0].bind(lambda symbols, timeout: [Instrument(ROW.exchange, ROW.symbol, ROW.kind)], channel="tq")
        assert not providers[0].ensure([ROW.key])[ROW.key].has_specs
        providers[1].bind(lambda symbols, timeout: calls.append(symbols) or [ROW], channel="ctp")
        assert providers[1].ensure([ROW.key], require_specs=True)[ROW.key] == ROW
        assert calls == [[ROW.key]]
    finally:
        for provider in providers:
            provider.stop()
        for session in sessions:
            session.stop()
