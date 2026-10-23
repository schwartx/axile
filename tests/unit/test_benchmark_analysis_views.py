"""Protect benchmark semantics and measurement boundaries."""

import socket
import sqlite3

import pytest

from scripts.benchmark_analysis_views import (
    aggregate,
    inspect_database,
    network_guard,
    new_semantics,
    old_semantics,
    same,
    table_rows,
)


def test_semantic_comparison_preserves_unknown_zero_boolean_and_shape():
    same({"value": 1.0, "fees": {"USD": -0.001}}, {"value": 1 + 1e-13, "fees": {"USD": -0.001}})
    for left, right in ((None, 0), (False, 0), ({}, {"value": None}), ([1, 2], [2, 1])):
        with pytest.raises(AssertionError, match="Semantic"):
            same(left, right)
    with pytest.raises(AssertionError):
        same(10**15, 10**15 + 1)
    with pytest.raises(AssertionError):
        same({"time": 1.7e12}, {"time": 1.7e12 + 1})


def test_wire_table_requires_exact_row_shape():
    assert table_rows({"columns": ["currency", "value"], "rows": [["USD", 0], ["CNY", None]]}) == [
        {"currency": "USD", "value": 0},
        {"currency": "CNY", "value": None},
    ]
    with pytest.raises(ValueError):
        table_rows({"columns": ["currency", "value"], "rows": [["USD"]]})


def test_network_guard_rejects_external_connections_before_system_call():
    with network_guard(12345), socket.socket() as connection:
        for address in (("203.0.113.1", 80), ("127.0.0.1", 12346)):
            with pytest.raises(AssertionError, match="outbound"):
                connection.connect(address)
            with pytest.raises(AssertionError, match="outbound"):
                connection.connect_ex(address)


def test_common_semantics_preserve_daily_presence_fees_and_unknown_holdings():
    summary = {
        "value": None,
        "cost": 0.0,
        "lossBp": None,
        "coverage": None,
        "covered": 1,
        "count": 2,
        "amountComplete": False,
        "feeCovered": 1,
        "fees": {"USD": 0.0},
    }
    calendar = {
        "status": "partial",
        "calendar_id": None,
        "label": None,
        "closed_ranges": [],
        "unavailable_ranges": [{"start": "2026-01-01", "end": "2026-01-02"}],
    }
    old = {
        "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
        "result": {
            "points": [],
            "calendar": calendar,
            "executions": [
                {
                    "record": {"id": 1, "execution_id": "e1", "created_at": "2026-01-02T01:00:00Z", "is_success": 1},
                    "noop": False,
                    "durationSec": None,
                    "symbolCount": None,
                    "positionCount": None,
                    "positions": None,
                    "reason": None,
                    "summary": summary,
                    "transactions": [],
                    "attempts": [],
                }
            ],
        },
        "daily_costs": {"2026-01-02": {"value": None, "fees": {"USD": 0.0}}},
        "events": [],
        "event_count": 0,
    }
    chart = {
        "settings": old["settings"],
        "header": {"calendar": {key: calendar[key] for key in ("status", "calendar_id", "label")}, "event_count": 0},
        "tables": {name: {"columns": [], "rows": []} for name in ("points", "bindings", "fallbacks", "events")},
    }
    chart["tables"].update(
        {
            "calendar_ranges": {
                "columns": ["kind", "start", "end"],
                "rows": [["unavailable", "2026-01-01", "2026-01-02"]],
            },
            "daily_costs": {"columns": ["day", "value", "present"], "rows": [["2026-01-02", None, 1]]},
            "daily_fees": {"columns": ["day", "currency", "value"], "rows": [["2026-01-02", "USD", 0.0]]},
            "execution_fees": {"columns": ["record_id", "currency", "value"], "rows": [[1, "USD", 0.0]]},
            "execution_index": {
                "columns": [
                    "record_id",
                    "execution_id",
                    "created_at",
                    "time",
                    "has_transactions",
                    "is_success",
                    "noop",
                    "duration_sec",
                    "symbol_count",
                    "position_count",
                    "positions_present",
                    "reason",
                    "value",
                    "cost",
                    "loss_bp",
                    "coverage",
                    "covered",
                    "count",
                    "amount_complete",
                    "fee_covered",
                ],
                "rows": [
                    [
                        1,
                        "e1",
                        "2026-01-02T01:00:00Z",
                        1767315600000.0,
                        False,
                        1,
                        False,
                        None,
                        None,
                        None,
                        False,
                        None,
                        None,
                        0.0,
                        None,
                        None,
                        1,
                        2,
                        False,
                        1,
                    ]
                ],
            },
        }
    )
    detail = {
        "header": {"positions_present": False},
        "tables": {
            name: {"columns": [], "rows": []} for name in ("positions", "attempts", "transactions", "transaction_fees")
        },
    }
    same(old_semantics(old, 1), new_semantics(chart, detail))
    detail["header"]["positions_present"] = True
    with pytest.raises(AssertionError, match="detail.positions"):
        same(old_semantics(old, 1), new_semantics(chart, detail))


def test_frozen_database_inspection_counts_source_trades_via_account(tmp_path):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE account_analysis(account_id INTEGER, current_snapshot TEXT);
            CREATE TABLE performance_snapshot(id TEXT, read_view_schema_version INTEGER);
            CREATE TABLE performance_cost_execution(snapshot_id TEXT, record_id INTEGER, time REAL);
            CREATE TABLE performance_result_range(snapshot_id TEXT, range_key TEXT, baseline TEXT, end TEXT);
            CREATE TABLE analysis_source_execution(record_id INTEGER, account_id INTEGER);
            CREATE TABLE analysis_source_trade(record_id INTEGER);
            INSERT INTO account_analysis VALUES(7,'s1');
            INSERT INTO performance_snapshot VALUES('s1',1);
            INSERT INTO performance_cost_execution VALUES('s1',1,10),('s1',2,20),('other',9,50);
            INSERT INTO performance_result_range VALUES('s1','all','1970-01-01T08:00:00','1970-01-01T08:00:00.015');
            INSERT INTO analysis_source_execution VALUES(1,7),(2,7),(3,8);
            INSERT INTO analysis_source_trade VALUES(1),(1),(2),(3);
        """)
    result = inspect_database(path, 7, "all", None)
    assert result["snapshot_id"] == "s1"
    assert result["record_id"] == 1
    assert result["row_counts"]["performance_cost_execution"] == 2
    assert result["row_counts"]["analysis_source_trade"] == 3


def test_report_does_not_claim_zero_rss_when_a_fast_request_has_no_sample():
    results = []
    for arm, endpoint in (("full", "full_snapshot"), ("views", "chart")):
        for mode in ("timing", "rss"):
            results.append(
                {
                    "arm": arm,
                    "mode": mode,
                    "samples": [
                        {
                            "endpoint": endpoint,
                            "iteration": 0,
                            "seconds": 0.001,
                            "bytes": 10,
                            "sampled_combined_peak_bytes": None,
                        }
                    ],
                }
            )
    metrics = aggregate(results)
    assert metrics["full"]["full_snapshot"]["sampled_combined_peak_bytes"] is None
    assert metrics["views"]["chart"]["rss_requests_with_samples"] == 0
