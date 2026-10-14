from __future__ import annotations

import asyncio
import threading
import time
from collections import UserDict
from collections.abc import Coroutine
from typing import Any

import pytest

from axile.executor.tq.runtime import TQRuntime
from tests.unit.executor.tq._catalog_test_support import CatalogApiMixin


class FakeApi(CatalogApiMixin):
    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.query_count = 0
        self.wait_count = 0
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None

    def create_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        return self.loop.create_task(coroutine)

    def wait_update(self, *, deadline: float) -> bool:
        del deadline
        if self.loop is not None:
            self.loop.run_until_complete(asyncio.sleep(0.001))
        self.wait_count += 1
        return False

    def is_changing(self, _entity: object) -> bool:
        return False

    def get_order(self) -> dict[str, object]:
        return {}

    def get_trade(self) -> dict[str, object]:
        return {}

    def close(self) -> None:
        if self.loop is not None:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()
            asyncio.set_event_loop(None)
        self.closed = True


def test_runtime_owns_api_and_queries_catalog_only_on_demand() -> None:
    api: FakeApi | None = None

    def factory() -> FakeApi:
        nonlocal api
        api = FakeApi()
        return api

    runtime = TQRuntime(factory)
    try:
        owner = runtime.call(lambda _api: threading.get_ident())
        assert api is not None
        assert owner == api.owner
        assert owner != threading.get_ident()
        assert api.query_count == 0
        assert runtime.resolver.to_tq("rb2610") == "SHFE.rb2610"
        assert runtime.resolver.to_tq("KQ.m@SHFE.rb") == "KQ.m@SHFE.rb"
        with pytest.raises(ValueError, match="仅支持行情查询"):
            runtime.resolver.to_tq("KQ.m@SHFE.rb", for_trade=True)
        assert api.query_count == 3
        assert api.wait_count >= 1
    finally:
        runtime.close()

    assert api is not None and api.closed is True


def test_full_catalog_only_queries_codes_and_does_not_fetch_specs():
    api = FakeApi()
    api.options = ["DCE.m2701-C-3000"]
    api.combines = ["DCE.SP m2701&m2705"]
    api.futures = ["SHFE.rb2610", "HKEX.HSI2701"]
    # SDK 详情接口或私有 GraphQL 不应再进入调用链。
    api.query_symbol_info = lambda *_args, **_kwargs: pytest.fail("must not query full metadata")
    api._get_symbol_list = lambda *_args, **_kwargs: pytest.fail("must not use private GraphQL")
    runtime = TQRuntime(lambda: api)
    try:
        rows = runtime.query_catalog(None, 1)
        assert {(r.key, r.kind) for r in rows} == {
            ("SHFE.rb2610", "FUTURE"),
            ("DCE.m2701-C-3000", "OPTION"),
            ("DCE.SP m2701&m2705", "COMBINE"),
        }
        assert all(r.multiplier is None and r.tick is None and not r.extra for r in rows)
        assert api.query_count == 3
    finally:
        runtime.close()


def test_failed_code_query_propagates_without_partial_full_result():
    api = FakeApi()
    query_quotes = api.query_quotes

    def query(**kwargs):
        if kwargs["ins_class"] == "OPTION":
            raise RuntimeError("query failed")
        return query_quotes(**kwargs)

    api.query_quotes = query
    runtime = TQRuntime(lambda: api)
    try:
        with pytest.raises(RuntimeError, match="query failed"):
            runtime.query_catalog(None, 1)
    finally:
        runtime.close()


def test_pending_command_timeout_cancels_before_execution() -> None:
    runtime = TQRuntime(FakeApi)
    started = threading.Event()
    release = threading.Event()
    first_errors: list[BaseException] = []

    def block_owner(_api: object) -> None:
        started.set()
        release.wait(1)

    def run_first() -> None:
        try:
            runtime.call(block_owner)
        except BaseException as exc:  # noqa: BLE001 - 线程断言需要带回主线程
            first_errors.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    try:
        assert started.wait(1)
        executed = threading.Event()
        with pytest.raises(TimeoutError, match="命令排队超时"):
            runtime.call(lambda _api: executed.set(), timeout=0.01)
        release.set()
        worker.join(1)
        runtime.call(lambda _api: None)
        assert not worker.is_alive()
        assert first_errors == []
        assert not executed.is_set()
    finally:
        release.set()
        worker.join(1)
        runtime.close()


