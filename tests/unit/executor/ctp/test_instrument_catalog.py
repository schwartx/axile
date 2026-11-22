"""全量只发一次请求；按需查询必须指定合约。"""

from contextlib import contextmanager
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openctp_ctp import thosttraderapi as td

from axile.common.serialization import from_payload, to_payload
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.execution_session import ExecutionSession
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.instrument_catalog import (
    CatalogError,
    CatalogLimits,
    InstrumentCatalog,
    LocalInstrumentCatalogProvider,
    catalog_query_scope,
)
from axile.executor.instrument_catalog_records import Instrument
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from tests.unit.executor.algorithms.utils.test_order_tracker import _build_pending_order
from tests.unit.executor.test_account_control_guard import _AdvancingClock
from tests.unit.server._execution_test_support import build_account


def _row():
    return SimpleNamespace(
        InstrumentID="rb2701",
        ExchangeID="SHFE",
        ProductClass="1",
        VolumeMultiple=10,
        PriceTick=1,
        ExpireDate="20270115",
    )


@pytest.mark.parametrize("combination_multiplier", [0, 10])
def test_ctp_full_refresh_uses_exactly_one_native_query_and_filters_combinations(combination_multiplier):
    executor = CTPExecutor(TradeChannel("ctp"))
    combination = SimpleNamespace(
        InstrumentID="SP b2702&b2703",
        ExchangeID="DCE",
        ProductClass="3",
        VolumeMultiple=combination_multiplier,
        PriceTick=1,
    )
    executor._query = Mock(return_value=[_row(), combination])
    records = executor._query_catalog(None, 60)
    executor._query.assert_called_once()
    name, request = executor._query.call_args.args
    assert name == "ReqQryInstrument"
    assert request.InstrumentID == request.ExchangeID == request.ProductID == ""
    assert [record.key for record in records] == ["SHFE.rb2701"]


def test_ctp_demand_query_is_scoped_to_the_missing_contract():
    executor = CTPExecutor(TradeChannel("ctp"))
    executor._query = Mock(return_value=[_row()])
    executor._query_catalog(["SHFE.rb2701"], 5)
    executor._query.assert_called_once()
    name, request = executor._query.call_args.args
    assert name == "ReqQryInstrument"
    assert request.ExchangeID == "SHFE"
    assert request.InstrumentID == "rb2701"
    assert request.ProductID == ""


def test_ctp_occupied_query_lock_has_a_short_wait_limit(monkeypatch):
    executor = CTPExecutor(TradeChannel("ctp"))
    monkeypatch.setattr(executor, "_catalog_checkpoint", lambda: None)
    with executor._query_lock:
        with pytest.raises(TimeoutError, match="等待 CTP 查询权超时"):
            with executor._query_slot(0.02):
                pytest.fail("must not acquire an occupied query slot")


def _controlled_executor(monkeypatch, *, respond, override=None):
    clock = _AdvancingClock(datetime(2026, 10, 13, 8, 30))
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.clock_monotonic", clock.time)
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.get_default_clock", lambda: clock)
    monkeypatch.setattr("axile.executor.catalog_execution.clock_monotonic", clock.time)
    monkeypatch.setattr("axile.executor.instrument_catalog.clock_monotonic", clock.time)
    executor = CTPExecutor(TradeChannel("ctp"))
    executor._timeout = 0.2
    executor._ready = True
    monkeypatch.setattr(executor, "_require_active_connection", lambda: None)
    guards, sent = [], []

    @contextmanager
    def control(checkpoint):
        guard = AccountControlGuard(
            account_id=1,
            execution_id=f"catalog-{len(guards)}",
            channel=TradeChannel("ctp"),
            policy=resolve_account_control_policy("ctp", override),
            baseline=AccountControlCounterSnapshot(),
            wait_clock=clock,
        )
        guard.set_termination_checkpoint(lambda _symbol: checkpoint())
        guards.append(guard)
        yield guard

    def query(request, rid):
        sent.append((request.InstrumentID, clock.time(), executor._pending_queries[rid].timeout))
        if respond:
            clock.sleep(0.1)
            row = _row()
            row.InstrumentID = request.InstrumentID
            executor._query_response(row, None, rid, True)
        return 0

    executor._catalog_control_factory = control
    executor._control_quota_state.group_timestamps["ctp_td_global",] = int(clock.time() * 1000)
    executor._trader_api = SimpleNamespace(ReqQryInstrument=query)
    return executor, clock, guards, sent


