"""共享目录、替换发布和下单前边界的行为测试。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.instrument_catalog import (
    CatalogError,
    CatalogLimits,
    InstrumentCatalog,
    LocalInstrumentCatalogProvider,
)
from axile.executor.instrument_catalog_records import Instrument, from_ctp

RB = Instrument("SHFE", "rb2701", "FUTURE", 10, 1)
AG = Instrument("SHFE", "ag2701", "FUTURE", 15, 1)


def finish(catalog, task):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = catalog.task(task["task_id"])
        if status["status"] != "running":
            return status
        time.sleep(0.005)
    pytest.fail("refresh did not finish")


def test_concurrent_waiters_share_one_query_and_cancel_is_isolated():
    catalog = InstrumentCatalog()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        entered.set()
        assert release.wait(1)
        return [RB]

    catalog.register("ctp", query)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(catalog.ensure, [RB.key])
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="cancel"):
            catalog.ensure([RB.key], lambda: (_ for _ in ()).throw(RuntimeError("cancel")))
        second = pool.submit(catalog.ensure, [RB.key])
        release.set()
        assert first.result(1)[RB.key] == second.result(1)[RB.key] == RB
    assert calls == [[RB.key]]


def test_successful_full_refresh_replaces_even_concurrent_demand_rows():
    catalog = InstrumentCatalog()
    entered, release = threading.Event(), threading.Event()

    def query(symbols, timeout):
        if symbols is None:
            entered.set()
            assert release.wait(1)
            return [replace(RB, tick=2)]
        return [AG] if symbols == [AG.key] else [RB]

    catalog.register("tq", query)
    frozen = catalog.ensure([RB.key])
    task = catalog.refresh()
    assert entered.wait(1)
    assert catalog.refresh()["task_id"] == task["task_id"]
    assert catalog.ensure([RB.key])[RB.key].tick == 1
    assert catalog.ensure([AG.key])[AG.key] == AG
    release.set()
    assert finish(catalog, task)["status"] == "succeeded"
    assert set(catalog.snapshot()) == {RB.key}
    assert catalog.snapshot()[RB.key].tick == 2
    assert frozen[RB.key].tick == 1
    assert catalog.ensure([AG.key])[AG.key] == AG
    assert "version" not in catalog.status()


@pytest.mark.parametrize("bad", [[], [replace(RB, tick=0)], [replace(RB, multiplier=float("nan"))]])
def test_invalid_full_result_never_replaces_cache(bad):
    catalog = InstrumentCatalog()
    catalog.register("good", lambda symbols, timeout: [RB])
    catalog.ensure([RB.key])
    catalog.unregister("good")
    catalog.register("bad", lambda symbols, timeout: bad)
    assert finish(catalog, catalog.refresh())["status"] == "failed"
    assert catalog.snapshot() == {RB.key: RB}


def test_timed_out_source_late_result_cannot_overwrite_replacement():
    catalog = InstrumentCatalog(CatalogLimits(demand_source=0.02, source=0.02, ensure=0.2))
    release = threading.Event()

    def slow(symbols, timeout):
        release.wait(1)
        return [replace(RB, tick=9)]

    catalog.register("dead", slow)
    catalog.register("available", lambda symbols, timeout: [RB])
    assert catalog.ensure([RB.key])[RB.key] == RB
    release.set()
    time.sleep(0.02)
    assert catalog.snapshot()[RB.key].tick == 1


def test_empty_cache_demand_never_requests_full_and_rebuild_reuses_results():
    catalog = InstrumentCatalog()
    calls = []
    first = LocalInstrumentCatalogProvider(catalog)
    first.bind(lambda symbols, timeout: calls.append(symbols) or [RB])
    assert first.ensure([RB.key])[RB.key] == RB
    first.close()
    second = LocalInstrumentCatalogProvider(catalog)
    second.bind(lambda symbols, timeout: pytest.fail("must use cache"))
    assert second.ensure([RB.key])[RB.key] == RB
    assert calls == [[RB.key]]
    with pytest.raises(CatalogError):
        InstrumentCatalog().ensure([RB.key])


def test_execution_fills_missing_specs_using_its_own_account_even_with_a_preferred_source():
    catalog = InstrumentCatalog()
    calls = []
    catalog.register(
        "recent-ctp",
        lambda symbols, timeout: pytest.fail("execution must query its own account"),
        channel="ctp",
        last_execution="2026-10-14T09:00:00",
    )
    codes = LocalInstrumentCatalogProvider(catalog)
    codes.bind(lambda symbols, timeout: [Instrument(RB.exchange, RB.symbol, RB.kind)], channel="tq")
    assert not codes.ensure([RB.key])[RB.key].has_specs
    executor = LocalInstrumentCatalogProvider(catalog)
    executor.bind(lambda symbols, timeout: calls.append(symbols) or [RB], channel="ctp")
    assert executor.ensure([RB.key], require_specs=True)[RB.key] == RB
    assert executor.ensure([RB.key], require_specs=True)[RB.key] == RB
    assert calls == [[RB.key]]


def test_paused_session_does_not_trigger_preheating_or_supply_full_refresh():
    catalog = InstrumentCatalog()
    calls = []
    catalog.arm_refresh("day", time.time() + 1)
    catalog.register("paused", lambda symbols, timeout: calls.append(symbols) or [RB], channel="ctp", enabled=False)
    assert catalog.status()["current_task"] is None
    assert catalog.status()["sources"] == 0
    assert catalog.ensure([RB.key], require_specs=True, source_name="paused")[RB.key] == RB
    assert finish(catalog, catalog.refresh())["status"] == "failed"
    assert calls == [[RB.key]]
    assert catalog.snapshot() == {RB.key: RB}


@pytest.mark.parametrize("change", ["pause", "rebind", "disconnect"])
def test_refresh_accepts_paused_source_result_but_rejects_invalidated_sessions(change):
    catalog = InstrumentCatalog()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        entered.set()
        assert release.wait(1)
        return [RB]

    catalog.register("account", query, channel="ctp")
    task = catalog.refresh()
    assert entered.wait(1)
    if change == "pause":
        catalog.register("account", query, channel="ctp", enabled=False)
    elif change == "rebind":
        catalog.register("account", lambda symbols, timeout: [AG], channel="ctp", enabled=False)
    else:
        catalog.unregister("account")
    release.set()
    assert finish(catalog, task)["status"] == ("succeeded" if change == "pause" else "failed")
    expected = {RB.key: RB} if change == "pause" else {}
    if change == "pause":
        assert catalog.ensure([RB.key], require_specs=True)[RB.key] == RB
    assert finish(catalog, catalog.refresh())["status"] == "failed"
    assert catalog.snapshot() == expected
    assert calls == [None]


def test_waiter_timeout_never_restarts_execution_on_late_result():
    catalog = InstrumentCatalog()
    release = threading.Event()
    catalog.register("source", lambda symbols, timeout: (release.wait(1), [RB])[1])
    with pytest.raises(CatalogError, match="超时"):
        catalog.ensure([RB.key], timeout=0.01)
    release.set()
    assert catalog.ensure([RB.key])[RB.key] == RB


def test_automatic_refresh_waits_for_first_source_and_coalesces_session():
    catalog = InstrumentCatalog()
    calls = []
    catalog.arm_refresh("day", time.time() + 1)
    assert catalog.status()["current_task"] is None
    catalog.register("one", lambda symbols, timeout: calls.append(symbols) or [RB])
    deadline = time.monotonic() + 1
    while catalog.status()["last_success_at"] is None and time.monotonic() < deadline:
        time.sleep(0.005)
    catalog.register("two", lambda symbols, timeout: pytest.fail("same session must not reload"))
    assert catalog.refresh("day")["status"] == "skipped"
    assert calls == [None]


def test_ctp_delisting_uses_lifecycle_and_end_of_expiry_day():
    row = {
        "InstrumentID": RB.symbol,
        "ExchangeID": RB.exchange,
        "ProductClass": "1",
        "VolumeMultiple": 10,
        "PriceTick": 1,
        "IsTrading": False,
        "ExpireDate": clock_now(tz=ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"),
    }
    assert from_ctp(row) is not None
    assert from_ctp({**row, "InstLifePhase": "3"}) is None
    assert from_ctp({**row, "ExpireDate": "20000101"}) is None


def test_code_only_records_are_valid_but_ctp_specs_are_required_separately():
    record = Instrument("SHFE", "rb2701", "FUTURE")
    record.validate()
    assert not record.has_specs
    with pytest.raises(ValueError, match="规格缺失"):
        record.validate(require_specs=True)
    assert Instrument.from_wire(record.wire()) == record


def test_ctp_enriches_code_only_cache_without_calling_tq_for_specs():
    catalog = InstrumentCatalog()
    catalog.register("tq", lambda symbols, timeout: [Instrument("SHFE", "rb2701", "FUTURE")], channel="tq")
    assert not catalog.ensure([RB.key])[RB.key].has_specs
    calls = []
    catalog.register("ctp", lambda symbols, timeout: calls.append(symbols) or [RB], channel="ctp")
    assert catalog.ensure([RB.key], require_specs=True)[RB.key] == RB
    assert catalog.ensure([RB.key], require_specs=True)[RB.key] == RB
    assert calls == [[RB.key]]
    catalog.unregister("ctp")
    with pytest.raises(CatalogError, match="没有可用"):
        catalog.ensure([AG.key], require_specs=True)


def test_refresh_prefers_ctp_then_most_recent_execution_and_fails_over():
    catalog = InstrumentCatalog()
    calls = []

    def query(name, fail=False):
        def load(symbols, timeout):
            calls.append(name)
            if fail:
                raise RuntimeError("unavailable")
            return [RB]

        return load

    catalog.register("tq-new", query("tq-new"), channel="tq", last_execution="2026-10-13T21:20:00")
    catalog.register("ctp-old", query("ctp-old"), channel="ctp", last_execution="2026-10-13T20:00:00")
    catalog.register("ctp-new", query("ctp-new", True), channel="ctp", last_execution="2026-10-13T21:00:00")
    assert finish(catalog, catalog.refresh())["status"] == "succeeded"
    assert calls == ["ctp-new", "ctp-old"]
    catalog.unregister("ctp-old")
    catalog.register("tq-old", query("tq-old"), channel="tq", last_execution="2026-10-13T19:00:00")
    calls.clear()
    assert finish(catalog, catalog.refresh())["status"] == "succeeded"
    assert calls == ["ctp-new", "tq-new"]


def test_catalog_and_execution_guards_share_send_interval_but_keep_separate_records():
    from axile.common.trade_channel import TradeChannel
    from axile.executor.account_control.decorators import run_controlled_call
    from axile.executor.account_control.guard import AccountControlGuard
    from axile.executor.account_control.presets import resolve_account_control_policy
    from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
    from tests.unit.executor.test_account_control_guard import _AdvancingClock

    clock = _AdvancingClock(datetime(2026, 10, 13, 8, 30))
    timestamps, lock = {}, threading.RLock()
    guards = [
        AccountControlGuard(
            account_id=1,
            execution_id=task,
            channel=TradeChannel.CTP,
            policy=resolve_account_control_policy("ctp"),
            baseline=AccountControlCounterSnapshot(),
            wait_clock=clock,
        )
        for task in ("execution", "catalog")
    ]
    sent = []
    for guard in guards:
        guard.share_group_timestamps(timestamps, lock)
        run_controlled_call(guard=guard, operation="query_instruments", call=lambda: sent.append(clock.time()))
    assert sent[1] - sent[0] >= 1.5
    for guard in guards:
        _, events = guard.flush_records()
        assert len(events) == 1
        assert events[0].execution_id == guard.execution_id


def test_bare_and_exchange_qualified_waiters_share_the_same_missing_contract():
    catalog = InstrumentCatalog()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        entered.set()
        assert release.wait(1)
        return [RB]

    catalog.register("source", query)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(catalog.ensure, [RB.symbol])
        assert entered.wait(1)
        second = pool.submit(catalog.ensure, [RB.key])
        release.set()
        assert first.result(1)[RB.symbol] == second.result(1)[RB.key] == RB
    assert len(calls) == 1


def test_missing_list_is_one_source_call_and_overlapping_waiter_reuses_it():
    catalog = InstrumentCatalog()
    entered, joined, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def query(symbols, timeout):
        calls.append(symbols)
        entered.set()
        assert release.wait(2)
        return [RB, AG]

    catalog.register("tq", query)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(catalog.ensure, [RB.key, AG.key])
        assert entered.wait(1)
        second = pool.submit(catalog.ensure, [AG.key], joined.set)
        assert joined.wait(1)
        release.set()
        assert first.result(1) == {RB.key: RB, AG.key: AG}
        assert second.result(1) == {AG.key: AG}
    assert calls == [[RB.key, AG.key]]


def test_consumer_cannot_mutate_cached_records_or_the_native_view():
    row = replace(RB, extra={"ProductID": "rb"})
    catalog = InstrumentCatalog()
    catalog.register("ctp", lambda symbols, timeout: [row])
    snapshot = catalog.ensure([RB.key])
    snapshot.clear()
    with pytest.raises(TypeError):
        row.extra["ProductID"] = "other"
    with pytest.raises(TypeError):
        row.ctp().values["PriceTick"] = 9
    assert catalog.ensure([RB.key])[RB.key].tick == 1


def test_successful_tq_code_refresh_replaces_specs_and_failed_refresh_keeps_codes():
    catalog = InstrumentCatalog()
    catalog.register("ctp", lambda symbols, timeout: [RB], channel="ctp")
    catalog.ensure([RB.key], require_specs=True)
    catalog.unregister("ctp")
    codes = Instrument(RB.exchange, RB.symbol, RB.kind)
    catalog.register("tq", lambda symbols, timeout: [codes], channel="tq")
    assert finish(catalog, catalog.refresh())["status"] == "succeeded"
    assert catalog.snapshot() == {RB.key: codes}
    catalog.register("tq", lambda symbols, timeout: [], channel="tq")
    assert finish(catalog, catalog.refresh())["status"] == "failed"
    assert catalog.snapshot() == {RB.key: codes}
