"""全量只发一次请求；按需查询必须指定合约。"""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.instrument_catalog import CatalogLimits, InstrumentCatalog, LocalInstrumentCatalogProvider
from tests.unit.executor.test_account_control_guard import _AdvancingClock


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
    executor = CTPExecutor(TradeChannel.CTP)
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
    executor = CTPExecutor(TradeChannel.CTP)
    executor._query = Mock(return_value=[_row()])
    executor._query_catalog(["SHFE.rb2701"], 5)
    executor._query.assert_called_once()
    name, request = executor._query.call_args.args
    assert name == "ReqQryInstrument"
    assert request.ExchangeID == "SHFE"
    assert request.InstrumentID == "rb2701"
    assert request.ProductID == ""


def test_ctp_occupied_query_lock_has_a_short_wait_limit(monkeypatch):
    executor = CTPExecutor(TradeChannel.CTP)
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
    executor = CTPExecutor(TradeChannel.CTP)
    executor._timeout = 0.2
    executor._ready = True
    monkeypatch.setattr(executor, "_require_active_connection", lambda: None)
    guards, sent = [], []

    @contextmanager
    def control(checkpoint):
        guard = AccountControlGuard(
            account_id=1,
            execution_id=f"catalog-{len(guards)}",
            channel=TradeChannel.CTP,
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


def test_catalog_native_query_uses_execution_group_usage_before_sending(monkeypatch):
    from axile.executor.account_control.exceptions import AccountControlBlockedError

    override = AccountControlOverride.model_validate(
        {"groups": {"ctp_td_global": {"per_day": {"limit": 1, "on_trigger": "block"}}}}
    )
    executor, clock, guards, sent = _controlled_executor(monkeypatch, respond=True, override=override)
    execution = AccountControlGuard(
        account_id=1,
        execution_id="execution",
        channel=TradeChannel.CTP,
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

    override = AccountControlOverride.model_validate(
        {"groups": {"ctp_td_global": {"per_day": {"limit": 1, "on_trigger": "block"}}}}
    )
    executor, clock, _, sent = _controlled_executor(monkeypatch, respond=True, override=override)
    executor._query_catalog(["SHFE.rb2701"], None)
    assert len(sent) == 1
    execution = AccountControlGuard(
        account_id=1,
        execution_id="next-execution",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy("ctp", override),
        baseline=AccountControlCounterSnapshot(),
        wait_clock=clock,
    )
    executor.set_account_control_guard(execution)
    executor._trader_api.ReqOrderInsert = Mock(side_effect=AssertionError("quota must block before sending"))
    with pytest.raises(AccountControlBlockedError):
        executor.place_order("rb2701", OrderDirection.BUY, OrderType.LIMIT, 1, 3500)
    executor._trader_api.ReqOrderInsert.assert_not_called()