@pytest.mark.parametrize("clear_positions", [False, True])
def test_paused_ctp_execution_fills_multiple_contracts_without_preheating(monkeypatch, clear_positions):
    executor, clock, guards, sent = _controlled_executor(monkeypatch, respond=True)
    # 十二个合约跨过原来的 5 秒来源和 15 秒准备期限，虚拟时钟不实际等待。
    symbols = [f"SHFE.rb27{month:02}" for month in range(1, 13)]
    catalog = InstrumentCatalog(CatalogLimits(ensure=0.01, demand_source=0.01))
    provider = LocalInstrumentCatalogProvider(catalog)
    query = executor._query_catalog
    provider.bind(query, channel="ctp")
    catalog.register(provider.owner, query, channel="ctp", enabled=False)
    executor.set_instrument_catalog_provider(provider)
    monkeypatch.setattr(executor, "_catalog_related_symbols", lambda: symbols if clear_positions else [])
    standard_input = SimpleNamespace(
        futures_close_policy=FuturesClosePolicy(),
        curr_target={} if clear_positions else dict.fromkeys(symbols, 1),
        last_target={},
        trade_rules={},
        symbol_algorithms={},
        forbidden_symbols=[],
        risk_symbols=[],
    )
    started = clock.time()
    assert not catalog.snapshot()
    assert catalog.status()["sources"] == 0
    executor._prepare_instrument_catalog(standard_input)
    assert executor._catalog_frozen
    assert set(catalog.snapshot()) == set(symbols)
    assert len(sent) == len(guards) == len(symbols)
    assert sent[0][1] - started >= 1.5
    assert all(int(later[1] * 1000) - int(earlier[1] * 1000) >= 1500 for earlier, later in zip(sent, sent[1:]))
    assert all(timeout == 0.2 for _, _, timeout in sent)
    assert clock.time() - started > 15
    assert catalog.status()["sources"] == 0


def test_ctp_no_response_times_out_after_sending_and_releases_query_slot(monkeypatch):
    executor, clock, guards, sent = _controlled_executor(monkeypatch, respond=False)
    started = clock.time()
    with pytest.raises(TimeoutError, match="连续 0.2 秒无回调"):
        executor._query_catalog(["SHFE.rb2701"], None)
    assert len(sent) == len(guards) == 1
    assert sent[0][1] - started >= 1.5  # 流控等待超过响应超时，仍然发出了请求。
    assert 0.2 <= clock.time() - sent[0][1] < 0.3
    assert not executor._pending_queries
    assert executor._query_lock.acquire(blocking=False)
    executor._query_lock.release()
    assert executor._catalog_query_state.active is False


@pytest.mark.parametrize(("send_at", "sent"), [(0.15, True), (0.2, False)])
def test_ctp_terminal_query_shares_budget_with_request_queue(monkeypatch, send_at, sent):
    executor, clock, _, _ = _controlled_executor(monkeypatch, respond=False)
    native_query = Mock(return_value=0)
    executor._trader_api.ReqQryOrder = native_query
    send = executor._send_trader_request

    def delayed_send(*args, **kwargs):
        clock.sleep(send_at)
        return send(*args, **kwargs)

    monkeypatch.setattr(executor, "_send_trader_request", delayed_send)
    start = clock.time()

    with pytest.raises(TimeoutError, match="超时"):
        executor._query("ReqQryOrder", td.CThostFtdcQryOrderField(), timeout=0.2)

    assert clock.time() - start == pytest.approx(0.2, abs=1e-6)
    assert native_query.call_count == int(sent)
    assert not executor._pending_queries


