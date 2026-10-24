"""验证独立权益回放、缺口与回测设置持久化."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from axile.server._native import _core
from axile.server.api.routes import account_performance
from axile.server.api.routes.account_performance import router
from axile.server.db.models.performance import PerformanceSettings
from axile.server.performance import close_before
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus
from tests.conftest import initialize_native_database, native_store_call
from tests.unit.server._business_analysis_support import analysis_record, database, persist_records
from tests.unit.server._business_analysis_support import schema_template as schema_template
from tests.unit.server._execution_test_support import build_account, insert_account
from tests.unit.server._performance_oracle import (
    Observation,
    build_wbt_input,
    calculate_performance,
    observation,
    performance_calendar,
)
from tests.unit.server.test_initial_migration import _connection, _insert_account


def obs(day, prices=None, target=None, asset=100, **kwargs):
    return Observation(
        day,
        f"exec-{day}",
        datetime(2026, 1, 1) + timedelta(days=day - 1),
        asset,
        target if target is not None else {"A": 1.0},
        prices or {"A": 100.0},
        **kwargs,
    )


def run(items, mode="ts", fee=0, range_key="all"):
    return calculate_performance(
        items, PerformanceSettings(backtest_weight_type=mode, backtest_fee_rate=fee), range_key
    )


def test_performance_calendar_merges_closed_days_and_preserves_unknown(monkeypatch):
    closed = {"2026-01-02", "2026-01-03", "2026-01-05"}
    unknown = {"2026-01-04"}

    def evaluate(channel, day):
        status = (
            CalendarDecisionStatus.UNAVAILABLE
            if day.isoformat() in unknown
            else CalendarDecisionStatus.AVAILABLE_CLOSED
            if day.isoformat() in closed
            else CalendarDecisionStatus.AVAILABLE_OPEN
        )
        return CalendarDayDecision(
            channel=str(channel), day=day, status=status, calendar_id="china", label="中国交易日"
        )

    monkeypatch.setattr("tests.unit.server._performance_oracle.evaluate_channel_calendar_day", evaluate)
    result = performance_calendar("ctp", "2026-01-01T09:00:00", "2026-01-05T16:00:00")
    assert result["status"] == "partial"
    assert [(item["start"], item["end"]) for item in result["closed_ranges"]] == [
        ("2026-01-02", "2026-01-03"),
        ("2026-01-05", "2026-01-05"),
    ]
    assert [(item["start"], item["end"]) for item in result["unavailable_ranges"]] == [("2026-01-04", "2026-01-04")]


def test_performance_calendar_not_required(monkeypatch):
    monkeypatch.setattr(
        "tests.unit.server._performance_oracle.evaluate_channel_calendar_day",
        lambda channel, day: CalendarDayDecision(
            channel=str(channel), day=day, status=CalendarDecisionStatus.NOT_REQUIRED
        ),
    )
    result = performance_calendar("always-open", "2026-01-01", "2026-01-03")
    assert result["status"] == "not_required"
    assert result["closed_ranges"] == []


def test_performance_calendar_degrades_to_unavailable_on_unregistered_channel(monkeypatch):
    def evaluate(channel, day):
        raise KeyError(channel)

    monkeypatch.setattr("tests.unit.server._performance_oracle.evaluate_channel_calendar_day", evaluate)
    result = performance_calendar("not-registered", "2026-01-01", "2026-01-03")
    assert result["status"] == "unavailable"
    assert result["closed_ranges"] == []


def test_close_before_uses_last_day_end_before_observation():
    result = run([obs(1, asset=100), obs(2, asset=110), obs(3, asset=99)])
    assert close_before(result["points"], "2026-01-01") == pytest.approx(100)
    assert close_before(result["points"], "2026-01-02") == pytest.approx(100)
    assert close_before(result["points"], "2026-01-03") == pytest.approx(110)
    assert close_before(result["points"], "2026-01-04") == pytest.approx(99)
    assert close_before([], "2026-01-01") is None


def test_replay_and_account_compound_returns():
    result = run([obs(1), obs(2, {"A": 110.0}, asset=110), obs(3, {"A": 99.0}, asset=99)])
    assert result["points"][0]["account_return"] == 0
    assert result["points"][-2]["account_return"] == pytest.approx(0.1)
    assert result["points"][-1]["account_return"] == pytest.approx(-0.01)
    assert result["points"][-1]["portfolio_return"] == pytest.approx(-0.01)
    assert result["points"][-1]["difference"] == pytest.approx(0)


@pytest.mark.parametrize("mode", ["ts", "cs"])
def test_account_weights_are_summed_including_legacy_mode(mode):
    weights = {"A": 1.0, "B": -0.5}
    result = run([obs(1, {"A": 100.0, "B": 100.0}, weights), obs(2, {"A": 110.0, "B": 110.0}, weights)], mode)
    assert result["points"][-1]["portfolio_return"] == pytest.approx(0.05)
    assert result["settings"]["backtest_weight_type"] == "cs"


def test_exit_row_accounts_for_last_holding_return_and_fee():
    items = [obs(1), obs(2, {"A": 110.0}, {}), obs(3, {"A": 140.0}, {})]
    built = build_wbt_input(items)
    assert built.skips["count"] == 0
    assert built.frame.iloc[-1]["weight"] == 0
    assert run(items)["points"][-1]["portfolio_return"] == pytest.approx(0.1)
    assert run(items, fee=0.001)["points"][-1]["portfolio_return"] == pytest.approx(0.0989)


def test_missing_exit_price_skips_but_portfolio_holds_and_recovers():
    items = [obs(1), obs(2, {"B": 10.0}, {"B": 1.0}, 110), obs(3, {"A": 120.0, "B": 11.0}, {"B": 1.0}, 120)]
    result = run(items)
    assert result["skips"]["count"] == 1
    assert result["skips"]["missing_ticks"] == 1
    assert result["skips"]["first_time"] == items[1].time.isoformat()
    assert result["used_record_count"] == 2
    # 跳过期间组合按 A 持仓延续，恢复点以 120 结算 A 退出行。
    assert result["points"][-1]["portfolio_return"] == pytest.approx(0.2)
    assert result["points"][-1]["difference"] == pytest.approx(0)
    assert result["points"][-1]["account_return"] == pytest.approx(0.2)


def test_missing_target_is_not_a_clear():
    item = obs(2)
    item.target = None
    result = run([obs(1), item, obs(3)])
    assert result["skips"]["count"] == 1
    assert result["skips"]["missing_target"] == 1
    assert result["skips"]["first_time"] == item.time.isoformat()
    # 缺失目标按持仓延续而非清仓：A@100→A@100 收益为 0。
    assert result["points"][-1]["portfolio_return"] == pytest.approx(0)


def test_range_restarts_after_old_gap_and_has_no_500_record_limit():
    items = [obs(i + 1, {"A": float(100 + i)}) for i in range(600)]
    items[2].target = None
    assert run(items)["skips"]["count"] == 1
    assert run(items)["used_record_count"] == 599
    recent = run(items, range_key="30")
    assert recent["skips"]["count"] == 0
    assert recent["record_count"] == 600
    assert recent["used_record_count"] == 31


def test_carry_forward_skip_equals_manually_held_weights():
    items = [obs(1), obs(2, {"A": 110.0}, asset=110), obs(3, {"A": 120.0}, {}, 120)]
    items[1].prices = {}
    skipped = run(items)
    held = run([items[0], items[2]])
    assert skipped["points"][-1]["portfolio_return"] == pytest.approx(held["points"][-1]["portfolio_return"])
    assert skipped["points"][-1]["portfolio_return"] == pytest.approx(0.2)
    assert skipped["skips"]["count"] == 1
    assert skipped["used_record_count"] + skipped["skips"]["count"] == skipped["observation_count"]


def test_failure_burst_is_carried_and_counted_as_missing_target():
    burst = [obs(i, asset=100 + i) for i in (2, 3, 4)]
    for item in burst:
        item.target = None
    items = [obs(1), *burst, obs(5, {"A": 120.0, "B": 10.0}, {"B": 1.0}, 120)]
    result = run(items)
    assert result["skips"]["count"] == 3
    assert result["skips"]["missing_target"] == 3
    assert result["skips"]["missing_ticks"] == 0
    assert result["skips"]["first_time"] == burst[0].time.isoformat()
    assert result["skips"]["last_time"] == burst[-1].time.isoformat()
    assert result["used_record_count"] == 2
    assert result["used_record_count"] + result["skips"]["count"] == result["observation_count"]
    # 无参与记录的突发日保留账户观察，组合按持仓延续计 0，恢复日以 A 退出行结算。
    assert all(point["portfolio_return"] is not None for point in result["points"])
    assert [point["portfolio_daily_return"] for point in result["points"][1:-1]] == [0, 0, 0, 0]
    assert result["points"][-1]["portfolio_return"] == pytest.approx(0.2)


def test_day_point_samples_last_participant_not_last_record():
    # 当日最后一条是闭市心跳（无目标）时，账户与组合都取最后一条参与记录。
    items = [obs(1), obs(2, {"A": 110.0}, asset=110), obs(2, asset=120)]
    items[2].target = None
    items[2].id = 99
    items[2].time = items[1].time + timedelta(hours=2)
    result = run(items)
    assert [p["date"] for p in result["points"]] == ["2026-01-01T00:00:00", "2026-01-01", "2026-01-02"]
    day2 = result["points"][-1]
    assert day2["observed_at"] == items[1].time.isoformat()
    assert day2["record_id"] == items[1].id
    assert day2["account_return"] == pytest.approx(0.1)
    assert day2["portfolio_return"] == pytest.approx(0.1)


def test_quick_path_pairs_day_points_with_same_participants():
    items = [obs(1), obs(2, {"A": 110.0}, asset=110), obs(2, asset=120), obs(3, {"A": 121.0}, asset=121)]
    items[2].target = None
    items[2].id = 99
    settings = PerformanceSettings(backtest_weight_type="ts", backtest_fee_rate=0)
    full = calculate_performance(items, settings, "all")
    quick = calculate_performance(items, settings, "all", include_backtest=False)
    assert [(p["date"], p["observed_at"], p["account_return"]) for p in quick["points"]] == [
        (p["date"], p["observed_at"], p["account_return"]) for p in full["points"]
    ]


def test_all_unusable_observations_keep_portfolio_null_not_flat_zero():
    items = [obs(1, asset=100), obs(2, asset=110), obs(3, asset=99)]
    for item in items:
        item.target = None
    result = run(items)
    assert result["skips"]["count"] == 3
    assert all(point["portfolio_return"] is None for point in result["points"])
    assert result["points"][-1]["account_return"] == pytest.approx(-0.01)


def test_portfolio_unknown_before_first_participating_observation():
    # 无共同基准路径：账户基准先于回测首个参与观测，起点前不得平铺 0 收益。
    items = [obs(1, asset=100), obs(2, {"A": 110.0}, asset=None)]
    items[0].target = None
    result = run(items)
    assert result["used_record_count"] == 1
    assert result["points"][0]["portfolio_return"] is None
    assert result["points"][1]["portfolio_return"] is None
    assert result["points"][2]["portfolio_return"] == 0


def test_anchor_portfolio_unknown_when_participation_starts_after_base_same_day():
    # 基准点仅资产、同日晚些时候才有首个参与观测：锚点回测收益按时刻比较回归 None，
    # 不得因同日就伪装成 0.0 起投。
    items = [obs(1, asset=100), obs(2, {"A": 100.0}, asset=None)]
    items[0].target = None
    items[0].time = datetime(2026, 1, 1, 9, 0)
    items[1].time = datetime(2026, 1, 1, 14, 0)
    result = run(items)
    anchor = result["points"][0]
    assert anchor["account_return"] == 0
    assert anchor["portfolio_return"] is None
    assert anchor["difference"] is None


def test_clear_positions_still_participates_after_holding():
    items = [obs(1), obs(2, {"A": 110.0}, {}, 110)]
    result = run(items)
    assert result["skips"]["count"] == 0
    assert result["used_record_count"] == 2
    assert result["points"][-1]["portfolio_return"] == pytest.approx(0.1)


def test_duplicates_keep_last_record_and_zero_assets_are_not_removed():
    items = [obs(1), obs(2, asset=110), obs(2, asset=0)]
    items[-1].id = 99
    result = run(items)
    assert result["used_record_count"] == 2
    assert result["points"][-1]["account_return"] == -1


def test_missing_asset_is_null_not_zero_and_later_cumulative_recovers():
    result = run([obs(1), obs(2, asset=None), obs(3, asset=120)])
    assert result["points"][-2]["account_return"] is None
    assert result["points"][-1]["account_return"] == pytest.approx(0.2)
    assert result["points"][-1]["account_daily_return"] is None


def test_intraday_returns_use_equity_instead_of_summing_bar_returns():
    items = [obs(1), obs(2, {"A": 110.0}, asset=110), obs(3, {"A": 121.0}, asset=121)]
    for i, item in enumerate(items):
        item.time = datetime(2026, 1, 1, 9 + i)
    result = run(items)
    assert result["points"][-1]["account_return"] == pytest.approx(0.21)
    # 日内 100→110→121 必须与账户权益一样得到 21%，而不是相加得到 20%。
    assert result["points"][-1]["portfolio_daily_return"] == pytest.approx(0.21)


@pytest.mark.parametrize("layout", ["nested", "dict", "list"])
def test_tick_formats_and_failed_records(layout):
    tick = {"symbol": "A", "bid_price": [99], "ask_price": [101]}
    result = {"account_assets": {"total_asset": 100}}
    if layout == "nested":
        result["symbol_results"] = {"A": {"first_tick": tick}}
    else:
        result["first_ticks"] = [tick] if layout == "list" else {"A": tick}
    record = SimpleNamespace(
        id=1,
        execution_id="x",
        created_at="2026-01-01T01:00:00Z",
        input={"curr_target": {"A": 1}},
        result=result,
        is_success=0,
    )
    item = observation(record)
    assert item.prices == {"A": 100.0}
    assert item.time.hour == 9
    tick["book_valid"] = False
    assert observation(record).prices == {}
    record.input = {}
    assert observation(record, {"A": 0.5}).target == {"A": 0.5}
    record.result["execution_kind"] = "clear_positions"
    assert observation(record).target == {}


def sized(status="SIZED", mode="weight", qty=1.0, notional=237015.0, equity=102335.888, reason="COMMON.SIZING.EXACT"):
    row = {
        "sizing_mode": mode,
        "status": status,
        "reason_code": reason,
        "account_weight": 1.05,
        "equity": equity,
        "reference_price": 100.0,
    }
    if qty is not None:
        row["target_quantity"] = qty
    if notional is not None:
        row["unit_notional"] = notional
    return {"sizing": row}


def test_mark_price_prefers_last_with_mid_fallback():
    def record_with_tick(tick):
        return SimpleNamespace(
            id=1,
            execution_id="x",
            created_at="2026-01-01T01:00:00Z",
            input={},
            result={"account_assets": {"total_asset": 100}, "first_ticks": {"A": tick}},
            is_success=1,
        )

    # 最新价有效时优先于买卖中间价。
    assert observation(record_with_tick({"last_price": 98.0, "bid_price": 90, "ask_price": 110})).prices == {"A": 98.0}
    # 无最新价回退中间价；仅最新价无盘口也可用。
    assert observation(record_with_tick({"bid_price": 99, "ask_price": 101})).prices == {"A": 100.0}
    assert observation(record_with_tick({"last_price": 97.0})).prices == {"A": 97.0}
    # 最新价无效回退中间价；盘口无效仍然剔除。
    assert observation(record_with_tick({"last_price": 0.0, "bid_price": 99, "ask_price": 101})).prices == {"A": 100.0}
    assert (
        observation(
            record_with_tick({"last_price": 98.0, "bid_price": 99, "ask_price": 101, "book_valid": False})
        ).prices
        == {}
    )


def sized_record(symbol_results, curr_target=None, kind=None):
    result = {"account_assets": {"total_asset": 102335.888}, "symbol_results": symbol_results}
    if kind:
        result["execution_kind"] = kind
    raw_input = {"curr_target": curr_target} if curr_target is not None else {}
    return SimpleNamespace(
        id=1,
        execution_id="x",
        created_at="2026-01-01T01:00:00Z",
        input=raw_input,
        result=result,
        is_success=1,
    )


def test_below_min_quantity_replays_zero_executable_weight():
    record = sized_record(
        {
            "X": {
                **sized(qty=0.0, reason="COMMON.SIZING.BELOW_MIN_QUANTITY"),
                "first_tick": {"bid_price": 99, "ask_price": 101},
            }
        },
        curr_target={"X": 1.05},
    )
    assert observation(record).target == {"X": 0.0}


def test_dual_weight_curves_keep_account_axis_and_zero_lot_is_not_fallback():
    first = sized_record({"X": {**sized(qty=0.0), "first_tick": {"last_price": 100}}}, {"X": 1.05})
    second = sized_record({"X": {**sized(qty=0.0), "first_tick": {"last_price": 110}}}, {"X": 1.05})
    second.id = 2
    second.created_at = "2026-01-02T01:00:00Z"
    second.result["account_assets"]["total_asset"] = 110
    result = run([observation(first), observation(second)])
    assert result["points"][-1]["account_return"] == pytest.approx(110 / 102335.888 - 1)
    assert result["points"][-1]["portfolio_return"] == 0
    assert result["points"][-1]["target_portfolio_return"] == pytest.approx(0.105)
    assert result["sizing_fallback_count"] == 0
    assert result["used_record_count"] == result["target_used_record_count"] == 2


def test_dual_weight_skips_and_fallback_only_count_participants():
    rows = [
        obs(1, {"A": 100}, {"A": 0}, target_weight={"A": 1.0}),
        obs(2, {"A": 110}, {"A": 1.0}, target_weight={"A": 1.0}, sizing_fallback=True),
        obs(3, {}, {"A": 1.0}, target_weight={"A": 1.0}, sizing_fallback=True),
        obs(4, {"A": 120}, {"A": 1.0}, target_weight={"A": 1.0}, sizing_fallback=True),
    ]
    rows[2].prices = {}
    result = run(rows)
    assert result["sizing_fallback_count"] == 2
    assert [(r["count"], r["start"], r["end"]) for r in result["sizing_fallback_ranges"]] == [
        (2, rows[1].time.isoformat(), rows[3].time.isoformat())
    ]
    assert result["points"][2]["sizing_fallback"] is True
    assert result["skips"]["missing_ticks"] == result["target_skips"]["missing_ticks"] == 1


def test_target_price_requirement_is_independent_of_sized_zero():
    rows = [
        obs(1, {"A": 100}, {"A": 0}, target_weight={"A": 1.0}),
        obs(2, {"A": 110}, {"A": 0}, target_weight={"A": 1.0}),
    ]
    rows[0].prices = rows[1].prices = {}
    result = run(rows)
    assert result["used_record_count"] == 2
    assert result["target_used_record_count"] == 0
    assert result["points"][-1]["portfolio_return"] == 0
    assert result["target_skips"]["missing_ticks"] == 2
    assert all(point["target_portfolio_return"] is None for point in result["points"])


def test_quantized_quantity_replays_lot_weight_per_row_equity():
    record = sized_record({"X": sized(qty=2.0), "S": sized(qty=-3.0, notional=34780.0, equity=101000.0)})
    target = observation(record).target
    assert target["X"] == pytest.approx(2 * 237015.0 / 102335.888)
    assert target["S"] == pytest.approx(-3 * 34780.0 / 101000.0)


def test_zero_target_row_keeps_explicit_zero_without_notional():
    record = sized_record(
        {"X": sized(qty=0.0, notional=None, reason="COMMON.SIZING.ZERO_TARGET"), "S": sized(qty=1.0, notional=34780.0)}
    )
    assert observation(record).target == {"X": 0.0, "S": pytest.approx(34780.0 / 102335.888)}


def test_lots_mode_falls_back_to_curr_target():
    record = sized_record({"X": sized(mode="lots", qty=2.0, notional=None)}, curr_target={"X": 1.05})
    assert observation(record).target == {"X": 1.05}


def test_partial_sizing_evidence_falls_back_whole_record():
    record = sized_record(
        {"X": sized(qty=1.0), "S": {"first_tick": {"bid_price": 99, "ask_price": 101}}},
        curr_target={"X": 0.6, "S": 0.4},
    )
    assert observation(record).target == {"X": 0.6, "S": 0.4}


def test_unavailable_sizing_falls_back_whole_record():
    record = sized_record(
        {"X": sized(status="UNAVAILABLE", qty=None, notional=None, reason="COMMON.SIZING.INVALID_PRICE")},
        curr_target={"X": 1.05},
    )
    assert observation(record).target == {"X": 1.05}


def test_nonpositive_equity_or_missing_notional_falls_back():
    record = sized_record({"X": sized(qty=1.0, equity=0.0)}, curr_target={"X": 1.05})
    assert observation(record).target == {"X": 1.05}
    record = sized_record({"X": sized(qty=1.0, notional=None)}, curr_target={"X": 1.05})
    assert observation(record).target == {"X": 1.05}


def test_clear_positions_with_full_sizing_still_empties_target():
    record = sized_record({"X": sized(qty=1.0)}, kind="clear_positions")
    assert observation(record).target == {}


def test_forbidden_symbol_absent_from_derived_vector_participates():
    record = sized_record(
        {"A": {**sized(qty=1.0, notional=100.0), "first_tick": {"bid_price": 99, "ask_price": 101}}},
        curr_target={"F": 0.4, "A": 0.6},
    )
    assert observation(record).target == {"A": pytest.approx(100.0 / 102335.888)}


def test_below_min_weight_excluded_from_portfolio_pnl():
    items = []
    for day, x_price in ((1, 100.0), (2, 90.0)):
        record = sized_record(
            {
                "X": {
                    **sized(qty=0.0, reason="COMMON.SIZING.BELOW_MIN_QUANTITY", notional=None),
                    "first_tick": {"bid_price": x_price - 1, "ask_price": x_price + 1},
                },
                "S": {
                    **sized(qty=1.0, notional=100.0, equity=102335.888),
                    "first_tick": {"bid_price": 99, "ask_price": 101},
                },
            },
            curr_target={"X": 1.05, "S": 0.0},
        )
        record.created_at = f"2026-01-0{day}T01:00:00Z"
        items.append(observation(record))
    result = run(items)
    assert result["skips"]["count"] == 0
    assert result["used_record_count"] == 2
    # X 不足一手为 0 敞口（旧口径会重放 1.05×-10%）；S 一手等值现金价格持平，组合收益恰为零。
    assert result["points"][-1]["portfolio_return"] == 0
    assert result["points"][-1]["difference"] == 0


@pytest.mark.parametrize("fee", [True, None, "0.01", float("nan"), float("inf"), -0.1, 1.0])
def test_invalid_settings_rejected(fee):
    with pytest.raises(ValidationError):
        PerformanceSettings(backtest_weight_type="ts", backtest_fee_rate=fee)


def test_empty_and_single_observation():
    assert run([])["points"] == []
    assert run([obs(1)])["points"][-1]["portfolio_return"] == 0
    assert run([obs(1)])["observation_count"] == 1


def test_two_intraday_observations_are_not_an_empty_chart():
    items = [obs(1), obs(2, {"A": 110.0}, asset=110)]
    items[1].time = items[0].time + timedelta(hours=1)
    result = run(items)
    assert result["observation_count"] == 2
    assert len(result["points"]) == 2
    assert result["points"][-1]["portfolio_return"] == pytest.approx(0.1)


@pytest.mark.parametrize("range_key", ["all", "30", "90"])
@pytest.mark.parametrize("scenario", ["normal", "intraday", "duplicate", "missing_asset", "no_baseline", "empty"])
def test_account_only_matches_full_without_building_or_running_wbt(monkeypatch, range_key, scenario):
    items = [obs(i + 1, asset=100 + i) for i in range(95)]
    if scenario == "intraday":
        items = [obs(1), obs(2, asset=110)]
        items[1].time = items[0].time + timedelta(hours=1)
    elif scenario == "duplicate":
        items.append(obs(95, asset=110))
        items[-1].id = 100
    elif scenario == "missing_asset":
        items[-1].asset = None
    elif scenario == "no_baseline":
        for item in items:
            item.target = None
    elif scenario == "empty":
        items = []
    settings = PerformanceSettings(backtest_weight_type="ts", backtest_fee_rate=0)
    full = calculate_performance(items, settings, range_key)

    def forbidden(*args, **kwargs):
        pytest.fail("账户快速路径不应构建或运行回测")

    monkeypatch.setattr("tests.unit.server._performance_oracle.build_wbt_input", forbidden)
    monkeypatch.setattr("tests.unit.server._performance_oracle._portfolio_daily", forbidden)
    quick = calculate_performance(items, settings, range_key, include_backtest=False)
    assert not quick["backtest_included"]
    assert quick["baseline"] == full["baseline"]
    assert quick["end"] == full["end"]
    assert quick["invalid_asset_count"] == full["invalid_asset_count"]
    assert quick["observation_count"] == full["observation_count"]
    assert [(p["date"], p["observed_at"], p["account_return"], p["account_daily_return"]) for p in quick["points"]] == [
        (p["date"], p["observed_at"], p["account_return"], p["account_daily_return"]) for p in full["points"]
    ]
    assert all(
        p["portfolio_return"] is None and p["portfolio_daily_return"] is None and p["difference"] is None
        for p in quick["points"]
    )
    assert quick["skips"] is None


@pytest.mark.parametrize(
    "source,expected", [("dummy", 100.0), ("real", 100.0), ("assumed", None), ("error", None), ("unavailable", None)]
)
def test_only_explicit_degraded_asset_sources_are_excluded(source, expected):
    record = SimpleNamespace(
        id=1,
        execution_id="x",
        created_at="2026-01-01",
        input={},
        result={"account_assets": {"total_asset": 100, "source": source}},
    )
    assert observation(record).asset == expected


def test_migration_roundtrip_defaults(tmp_path):
    path = tmp_path / "migration.db"
    initialize_native_database(path, revision="0010")
    with _connection(path) as connection:
        _insert_account(connection, id=2)
    initialize_native_database(path, revision="0011")
    with _connection(path) as connection:
        assert tuple(connection.execute("SELECT backtest_weight_type, backtest_fee_rate FROM account").fetchone()) == (
            "ts",
            0,
        )
    native_store_call(path, "migration_downgrade", {"public": "0010"})
    with _connection(path) as connection:
        assert connection.execute("SELECT id FROM account").fetchone()[0] == 2
        assert "backtest_weight_type" not in {row["name"] for row in connection.execute("PRAGMA table_info(account)")}
    initialize_native_database(path, revision="0011")
    with _connection(path) as connection:
        assert tuple(connection.execute("SELECT backtest_weight_type, backtest_fee_rate FROM account").fetchone()) == (
            "ts",
            0,
        )


def test_routes_persist_settings_across_sessions_without_scheduler(tmp_path, schema_template, monkeypatch):
    path = tmp_path / "analysis.db"

    async def setup():
        async with database(tmp_path, schema_template, count=0) as (_, sessions, _):
            with closing(sqlite3.connect(path)) as connection, connection:
                insert_account(connection, build_account(id=3, account_control_preset="default"))
                connection.execute(
                    "INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES (2,NULL,'2026-01-02T09:00:00')"
                )
            async with sessions() as session:
                await persist_records(
                    session,
                    [
                        analysis_record(
                            id=i + 1,
                            execution_id=f"routes-{i}",
                            account_id=2,
                            is_success=1,
                            created_at=f"2026-01-0{i + 1}T09:00:00",
                            input={"curr_target": {"A": 1}},
                            result={
                                "account_assets": {"total_asset": price},
                                "first_ticks": {"A": {"bid_price": price, "ask_price": price}},
                            },
                        )
                        for i, price in enumerate([100, 110, 99])
                    ],
                )

    asyncio.run(setup())
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    monkeypatch.setattr(account_performance, "database_path", lambda: str(path))
    monkeypatch.setattr(account_performance, "analysis_store", lambda: _core.Store(str(path)))
    app = FastAPI()
    app.include_router(router, prefix="/account")

    with TestClient(app) as client:
        # worker 重建前仅同步路径可用;带回测读取依赖快照发布,写路径阶段恢复。
        initial = client.get("/account/performance/2?include_backtest=false").json()
        assert initial["backtest_included"] is False
        assert initial["settings"] == {"backtest_weight_type": "cs", "backtest_fee_rate": 0}
        assert initial["bindings"][0]["portfolio_id"] is None
        settings = {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002}
        response = client.patch("/account/performance-settings/2", json=settings)
        assert response.status_code == 200, response.text
        assert response.json() == settings
        assert client.post("/account/performance/2/refresh").status_code == 202
    with TestClient(app) as client:
        assert client.get("/account/performance/2?include_backtest=false").json()["settings"] == settings
        assert (
            client.get("/account/performance/3?include_backtest=false").json()["settings"]["backtest_weight_type"]
            == "cs"
        )
        assert client.get("/account/performance/999").status_code == 404
        assert client.get("/account/performance/2?range=bad").status_code == 422
        assert (
            client.patch(
                "/account/performance-settings/2", json={**settings, "backtest_weight_type": "bad"}
            ).status_code
            == 422
        )
