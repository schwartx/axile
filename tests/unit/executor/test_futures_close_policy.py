"""品种规则、账户费用比较与今昨拆单的跨层契约。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from axile.common.trade_channel import TradeChannel
from axile.executor.futures_close_policy import (
    CommissionRates,
    FuturesClosePolicy,
    close_capability,
    futures_product,
    rule_priority,
)
from axile.executor.futures_close_runtime import FuturesCloseRuntimeMixin
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection
from axile.executor.termination import ExecutionTerminated


def assets(*, today=3, yesterday=2, frozen=0):
    return UnifiedAccountAssets(
        available_cash=100,
        total_asset=100,
        market_value=0,
        positions=[
            Position(
                symbol="ag2611",
                volume=today + yesterday,
                available_volume=today + yesterday - frozen,
                direction=PositionDirection.LONG,
                market_value=0,
                extra={"long_td": today, "long_yd": yesterday},
            )
        ],
    )


def rates(yesterday=10, today=1, money=0):
    return CommissionRates(
        yesterday_money=money,
        yesterday_volume=yesterday,
        today_money=money,
        today_volume=today,
        queried_at="2026-10-16T09:00:00",
    )


class Owner(FuturesCloseRuntimeMixin):
    def __init__(self, policy):
        self.channel_type = TradeChannel.CTP
        self.logger = MagicMock()
        self._trading_day = "20261016"
        self._instruments = {"ag2611": SimpleNamespace(VolumeMultiple=15)}
        self._query_commission_rates = MagicMock(return_value=rates())
        self.get_market_data = MagicMock(return_value={"ag2611": SimpleNamespace(last_price=5000)})
        self.runtime = MagicMock()
        self._prepare_futures_close_policy(policy)

    def get_active_execution_runtime(self):
        return self.runtime

    def handle_termination_checkpoint(self):
        pass

    def plan(self, exchange="SHFE", **kwargs):
        return self._plan_futures_close("ag2611", OrderDirection.SELL, 4, assets(**kwargs), exchange)


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("AG2611", "AG"),
        ("ag2612", "AG"),
        ("SHFE.rb2701", "RB"),
        ("TA701", "TA"),
        ("ag2611C5000", None),
        ("SP ag2611&ag2612", None),
    ],
)
def test_product_uses_letters_not_month(symbol, expected):
    assert futures_product(symbol) == expected


def test_override_normalization_and_validation():
    policy = FuturesClosePolicy(overrides={"ag": "today_first"})
    assert rule_priority(policy, futures_product("AG2611")) == ("today_first", "override")
    assert rule_priority(policy, futures_product("AG2612")) == ("today_first", "override")
    for overrides in ({"AG2611": "today_first"}, {"ag": "today_first", "AG": "yesterday_first"}):
        with pytest.raises(ValidationError):
            FuturesClosePolicy(overrides=overrides)
    policy.validate_channel(TradeChannel.TQ)
    with pytest.raises(ValueError, match="不支持"):
        FuturesClosePolicy(mode="fee").validate_channel(TradeChannel.TQ)
    assert rule_priority(FuturesClosePolicy(fallback="today_first"), "UNKNOWN") == ("today_first", "default")
    assert rule_priority(FuturesClosePolicy(fallback="today_first"), "AG") == ("yesterday_first", "preset")


def test_presets_leave_fees_unknown_and_declare_modes():
    assert close_capability(automatic=True).modes == ["rules", "fee"]
    assert close_capability(automatic=False).modes == ["rules"]
    presets = close_capability(automatic=False).presets
    assert len(presets) == len({p.product for p in presets})
    assert all(p.yesterday_fee is None and p.today_fee is None for p in presets)


@pytest.mark.parametrize(
    "priority,expected",
    [
        ("today_first", [(3, {"offset_flag": "close_today"}), (1, {"offset_flag": "close_yesterday"})]),
        ("yesterday_first", [(2, {"offset_flag": "close_yesterday"}), (2, {"offset_flag": "close_today"})]),
    ],
)
def test_account_rule_controls_both_exchange_plans(priority, expected):
    owner = Owner(FuturesClosePolicy(overrides={"AG": priority}))
    for exchange in ("SHFE", "INE"):
        assert owner.plan(exchange) == expected
    owner._query_commission_rates.assert_not_called()


def test_fee_comparison_zero_is_valid_and_ties_use_product_rule():
    owner = Owner(FuturesClosePolicy(mode="fee", overrides={"AG": "yesterday_first"}))
    owner._query_commission_rates.return_value = rates(yesterday=10, today=0)
    assert owner.plan()[0] == (3, {"offset_flag": "close_today"})
    decision = owner.runtime.emit_audit_event.call_args.kwargs["details"]["futures_close_decision"]
    assert decision["source"] == "fee" and decision["today_cost"] == 0
    owner._trading_day = "20261019"
    owner._query_commission_rates.return_value = rates(yesterday=1, today=1)
    assert owner.plan()[0][1]["offset_flag"] == "close_yesterday"
    decision = owner.runtime.emit_audit_event.call_args.kwargs["details"]["futures_close_decision"]
    assert decision["source"] == "override" and "相同" in decision["fallback_reason"]


def test_failure_falls_back_without_repeated_queries_or_credential_leak():
    owner = Owner(FuturesClosePolicy(mode="fee", overrides={"AG": "today_first"}))
    owner._query_commission_rates.side_effect = TimeoutError("password=must-not-export")
    assert owner.plan()[0][1]["offset_flag"] == "close_today"
    owner.plan()
    owner._query_commission_rates.assert_called_once()
    decision = owner.runtime.emit_audit_event.call_args.kwargs["details"]["futures_close_decision"]
    assert "must-not-export" not in str(decision)
    assert "不可用" in decision["fallback_reason"]
    owner._prepare_futures_close_policy(FuturesClosePolicy(mode="fee"))
    owner.plan()
    assert owner._query_commission_rates.call_count == 2


def test_fee_cache_is_account_and_trading_day_scoped():
    owner = Owner(FuturesClosePolicy(mode="fee"))
    owner.plan()
    owner._prepare_futures_close_policy(FuturesClosePolicy(mode="fee"))
    owner.plan()
    owner._query_commission_rates.assert_called_once()
    other = Owner(FuturesClosePolicy(mode="fee"))
    other.plan()
    other._query_commission_rates.assert_called_once()
    owner._trading_day = "20261019"
    owner.plan()
    assert owner._query_commission_rates.call_count == 2


def test_exchange_restrictions_and_frozen_positions_do_not_get_fee_fallback():
    owner = Owner(FuturesClosePolicy(mode="fee"))
    assert owner.plan("CFFEX") == [(4, {"offset_flag": "close"})]
    with pytest.raises(ValueError, match="冻结"):
        owner.plan(frozen=1)
    owner._query_commission_rates.assert_not_called()


@pytest.mark.parametrize("error", [ExecutionTerminated(reason="test", mode="graceful"), MemoryError()])
def test_termination_and_fatal_errors_are_not_fee_fallback(error):
    owner = Owner(FuturesClosePolicy(mode="fee"))
    owner._query_commission_rates.side_effect = error
    with pytest.raises(type(error)):
        owner.plan()


def test_money_and_volume_fees_are_compared_on_same_basis():
    assert rates(yesterday=2, today=3, money=0.0001).costs(5000, 15) == (9.5, 10.5)
    assert rates(yesterday=0, today=1).costs(0, 0) == (0, 1)
    with pytest.raises(ValueError):
        rates(money=0.0001).costs(0, 15)
    with pytest.raises(ValidationError):
        rates(today=float("nan"))


def test_ctp_query_builds_account_request_and_preserves_real_zero_rate():
    import threading

    from axile.executor.ctp.ctp_execute import CTPExecutor
    from axile.executor.ctp.spi import TraderSpi

    executor = CTPExecutor.__new__(CTPExecutor)
    executor._catalog_query_state = threading.local()
    executor._timeout = 20
    executor._config = lambda: SimpleNamespace(broker_id="fixture", investor_id="fixture")
    executor._instruments = {"ag2611": SimpleNamespace(ExchangeID="SHFE")}
    row = SimpleNamespace(
        InstrumentID="ag2611",
        CloseRatioByMoney=0,
        CloseRatioByVolume=10,
        CloseTodayRatioByMoney=0,
        CloseTodayRatioByVolume=0,
    )
    executor._query = MagicMock(return_value=[row])
    actual = executor._query_commission_rates("ag2611")
    assert actual.costs(0, 0) == (10, 0)
    call = executor._query.call_args
    assert call.args[0] == "ReqQryInstrumentCommissionRate"
    assert call.args[1].InvestorID == "fixture"
    assert call.args[1].InstrumentID == "ag2611"
    assert call.kwargs["timeout"] == 3
    assert executor._catalog_query_state.fee_deadline is None
    owner = SimpleNamespace(_query_response=MagicMock())
    TraderSpi(owner).OnRspQryInstrumentCommissionRate(row, None, 123, True)
    owner._query_response.assert_called_once_with(row, None, 123, True)
    executor._query.return_value = []
    with pytest.raises(ValueError, match="缺失"):
        executor._query_commission_rates("ag2611")
    assert executor._catalog_query_state.fee_deadline is None


def test_ctp_fee_deadline_includes_query_slot_and_throttle_waits(monkeypatch):
    import threading

    from axile.executor.abstract_executor.facade import AbstractExecutorFacadeMixin
    from axile.executor.ctp.ctp_execute import CTPExecutor

    executor = CTPExecutor.__new__(CTPExecutor)
    executor._catalog_query_state = threading.local()
    executor._catalog_query_state.fee_deadline = 1.0
    executor._require_active_connection = lambda: None
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.clock_monotonic", lambda: 2.0)
    monkeypatch.setattr(AbstractExecutorFacadeMixin, "handle_termination_checkpoint", lambda *a, **k: None)
    with pytest.raises(TimeoutError, match="手续费查询超时"):
        executor._catalog_checkpoint()
    with pytest.raises(TimeoutError, match="手续费查询超时"):
        executor.handle_termination_checkpoint()


def test_fee_cache_uses_account_snapshot_trading_day_after_day_rollover():
    owner = Owner(FuturesClosePolicy(mode="fee"))
    for day in ("20261016", "20261019"):
        snapshot = assets()
        snapshot.extra["trading_day"] = day
        owner._plan_futures_close("ag2611", OrderDirection.SELL, 4, snapshot, "SHFE")
    assert owner._query_commission_rates.call_count == 2
    assert owner._trading_day == "20261016"