def test_ctp_order_query_deadline_interrupts_quota_wait(monkeypatch):
    executor, clock, _, _ = _controlled_executor(monkeypatch, respond=False)
    query = Mock(return_value=0)
    executor._trader_api.ReqQryOrder = query
    with executor._catalog_control_factory(executor._catalog_checkpoint) as guard:
        executor.set_account_control_guard(guard)
        start = clock.time()
        with pytest.raises(TimeoutError, match="流控额度超时"):
            executor._query("ReqQryOrder", td.CThostFtdcQryOrderField(), timeout=0.2)
        assert clock.time() - start == pytest.approx(0.2, abs=1e-6)
        assert not guard._wait_queue
    query.assert_not_called()
    assert not executor._pending_queries
    with executor._query_slot(0):
        pass


def test_ctp_terminal_query_operator_stop_runs_cancel_cleanup(monkeypatch):
    owner, clock, _, _ = _controlled_executor(monkeypatch, respond=False)
    owner.account_config = CTPAccountConfig(
        broker_id="9999",
        investor_id="test",
        password="fake",
        td_front="tcp://127.0.0.1:3001",
        md_front="tcp://127.0.0.1:3002",
        auth_code="fake",
        app_id="test",
    )
    requested = Event()
    owner.set_termination_controller(ExecutionTerminationController(requested, mode_provider=lambda: "cancel_pending"))
    runtime = owner.prepare_execution_runtime()
    runtime.reset_for_execute(execution_timeout=90)
    session = ExecutionSession(owner=owner, symbol="rb2701", runtime=runtime)
    order = _build_pending_order("old", session.symbol)
    tracker = OrderTracker(session, clock=clock, order_refresh_interval=2.0)
    tracker.add_order(order)
    monkeypatch.setattr(session, "get_pending_orders", lambda *, timeout=None: [order])
    cancel = Mock(return_value=True)
    monkeypatch.setattr(owner, "cancel_order", cancel)
    queries = []

    def native_query(_request, rid):
        queries.append(rid)
        if len(queries) == 2:
            requested.set()
        else:
            owner._query_response(None, None, rid, True)
        return 0

    owner._trader_api.ReqQryOrder = native_query
    with pytest.raises(ExecutionTerminated) as stopped:
        tracker.wait_for_completion(timeout=0, settle_on_timeout=True)

    assert stopped.value.trigger == "operator" and stopped.value.mode == "cancel_pending"
    assert stopped.value.cancel_attempted
    assert stopped.value.cancel_unconfirmed
    assert len(queries) == 3
    assert cancel.call_count == 2
    assert not owner._pending_queries


def test_catalog_native_query_uses_execution_group_usage_before_sending(monkeypatch):
    from axile.executor.account_control.exceptions import AccountControlBlockedError

    override = from_payload(
        AccountControlOverride, {"groups": {"ctp_td_global": {"per_day": {"limit": 1, "on_trigger": "block"}}}}
    )
    executor, clock, guards, sent = _controlled_executor(monkeypatch, respond=True, override=override)
    execution = AccountControlGuard(
        account_id=1,
        execution_id="execution",
        channel=TradeChannel("ctp"),
        policy=resolve_account_control_policy("ctp", override),
        baseline=AccountControlCounterSnapshot(),
        wait_clock=clock,
    )
    executor.set_account_control_guard(execution)
    execution.begin_operation("query_orders").record_outcome("fetched")
    with pytest.raises(AccountControlBlockedError):
        executor._query_catalog(["SHFE.rb2701"], None)
    assert sent == []
    assert len(guards) == 1
    assert executor.get_account_control_guard() is execution
    assert guards[0].flush_records()[0] == []
    assert execution.flush_records()[1][0].operation == "query_orders"


def test_public_order_is_blocked_by_catalog_usage_before_native_submission(monkeypatch):
    from axile.executor.account_control.exceptions import AccountControlBlockedError
    from axile.executor.models.unified_order import OrderDirection, OrderType

    override = from_payload(
        AccountControlOverride, {"groups": {"ctp_td_global": {"per_day": {"limit": 1, "on_trigger": "block"}}}}
    )
    executor, clock, _, sent = _controlled_executor(monkeypatch, respond=True, override=override)
    executor._query_catalog(["SHFE.rb2701"], None)
    assert len(sent) == 1
    execution = AccountControlGuard(
        account_id=1,
        execution_id="next-execution",
        channel=TradeChannel("ctp"),
        policy=resolve_account_control_policy("ctp", override),
        baseline=AccountControlCounterSnapshot(),
        wait_clock=clock,
    )
    executor.set_account_control_guard(execution)
    executor._trader_api.ReqOrderInsert = Mock(side_effect=AssertionError("quota must block before sending"))
    with pytest.raises(AccountControlBlockedError):
        executor.place_order("rb2701", OrderDirection.BUY, OrderType.LIMIT, 1, 3500)
    executor._trader_api.ReqOrderInsert.assert_not_called()