def test_running_mutation_waits_for_one_determinate_result() -> None:
    runtime = TQRuntime(FakeApi)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    results: list[str] = []
    errors: list[BaseException] = []

    def mutation(_api: object) -> str:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        return "order-1"

    def invoke() -> None:
        try:
            results.append(runtime.call(mutation, timeout=0.01))
        except BaseException as exc:  # noqa: BLE001 - 线程断言需要带回主线程
            errors.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert started.wait(1)
        time.sleep(0.03)
        assert worker.is_alive()
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert errors == []
        assert results == ["order-1"]
        assert calls == 1
    finally:
        release.set()
        worker.join(1)
        runtime.close()


def test_operation_result_does_not_wait_for_next_update() -> None:
    class BlockingPumpApi(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.block_pump = False
            self.release_pump = threading.Event()

        def wait_update(self, *, deadline: float) -> bool:
            super().wait_update(deadline=deadline)
            if self.block_pump:
                self.release_pump.wait(1)
            return False

    api = BlockingPumpApi()
    runtime = TQRuntime(lambda: api, command_timeout=0.05)
    try:

        def operation(_api: object) -> str:
            api.block_pump = True
            return "done"

        assert runtime.call(operation) == "done"
    finally:
        api.release_pump.set()
        runtime.close()


def test_runtime_propagates_operation_error() -> None:
    runtime = TQRuntime(FakeApi)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            runtime.call(lambda _api: (_ for _ in ()).throw(ValueError("boom")))
    finally:
        runtime.close()


def test_runtime_emits_quote_snapshots() -> None:
    class ChangingApi(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.quote = {"instrument_id": "rb2610", "exchange_id": "SHFE", "last_price": 3200}
            self.changed = False

        async def get_quote(self, _symbol: str) -> dict[str, object]:
            return self.quote

        def is_changing(self, entity: object) -> bool:
            return self.changed and entity is self.quote

    api = ChangingApi()
    runtime = TQRuntime(lambda: api)
    events: list[tuple[str, dict[str, object]]] = []
    runtime.add_listener(lambda kind, row: events.append((kind, row)))
    try:
        runtime.quote_snapshot("SHFE.rb2610")
        runtime.call(lambda _api: setattr(api, "changed", True))
        assert events[-1] == (
            "quote",
            {"instrument_id": "rb2610", "exchange_id": "SHFE", "last_price": 3200},
        )
        api.quote["last_price"] = 3300
        assert events[-1][1]["last_price"] == 3200
    finally:
        runtime.close()


def test_runtime_propagates_wait_update_and_close_errors() -> None:
    class BrokenPumpApi(FakeApi):
        fail = False

        def wait_update(self, *, deadline: float) -> bool:
            del deadline
            if self.fail:
                raise OSError("pump failed")
            return False

    runtime = TQRuntime(BrokenPumpApi)
    assert runtime.call(lambda api: setattr(api, "fail", True)) is None
    deadline = time.monotonic() + 1
    while runtime.is_alive() and time.monotonic() < deadline:
        time.sleep(0.001)
    with pytest.raises(RuntimeError, match="异常停止"):
        runtime.call(lambda _api: None)
    runtime.close()

    class BrokenCloseApi(FakeApi):
        def close(self) -> None:
            raise OSError("close failed")

    runtime = TQRuntime(BrokenCloseApi)
    # Ensure the event pump has entered its steady state before closing it.
    deadline = time.monotonic() + 1
    while runtime.call(lambda api: getattr(api, "wait_count")) < 1 and time.monotonic() < deadline:
        pass
    with pytest.raises(RuntimeError, match="close failed"):
        runtime.close()


class AsyncQuoteApi(FakeApi):
    """只允许在 SDK 事件循环内取价，可独立挂起、失败和更新合约。"""

    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, asyncio.Future] = {}
        self.calls: list[str] = []
        self.quotes: dict[str, dict[str, object]] = {}
        self.changed: set[str] = set()
        self.trades: dict[str, object] = {}
        self.fail_pump = False
        self.tasks: list[asyncio.Task] = []

    def create_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = super().create_task(coroutine)
        self.tasks.append(task)
        return task

    def get_quote(self, symbol: str) -> asyncio.Future:
        assert asyncio.get_running_loop() is self.loop
        self.calls.append(symbol)
        future = asyncio.get_running_loop().create_future()
        self.requests[symbol] = future
        return future

    def deliver(self, symbol: str, price: int = 3200) -> None:
        row = {
            "instrument_id": symbol,
            "last_price": price,
            "datetime": "2026-09-01 09:00:00",
            "nested": UserDict({"x": 1, "_api": threading.Lock()}),
        }
        if symbol in self.quotes:
            self.quotes[symbol].update(row)
            self.changed.add(symbol)
        else:
            self.quotes[symbol] = row
            self.requests[symbol].set_result(row)

    def is_changing(self, entity: object) -> bool:
        for symbol in tuple(self.changed):
            if entity is self.quotes[symbol]:
                self.changed.remove(symbol)
                return True
        return False

    def get_trade(self) -> dict[str, object]:
        return self.trades

    def wait_update(self, *, deadline: float) -> bool:
        if self.fail_pump:
            raise OSError("pump failed")
        return super().wait_update(deadline=deadline)


@pytest.fixture
def quote_runtime():
    runtime = TQRuntime(AsyncQuoteApi)
    try:
        yield runtime
    finally:
        runtime.close()


def test_pending_quote_does_not_block_quotes_queries_or_trade_callbacks(quote_runtime: TQRuntime) -> None:
    from concurrent.futures import ThreadPoolExecutor

    runtime = quote_runtime
    trade_received = threading.Event()
    runtime.add_listener(lambda kind, _row: trade_received.set() if kind == "trade" else None)
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(runtime.quote_snapshots, ["slow", "ready"], timeout=0.5)
        runtime.subscribe(["slow", "ready"])
        runtime.call(lambda api: getattr(api, "deliver")("ready"))
        assert runtime.quote_snapshot("ready", timeout=0.1)["last_price"] == 3200
        assert runtime.call(lambda api: getattr(api, "get_order")(), timeout=0.1) == {}
        runtime.call(lambda api: getattr(api, "trades").update({"t1": {"trade_id": "t1"}}))
        assert trade_received.wait(0.1)
        assert not waiting.done()
        assert set(waiting.result(timeout=1)) == {"ready"}


def test_batch_deadline_and_late_recovery_reuse_subscription(quote_runtime: TQRuntime, caplog) -> None:
    runtime = quote_runtime
    caplog.set_level("INFO")
    symbols = [f"slow{i}" for i in range(8)]
    started = time.monotonic()
    assert runtime.quote_snapshots(symbols, timeout=0.1) == {}
    assert time.monotonic() - started < 0.4
    assert runtime.quote_snapshots(symbols, timeout=0.01) == {}
    with pytest.raises(TimeoutError, match="slow0"):
        runtime.quote_snapshot("slow0", timeout=0.01)
    assert len([r for r in caplog.records if "首次行情订阅超时" in r.message]) == len(symbols)
    runtime.call(lambda api: getattr(api, "deliver")("slow0"))
    assert runtime.quote_snapshot("slow0", timeout=0.1)["last_price"] == 3200
    assert runtime.call(lambda api: list(getattr(api, "calls"))) == symbols
    assert any("订阅恢复 slow0" in r.message for r in caplog.records)


def test_concurrent_reads_share_task_and_publish_isolated_snapshots(quote_runtime: TQRuntime) -> None:
    from concurrent.futures import ThreadPoolExecutor

    runtime = quote_runtime
    received = threading.Event()
    events: list[dict[str, object]] = []

    def listener(kind: str, row: dict[str, object]) -> None:
        if kind == "quote":
            events.append(row)
            received.set()

    runtime.add_listener(listener)
    runtime.subscribe(["one"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        reads = [pool.submit(runtime.quote_snapshot, "one", timeout=1) for _ in range(6)]
        runtime.call(lambda api: getattr(api, "deliver")("one"))
        rows = [read.result(timeout=1) for read in reads]
    assert received.wait(0.1)
    assert len(events) == 1  # 首份快照不依赖 is_changing。
    rows[0]["nested"]["x"] = 99
    events[0]["nested"]["x"] = 88
    assert type(rows[1]["nested"]) is dict
    assert rows[1]["nested"] == {"x": 1}
    assert runtime.quote_snapshot("one")["nested"] == {"x": 1}
    received.clear()
    runtime.call(lambda api: getattr(api, "deliver")("one", 3300))
    assert received.wait(0.1)
    updated = runtime.quote_snapshot("one", timeout=0)
    assert updated["last_price"] == 3300
    assert updated["datetime"] == rows[1]["datetime"]
    assert runtime.call(lambda api: list(getattr(api, "calls"))) == ["one"]


def test_subscription_error_is_isolated_and_not_retried(quote_runtime: TQRuntime, caplog) -> None:
    runtime = quote_runtime
    runtime.subscribe(["bad", "good"])
    runtime.call(lambda api: getattr(api, "requests")["bad"].set_exception(ValueError("invalid symbol")))
    runtime.call(lambda api: getattr(api, "deliver")("good"))
    assert set(runtime.quote_snapshots(["bad", "good"], timeout=0.1)) == {"good"}
    for _ in range(2):
        with pytest.raises(RuntimeError, match="bad.*invalid symbol"):
            runtime.quote_snapshot("bad")
    assert runtime.call(lambda api: list(getattr(api, "calls"))) == ["bad", "good"]
    assert len([r for r in caplog.records if "行情订阅失败 bad" in r.message]) == 1


@pytest.mark.parametrize("pump_failure", [False, True])
def test_shutdown_cancels_subscription_and_wakes_readers(quote_runtime: TQRuntime, pump_failure: bool) -> None:
    from concurrent.futures import ThreadPoolExecutor

    runtime = quote_runtime
    runtime.subscribe(["slow"])
    tasks = runtime.call(lambda api: list(getattr(api, "tasks")))
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(runtime.quote_snapshot, "slow", timeout=10)
        if pump_failure:
            runtime.call(lambda api: setattr(api, "fail_pump", True))
        else:
            runtime.close()
        with pytest.raises(RuntimeError, match="停止"):
            waiting.result(timeout=0.5)
    runtime._thread.join(timeout=1)
    assert all(task.cancelled() for task in tasks)


def test_cached_snapshot_does_not_queue_behind_owner(quote_runtime: TQRuntime) -> None:
    from concurrent.futures import ThreadPoolExecutor

    runtime = quote_runtime
    runtime.subscribe(["ready"])
    runtime.call(lambda api: getattr(api, "deliver")("ready"))
    assert runtime.quote_snapshot("ready", timeout=0.1)["last_price"] == 3200
    started = threading.Event()
    release = threading.Event()

    def block_owner(_api: object) -> None:
        started.set()
        release.wait(1)

    with ThreadPoolExecutor() as pool:
        blocking = pool.submit(runtime.call, block_owner)
        try:
            assert started.wait(0.5)
            cached = pool.submit(runtime.quote_snapshot, "ready", timeout=0)
            assert cached.result(timeout=0.1)["last_price"] == 3200
            assert not blocking.done()
        finally:
            release.set()
        blocking.result(timeout=1)