def test_bounded_batch_includes_shared_throttle_and_releases_native_query_slot(monkeypatch):
    executor, clock, guards, sent = _controlled_executor(monkeypatch, respond=True)
    started = clock.time()
    with pytest.raises((CatalogError, TimeoutError), match="超时"):
        executor._query_catalog([f"SHFE.rb27{index:02}" for index in range(1, 51)], 3.2)
    assert len(sent) == 2  # 两次共享限流放行后，第三次等不到额度便到达整批截止时间。
    assert all(stamp < started + 3.2 for _, stamp, _ in sent)
    assert clock.time() - started < 3.31
    assert not executor._pending_queries and not executor._catalog_query_state.active
    assert executor._query_lock.acquire(blocking=False)
    executor._query_lock.release()
    executor._query_catalog(["SHFE.rb2799"], None)
    assert len(sent) == 3 and sent[-1][0] == "rb2799"


def test_inherited_dispatch_deadline_cannot_be_extended_by_native_source(monkeypatch):
    executor, clock, _, sent = _controlled_executor(monkeypatch, respond=True)
    started = clock.time()
    with catalog_query_scope(started + 1):
        clock.sleep(0.7)  # IPC 排队已用去的时间。
        with pytest.raises(TimeoutError, match="超时"):
            executor._query_catalog(["SHFE.rb2701"], 100)
    assert sent == []  # 不能把 IPC 剩余 0.3 秒扩成来源声明的 100 秒。
    assert clock.time() - started < 1.11
    assert not executor._catalog_query_state.active


def test_bounded_catalog_query_lock_wait_consumes_the_same_deadline(monkeypatch):
    executor, clock, guards, sent = _controlled_executor(monkeypatch, respond=True)
    started = clock.time()
    with executor._query_lock:
        with pytest.raises(TimeoutError, match="超时"):
            executor._query_catalog(["SHFE.rb2701"], 0.2)
    assert clock.time() - started < 0.22
    assert guards == [] and sent == []
    assert not executor._catalog_query_state.active


def test_continuous_native_callbacks_do_not_extend_the_batch_deadline(monkeypatch):
    executor, clock, _, sent = _controlled_executor(monkeypatch, respond=False)
    executor._control_quota_state.group_timestamps.clear()
    original_wait = clock.event_wait

    def callbacks(event, timeout):
        clock.sleep(0.04)
        for rid in list(executor._pending_queries):
            executor._query_response(_row(), None, rid, False)  # 有进展但不结束原生响应。
        return event.is_set()

    monkeypatch.setattr(clock, "event_wait", callbacks)
    started = clock.time()
    with pytest.raises(TimeoutError, match="超时"):
        executor._query_catalog(["SHFE.rb2701", "SHFE.rb2702"], 0.15)
    assert clock.time() - started < 0.2
    assert len(sent) == 1
    assert not executor._pending_queries
    monkeypatch.setattr(clock, "event_wait", original_wait)
    assert executor._query_lock.acquire(blocking=False)
    executor._query_lock.release()


@pytest.mark.parametrize(
    "case",
    [
        "fixed",
        "shared_hit",
        "cold",
        "dependency_miss",
        "unavailable",
        "position_changes",
        "settlement_shared_hit",
        "settlement_cache_miss",
    ],
)
def test_ctp_execution_only_refreshes_assets_after_waiting_for_catalog(monkeypatch, case):
    """真实目录和规划流程只用最后一次读取写审计。"""
    executor = CTPExecutor(TradeChannel("ctp"))
    executor.account_config = from_payload(CTPAccountConfig, build_account().account_config)
    standard_input = UnifiedStandardInput.from_dict(
        {"account_config": to_payload(executor.account_config), "curr_target": {"rb2701": 2}, "last_target": {}},
        channel_type=TradeChannel("ctp"),
    )
    option = "ag2701C8000"
    records = {
        "rb2701": Instrument("SHFE", "rb2701", "FUTURE", 10, 1),
        "ag2701": Instrument("SHFE", "ag2701", "FUTURE", 15, 1),
        option: Instrument("SHFE", option, "OPTION", 15, 1, extra={"UnderlyingInstrID": "ag2701"}),
    }
    trace = []

    def query_instruments(symbols, _timeout):
        trace.append(("catalog", tuple(symbols)))
        return [records[symbol.split(".")[-1]] for symbol in symbols if symbol.split(".")[-1] in records]

    provider = LocalInstrumentCatalogProvider(InstrumentCatalog())
    provider.bind(query_instruments, channel="ctp")
    executor.set_instrument_catalog_provider(provider)
    seeded = ["rb2701"]
    if case in {"shared_hit", "settlement_shared_hit"}:
        seeded.append("ag2701")
    if case == "dependency_miss":
        seeded.append(option)
    provider.ensure(seeded, require_specs=True)
    trace.clear()

    position_reads = 0

    def query_account(name, _request):
        nonlocal position_reads
        trace.append(name)
        if name == "ReqQryTradingAccount":
            return [SimpleNamespace(Balance=10000 + 100 * position_reads)]
        assert name == "ReqQryInvestorPosition"
        position_reads += 1
        symbol = {"fixed": "rb2701", "dependency_miss": option, "unavailable": "au2701"}.get(case, "ag2701")
        if case.startswith("settlement_") and position_reads == 1:
            symbol = "rb2701"
        if case == "position_changes" and position_reads == 2:
            symbol = "au2701"
        return [
            SimpleNamespace(
                InstrumentID=symbol,
                Position=position_reads,
                TodayPosition=position_reads,
                PositionCost=1000,
                PosiDirection=td.THOST_FTDC_PD_Long,
            )
        ]

    monkeypatch.setattr(executor, "_query", query_account)
    monkeypatch.setattr(executor, "_position_valuation_quotes", lambda positions: ({}, {}))
    executor._prepare_instrument_catalog(standard_input, include_account_symbols=False)
    assert trace == []
    pinned = executor._catalog_record("rb2701")
    observations = []
    engine = executor._execution_engine()
    engine.on_planning_assets = observations.append

    if case in {"unavailable", "position_changes"}:
        message = "合约查询返回空结果" if case == "unavailable" else "au2701"
        with pytest.raises(CatalogError, match=message):
            engine._build_symbol_planning_context(standard_input)
        assert not observations
    else:
        assets, _, _ = engine._build_symbol_planning_context(standard_input)
        assert observations == [assets] and observations[0] is assets
        assert assets.total_asset == 10000 + 100 * (position_reads - 1)
        assert assets.positions[0].volume == position_reads
    if case.startswith("settlement_"):
        settled, error = engine._read_result_assets()
        assert observations == [assets]
        if case == "settlement_shared_hit":
            assert error is None
            assert [(position.symbol, position.volume) for position in settled.positions] == [("ag2701", 2)]
        else:
            assert settled.source == "unavailable"
            assert error and "ag2701" in error
    assert executor._catalog_frozen and executor._catalog_record("rb2701") is pinned
    account_reads = ["ReqQryTradingAccount", "ReqQryInvestorPosition"]
    expected = {
        "fixed": account_reads,
        "shared_hit": account_reads,
        "cold": [*account_reads, ("catalog", ("ag2701",)), *account_reads],
        "dependency_miss": [*account_reads, ("catalog", ("SHFE.ag2701",)), *account_reads],
        "unavailable": [*account_reads, ("catalog", ("au2701",))],
        "position_changes": [*account_reads, ("catalog", ("ag2701",)), *account_reads],
        "settlement_shared_hit": account_reads * 2,
        "settlement_cache_miss": account_reads * 2,
    }
    assert trace == expected[case]
