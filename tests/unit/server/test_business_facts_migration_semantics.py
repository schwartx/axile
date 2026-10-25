"""Independent historical fixtures for the business fact storage migration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import wraps
from pathlib import Path

import pytest
import sqlalchemy as sa

from scripts.business_facts_contract import business_evidence
from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.unit.server._business_facts_test_support import audit_artifacts

_CREATED_AT = "2026-01-02T09:30:00.123456+08:00"
_BUSINESS_MAP_FIELDS = frozenset(
    {
        "curr_target",
        "last_target",
        "strategy_target",
        "account_target",
        "raw_weights",
        "normalized_weights",
        "symbol_results",
        "first_ticks",
        "target_volume",
    }
)


def _run_async(function):
    """Use the repository's asyncio.run convention without an extra pytest plugin."""

    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def _legacy_order(**overrides):
    """Build a supplied historical order without normalizing identity or values."""
    return {
        "order_id": "same-native-id",
        "symbol": "A/B~C",
        "direction": "BUY",
        "order_type": "LIMIT",
        "status": "PARTIAL",
        "volume": 2,
        "price": 0.0,
        "filled_volume": 0,
        "avg_price": None,
        "create_time": "2026-01-02 01:30:00.000000Z",
        "update_timestamp": 0,
        "update_time": " 2026-01-02T01:30:00+00:00 ",
        "extra": {"exchange": "native", "trading_day": "20260102"},
        **overrides,
    }


def _legacy_trade(**overrides):
    """Keep supplied amount and fee evidence even when arithmetic differs."""
    return {
        "trade_id": "",
        "order_id": "same-native-id",
        "symbol": "embedded-symbol",
        "trade_time": "2026-01-02 09:30:00.123456",
        "trade_volume": 2,
        "trade_price": 3.0,
        "trade_value": 999,
        "value_conversion": {"currency": "CNY", "multiplier": 10, "reference": {"native": True}},
        "extra": {"commission": 0, "commissionAsset": "CNY", "native": {"empty": []}},
        **overrides,
    }


def _historical_documents():
    """Cover valid and degraded legacy evidence using entirely synthetic payloads."""
    basic_input = {
        "channel_type": "ctp",
        "curr_target": {"A/B~C": 0, "zero-float": 0.0},
        "last_target": {},
        "algorithm": {"name": "synthetic", "params": {"empty": {}, "nullable": None}},
        "trade_rules": {},
        "execution_timeout": 0,
    }
    rich_result = {
        "status": "PARTIAL",
        "task_status": "failed",
        "outcome": "warning",
        "outcome_reason": "synthetic incomplete execution",
        "error": None,
        "reason_code": "",
        "execution_time": 0.0,
        "success": False,
        "channel_type": "ctp",
        "inputs": deepcopy(basic_input),
        "orders": [_legacy_order(symbol="root-only"), _legacy_order(order_id="", volume=0)],
        "trades": [_legacy_trade(symbol="root-only"), _legacy_trade(trade_value=None)],
        "first_ticks": {"A/B~C": {"symbol": "root-tick", "last_price": 0, "timestamp": 0}},
        "target_volume": {"A/B~C": 3},
        "symbol_results": {
            "A/B~C": {
                "symbol": "embedded-symbol",
                "algorithm": "synthetic",
                "status": "PARTIAL",
                "target_volume": 2.0,
                "final_volume": 0,
                "orders": [_legacy_order(), _legacy_order(status="CANCELED")],
                "trades": [_legacy_trade(), _legacy_trade(trade_price=4, trade_value=0.0)],
                "first_tick": {
                    "symbol": "embedded-symbol",
                    "last_price": 0.0,
                    "bid_price1": None,
                    "ask_price1": 0,
                    "timestamp": 0,
                    "update_time": "native-time-without-timezone",
                    "book_valid": False,
                    "extra": {"native": {"empty": {}}},
                },
                "sizing": {
                    "target_weight": 0,
                    "target_quantity": 0.0,
                    "current_quantity": 0,
                    "price": None,
                    "native_rounding": {"empty": []},
                },
                "memory": {"decision": [0, None, False]},
                "unknown_symbol": {"nullable": None},
            },
            "": {"orders": [], "trades": [], "first_tick": None, "sizing": {}},
        },
        "account_assets": {
            "available_cash": 0,
            "total_asset": 0.0,
            "market_value": None,
            "currency": "",
            "update_time": " 2026-01-02T09:30:00.123456+08:00 ",
            "source": "synthetic-native",
            "positions": [
                {"symbol": "A/B~C", "volume": 0, "available_volume": None, "direction": "SHORT"},
                {"symbol": "A/B~C", "volume": 0.0, "position_attributes": {"native": [0, None]}},
            ],
            "extra": {"native_asset": []},
        },
        "memory": {"synthetic": [None, False, 0, "", [], {}]},
        "extra": {"unrecognized-channel": {"fee": None}},
        "unknown_top_level": {"ordered": {"z": 0, "a": None}},
    }
    cases = [
        ("absent", {}, {}),
        (
            "explicit-null",
            {"curr_target": None, "last_target": None, "execution_timeout": None},
            {"status": None, "error": None, "success": None, "execution_time": None, "account_assets": None},
        ),
        (
            "explicit-empty-zero",
            {"curr_target": {}, "last_target": {}, "forbidden_symbols": [], "risk_symbols": []},
            {
                "status": "",
                "error": "",
                "success": False,
                "execution_time": 0,
                "symbol_results": {},
                "orders": [],
                "trades": [],
                "first_ticks": {},
                "target_volume": {},
                "account_assets": {"total_asset": 0, "available_cash": 0.0, "positions": []},
            },
        ),
        ("root-and-symbol-occurrences", deepcopy(basic_input), rich_result),
        (
            "malformed-known-scalars",
            {"channel_type": 23, "execution_timeout": "0", "curr_target": {"A": "0.25", "B": False}},
            {
                "status": ["legacy"],
                "success": "false",
                "execution_time": "0",
                "error": {"native": 1},
                "account_assets": {"total_asset": "0", "positions": [{"symbol": 0, "volume": "0"}]},
                "symbol_results": {"A": {"target_volume": "0", "first_tick": {"last_price": "0"}}},
            },
        ),
        (
            "mixed-arrays-and-containers",
            {"curr_target": [], "last_target": 0, "risk_symbols": [None, "", False, {"legacy": True}]},
            {
                "orders": [None, "legacy-order", {}, _legacy_order()],
                "trades": [False, 0, [], _legacy_trade()],
                "symbol_results": {
                    "malformed": None,
                    "scalar": "legacy-symbol",
                    "mixed": {"orders": "bad-container", "trades": None, "first_tick": [], "sizing": 0},
                },
                "account_assets": {"positions": [None, "legacy-position", {}, {"symbol": "A", "volume": 0}]},
            },
        ),
        (
            "differing-input-snapshots",
            {"curr_target": {"A": 0.25}, "last_target": {"A": 0.5}},
            {"inputs": {"curr_target": {"A": 0.75}, "last_target": None}, "status": "FAILED"},
        ),
        (
            "precision-and-unknown-extension",
            {"curr_target": {"large": 9007199254740993, "small": 0.0000000000000001}, "unknown": [False, None]},
            {
                "symbol_results": {"A": {"target_volume": 9007199254740993}},
                "orders": [_legacy_order(volume=9007199254740993, price=0.0000000000000001)],
                "trades": [_legacy_trade(trade_volume=9007199254740993, trade_value=0.0000000000000001)],
                "unknown": {"nested": {"z": [], "a": None}},
            },
        ),
        ("null-input-root", None, {}),
        ("null-result-root", {}, None),
        (
            "legacy-zero-placeholder",
            {},
            {
                "status": "FAILED",
                "execution_time": 0,
                "inputs": None,
                "account_assets": {
                    "available_cash": 0,
                    "total_asset": 0.0,
                    "market_value": 0,
                    "positions": [],
                    "extra": {},
                },
                "symbol_results": {},
                "extra": {"worker_error": {"error": "synthetic"}},
            },
        ),
        (
            "genuine-zero-observation",
            {},
            {
                "status": "NOOP",
                "execution_time": 0,
                "inputs": None,
                "account_assets": {
                    "available_cash": 0,
                    "total_asset": 0.0,
                    "market_value": 0,
                    "positions": [],
                    "extra": {},
                },
                "symbol_results": {},
                "extra": {},
            },
        ),
    ]
    return [(name, deepcopy(raw_input), deepcopy(raw_result)) for name, raw_input, raw_result in cases]


def _channel_documents():
    """Reuse independently prepared native-channel evidence in the real migration."""
    from tests.unit.server.test_business_facts_ctp import _order as ctp_order
    from tests.unit.server.test_business_facts_ctp import _position as ctp_position
    from tests.unit.server.test_business_facts_ctp import _trade as ctp_trade
    from tests.unit.server.test_business_facts_external_channel import _asset_snapshot as custom_assets
    from tests.unit.server.test_business_facts_external_channel import _order as custom_order
    from tests.unit.server.test_business_facts_external_channel import _trade as custom_trade
    from tests.unit.server.test_business_facts_gm import _gm_assets, _gm_order, _gm_trade
    from tests.unit.server.test_business_facts_tq import tq_historical_payloads

    tq_input, tq_result = tq_historical_payloads()
    return [
        ("tq-native-evidence", tq_input, tq_result),
        (
            "ctp-native-evidence",
            {"channel_type": "CTP", "curr_target": {"rb2611": -2}},
            {
                "channel_type": "CTP",
                "status": "PARTIAL",
                "account_assets": {"total_asset": 280000.0, "positions": [ctp_position()]},
                "orders": [ctp_order()],
                "trades": [ctp_trade()],
            },
        ),
        (
            "gm-native-evidence",
            {"channel_type": "gm", "curr_target": {"600000.SH": 0.5}},
            {"channel_type": "gm", "account_assets": _gm_assets(), "orders": [_gm_order()], "trades": [_gm_trade()]},
        ),
        (
            "custom-native-evidence",
            {"channel_type": "custom", "curr_target": {"XAGCNY": -0.125}},
            {
                "channel_type": "custom",
                "account_assets": custom_assets(),
                "orders": [custom_order()],
                "trades": [custom_trade(extra={"commission": -0.0000125, "commission_asset": "HKD"})],
            },
        ),
    ]


def _apply_0034(database):
    """Run the real Rust migration chain with every test-side connection closed."""
    return initialize_native_database(database)


def _downgrade(database):
    return native_store_call(database, "migration_downgrade", {"public": "0033"})


def _schema_signature(connection):
    """Capture persistent DDL so a failed conversion must restore the full schema."""
    return connection.execute(
        sa.text("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
    ).all()


def _seed_historical_records(connection, *, channel_documents=False):
    """Seed the nullable execution identity case and all document variants."""
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["account", "executerecord"])
    connection.execute(
        metadata.tables["account"].insert(),
        {
            "id": 2,
            "name": "synthetic",
            "market": "FUTURES",
            "trade_channel": "ctp",
            "account_control_preset": "default",
            "account_config": {},
            "is_started": False,
            "cron_expr": "0 0 0 * * *",
            "brokerage": "",
            "weight_precision": 0,
            "updated_at": _CREATED_AT,
            "created_at": _CREATED_AT,
        },
    )
    expected = {}
    documents = _historical_documents()
    if channel_documents:
        documents.extend(_channel_documents())
    for record_id, (name, raw_input, raw_result) in enumerate(documents, start=1):
        execution_id = None if record_id <= 2 else f"synthetic-{record_id}"
        row = {
            "id": record_id,
            "account_id": 2,
            "execution_id": execution_id,
            "created_at": _CREATED_AT,
            "is_success": int(record_id % 2 == 0),
            "raw_input": raw_input,
            "raw_result": raw_result,
        }
        connection.execute(metadata.tables["executerecord"].insert(), row)
        expected[record_id] = {"name": name, **row}
    return expected


def _assert_same_evidence(expected, actual, path=""):
    """Compare business facts and audit evidence; representation is not a storage contract."""
    assert business_evidence(actual) == business_evidence(expected), f"{path}: business evidence changed"


def _contains_document(value, document):
    """Find forbidden valid-domain shadow copies inside any residual metadata."""
    if type(value) is type(document) and value == document:
        return True
    if isinstance(value, dict):
        return any(_contains_document(child, document) for child in value.values())
    if isinstance(value, list):
        return any(_contains_document(child, document) for child in value)
    return False


def _migration_database(tmp_path: Path, *, channel_documents=False):
    """Prepare a real 0033 database with no production payloads or credentials."""
    database = tmp_path / "business-facts-migration.db"
    initialize_native_database(database, revision="0033")
    engine = sa.create_engine(f"sqlite:///{database}", poolclass=sa.pool.NullPool)
    with engine.begin() as connection:
        expected = _seed_historical_records(connection, channel_documents=channel_documents)
    return database, engine, expected


def _seed_historical_snapshots(connection):
    """Retain standalone observations and target snapshot identities during conversion."""
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["portfolio", "account_asset_snapshot", "target_weight_snapshot"])
    connection.execute(
        metadata.tables["portfolio"].insert(),
        {
            "id": 11,
            "name": "synthetic",
            "market": "FUTURES",
            "custom_calc_py_code": "",
            "updated_at": _CREATED_AT,
            "created_at": _CREATED_AT,
        },
    )
    assets = [
        {
            "id": 101,
            "account_id": 2,
            "execution_id": None,
            "source": "manual",
            "created_at": _CREATED_AT,
            "assets": {"total_asset": 0, "available_cash": 0.0, "positions": []},
        },
        {
            "id": 102,
            "account_id": 2,
            "execution_id": None,
            "source": "manual",
            "created_at": _CREATED_AT,
            "assets": {"total_asset": None, "source": "unavailable", "positions": None, "extra": {"native": []}},
        },
        {
            "id": 103,
            "account_id": 2,
            "execution_id": "synthetic-4",
            "source": "execution",
            "created_at": _CREATED_AT,
            "assets": deepcopy(_historical_documents()[3][2]["account_assets"]),
        },
    ]
    targets = [
        {
            "id": 201,
            "portfolio_id": 11,
            "account_id": None,
            "execution_id": None,
            "source": "manual",
            "calculated_at": _CREATED_AT,
            "raw_weights": None,
            "normalized_weights": {},
        },
        {
            "id": 202,
            "portfolio_id": 11,
            "account_id": 2,
            "execution_id": None,
            "source": "manual",
            "calculated_at": _CREATED_AT,
            "raw_weights": {},
            "normalized_weights": None,
        },
        {
            "id": 203,
            "portfolio_id": 11,
            "account_id": 2,
            "execution_id": "synthetic-4",
            "source": "execution",
            "calculated_at": _CREATED_AT,
            "raw_weights": {"A/B~C": 0, "zero-float": 0.0},
            "normalized_weights": {"A/B~C": -0.25, "zero-float": 0.0},
        },
    ]
    for row in assets:
        connection.execute(metadata.tables["account_asset_snapshot"].insert(), row)
    for row in targets:
        connection.execute(metadata.tables["target_weight_snapshot"].insert(), row)
    return assets, targets


def _historical_summary(result):
    """Freeze a worker journal with stable copies and independently retained metrics."""
    from axile.server.execution.execution_summaries import build_symbol_reconciliation

    reconciliation = build_symbol_reconciliation(result, {"total_asset": 123456.75, "positions": []})
    reconciliation["account"]["equity_before"] = 123457.25
    reconciliation["symbols"][0]["tca"]["journal_metric"] = {"captured": 0, "nullable": None}
    return {
        **{
            key: deepcopy(result[key])
            for key in ("status", "error", "reason_code", "outcome", "outcome_reason", "symbol_results")
        },
        "summary": {"symbols_total": 2, "symbols_succeeded": 1, "symbols_failed": 1, "symbols_noop": 0},
        "success": False,
        "execution_time": 0.0,
        "reconciliation": reconciliation,
        "independent_journal": {"observed_at": "native-journal-time", "zero": 0, "unknown": None},
    }


def _seed_summary_journals(connection):
    """Create equivalent, differing and orphan worker journals in the old schema."""
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["executerecord", "execution_artifact"])
    raw_input, result = deepcopy(_historical_documents()[3][1:])
    summaries = {}
    records = {}
    for record_id, suffix in ((31, "exact"), (32, "different")):
        execution_id = f"synthetic-summary-{suffix}"
        records[record_id] = {
            "id": record_id,
            "account_id": 2,
            "execution_id": execution_id,
            "created_at": _CREATED_AT,
            "is_success": 0,
            "raw_input": deepcopy(raw_input),
            "raw_result": deepcopy(result),
        }
        connection.execute(metadata.tables["executerecord"].insert(), records[record_id])
        summary = _historical_summary(result)
        if suffix == "different":
            summary["symbol_results"]["A/B~C"]["orders"][0]["price"] = 987.25
        artifact_id = 500 + record_id
        summaries[artifact_id] = {
            "id": artifact_id,
            "execution_id": execution_id,
            "artifact_type": "EXECUTION_SUMMARY",
            "schema_version": 2 if suffix == "exact" else 7,
            "created_at": _CREATED_AT,
            "content": summary,
        }
    summaries[599] = {
        "id": 599,
        "execution_id": "synthetic-summary-orphan",
        "artifact_type": "EXECUTION_SUMMARY",
        "schema_version": 11,
        "created_at": _CREATED_AT,
        "content": _historical_summary(result),
    }
    for row in summaries.values():
        connection.execute(metadata.tables["execution_artifact"].insert(), row)
    return records, summaries


def _captured_notification_context(execution_id):
    """Capture business data and display strings independently of current configuration."""
    return {
        "event_id": None,
        "event_type": "execution.finished",
        "account": {"id": 2, "name": "frozen account name", "mark": "frozen-mark"},
        "execution": {
            "id": execution_id,
            "status": "PARTIAL",
            "success": False,
            "notified_at": "native-capture-time",
            "execution_time": 0.0,
            "is_test": False,
        },
        "strategy": {"captured_name": "frozen strategy"},
        "assets": {"available_cash": 0, "total_asset": 100.0, "market_value": None},
        "targets": {"curr_target": {"A/B~C": 0, "zero-float": 0.0}},
        "positions": None,
        "orders": [_legacy_order()],
        "trades": [_legacy_trade()],
        "symbols": [{"symbol": "A/B~C", "native": None}],
        "summary": {"order_count": 1, "trade_count": 1, "trade_value": 999, "native_fee": None},
        "default_feishu_variables": {"account_mark": "frozen-mark", "dt": "native-capture-time", "unknown": None},
    }


def _seed_notification_outbox_evidence(connection):
    """Keep mismatches and dispatch states independent from canonical result storage."""
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["account", "executerecord", "notification_event"])
    account = metadata.tables["account"]
    other_account = dict(connection.execute(sa.select(account).where(account.c.id == 2)).mappings().one())
    other_account.update(id=3, name="another synthetic account")
    connection.execute(account.insert(), other_account)
    raw_input, result = deepcopy(_historical_documents()[3][1:])
    records, events = {}, {}
    variants = (
        "exact",
        "metadata-only",
        "business-different",
        "number-kind-different",
        "missing-event",
        "wrong-account",
        "wrong-event-type",
        "wrong-event-id",
    )
    for ordinal, variant in enumerate(variants):
        record_id = 41 + ordinal
        execution_id = f"synthetic-notification-{variant}"
        captured = _captured_notification_context(execution_id)
        payload = deepcopy(result)
        payload["extra"] = {
            **payload["extra"],
            "server_notification_context": captured,
            "unrelated_extra": {"preserved": 0},
        }
        records[record_id] = {
            "id": record_id,
            "account_id": 2,
            "execution_id": execution_id,
            "created_at": _CREATED_AT,
            "is_success": 0,
            "raw_input": deepcopy(raw_input),
            "raw_result": payload,
        }
        connection.execute(metadata.tables["executerecord"].insert(), records[record_id])
        if variant == "missing-event":
            continue
        event_context = deepcopy(captured)
        event_context["event_id"] = f"execution:{execution_id}"
        if variant == "metadata-only":
            event_context["execution"]["supplements"] = [
                {"group_id": "frozen-group", "index": 1, "is_last": True, "end_reason": None}
            ]
            event_context["execution"]["notified_at"] = "native-outbox-time"
        elif variant == "business-different":
            event_context["assets"]["available_cash"] = 1
        elif variant == "number-kind-different":
            event_context["targets"]["curr_target"]["A/B~C"] = 0.0
        sequence = 600 + record_id
        events[sequence] = {
            "sequence": sequence,
            "id": f"execution:{execution_id}" if variant != "wrong-event-id" else f"mismatched:{execution_id}",
            "account_id": 3 if variant == "wrong-account" else 2,
            "created_at": _CREATED_AT,
            "event_type": "account_control.warning" if variant == "wrong-event-type" else "execution.finished",
            "context": event_context,
            "status": ("pending", "calling", "succeeded", "failed", "unknown")[ordinal % 5],
            "finished_at": None if ordinal < 2 else "native-outbox-finished-time",
            "error": "frozen delivery error" if ordinal == 3 else None,
        }
    events[699] = {
        "sequence": 699,
        "id": "execution:synthetic-notification-orphan",
        "account_id": 2,
        "created_at": _CREATED_AT,
        "event_type": "execution.finished",
        "context": _captured_notification_context("synthetic-notification-orphan"),
        "status": "unknown",
        "finished_at": "native-orphan-time",
        "error": "frozen unknown delivery",
    }
    for row in events.values():
        connection.execute(metadata.tables["notification_event"].insert(), row)
    return records, events, {41, 42}


@_run_async
async def test_migration_preserves_historical_evidence_and_record_identity(tmp_path):
    """Historical records retain meaning through the actual one-time migration."""
    database, engine, expected = _migration_database(tmp_path, channel_documents=True)
    try:
        _apply_0034(database)
        with engine.begin() as connection:
            columns = {column["name"] for column in sa.inspect(connection).get_columns("executerecord")}
            assert "raw_input" not in columns
            assert "raw_result" not in columns
            identities = (
                connection.execute(
                    sa.text(
                        "SELECT id, account_id, execution_id, created_at, is_success FROM executerecord ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
            assert business_evidence([dict(row) for row in identities]) == business_evidence(
                [
                    {key: source[key] for key in ("id", "account_id", "execution_id", "created_at", "is_success")}
                    for source in expected.values()
                ]
            )
            null_records = (
                connection.execute(sa.text("SELECT input_id FROM executerecord WHERE execution_id IS NULL ORDER BY id"))
                .scalars()
                .all()
            )
            assert business_evidence(len(null_records)) == business_evidence(2)
            assert None not in null_records
            assert business_evidence(len(set(null_records))) == business_evidence(2)
            asset_validity = dict(
                connection.execute(
                    sa.text(
                        "SELECT binding.record_id,asset.observation_valid FROM business_asset_binding AS binding "
                        "JOIN business_asset_snapshot AS asset ON asset.asset_id=binding.asset_id "
                        "WHERE binding.path='/account_assets' AND binding.record_id IN (11,12)"
                    )
                ).all()
            )
            assert business_evidence(asset_validity) == business_evidence({11: 0, 12: 1})

        for record_id, source in expected.items():
            detail = native_store_call(database, "read_execution_detail", {"account_id": 2, "record_id": record_id})
            assert detail is not None
            _assert_same_evidence(source["raw_input"], detail["input"], f"{source['name']}/input")
            _assert_same_evidence(source["raw_result"], detail["result"], f"{source['name']}/result")
    finally:
        engine.dispose()


def test_migration_shares_identical_input_and_keeps_differing_result_input(tmp_path):
    """Input references preserve the independently recorded input observations."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        _apply_0034(database)
        with engine.begin() as connection:
            shared = connection.execute(sa.text("SELECT input_id, result_input_id FROM executerecord WHERE id=4")).one()
            different = connection.execute(
                sa.text("SELECT input_id, result_input_id FROM executerecord WHERE id=7")
            ).one()
            assert shared.input_id is not None
            assert business_evidence(shared.input_id) == business_evidence(shared.result_input_id)
            assert different.input_id is not None
            assert different.result_input_id is not None
            assert different.input_id != different.result_input_id
    finally:
        engine.dispose()


@pytest.mark.parametrize("column", ["raw_input", "raw_result"])
@pytest.mark.parametrize("invalid", [[{"synthetic": "nonempty"}]], ids=["nonempty-array"])
def test_migration_rejects_unmappable_roots_before_dropping_legacy_columns(tmp_path, column, invalid):
    """Unsupported roots fail with safe record/path context and leave source evidence."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            table = sa.Table("executerecord", sa.MetaData(), autoload_with=connection)
            connection.execute(table.update().where(table.c.id == 12).values({column: invalid}))
            original_schema = _schema_signature(connection)
        with pytest.raises(require_native_store("migration_upgrade").NativeError) as failure:
            _apply_0034(database)
        assert column in str(failure.value)
        assert "12" in str(failure.value)
        with engine.begin() as connection:
            columns = {item["name"] for item in sa.inspect(connection).get_columns("executerecord")}
            assert {"raw_input", "raw_result"} <= columns
            original = connection.execute(sa.select(table.c[column]).where(table.c.id == 12)).scalar_one()
            _assert_same_evidence(invalid, original, column)
            assert business_evidence(_schema_signature(connection)) == business_evidence(original_schema)
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0033"
            connection.execute(table.update().where(table.c.id == 12).values({column: {}}))
        _apply_0034(database)
        with engine.begin() as connection:
            assert "raw_input" not in {item["name"] for item in sa.inspect(connection).get_columns("executerecord")}
    finally:
        engine.dispose()


@pytest.mark.parametrize("column", ["raw_input", "raw_result"])
def test_migration_rejects_invalid_json_text_without_destroying_source(tmp_path, column):
    """Damaged legacy JSON reports its source location and leaves original bytes."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"UPDATE executerecord SET {column}=:value WHERE id=12"), {"value": "{"})
            original_schema = _schema_signature(connection)
        with pytest.raises(require_native_store("migration_upgrade").NativeError) as failure:
            _apply_0034(database)
        assert column in str(failure.value)
        assert "12" in str(failure.value)
        with engine.begin() as connection:
            columns = {item["name"] for item in sa.inspect(connection).get_columns("executerecord")}
            assert {"raw_input", "raw_result"} <= columns
            assert business_evidence(
                connection.execute(sa.text(f"SELECT {column} FROM executerecord WHERE id=12")).scalar_one()
            ) == business_evidence("{")
            assert business_evidence(_schema_signature(connection)) == business_evidence(original_schema)
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0033"
            connection.execute(sa.text(f"UPDATE executerecord SET {column}=:value WHERE id=12"), {"value": "{}"})
        _apply_0034(database)
        with engine.begin() as connection:
            assert "raw_result" not in {item["name"] for item in sa.inspect(connection).get_columns("executerecord")}
    finally:
        engine.dispose()


@_run_async
@pytest.mark.parametrize("column", ["raw_input", "raw_result"])
@pytest.mark.parametrize("invalid", [[], 0, "synthetic-scalar"], ids=["empty-array", "zero", "string"])
async def test_migration_preserves_small_malformed_root_states(tmp_path, column, invalid):
    """Small malformed roots never silently turn into invented empty documents."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            table = sa.Table("executerecord", sa.MetaData(), autoload_with=connection)
            connection.execute(table.update().where(table.c.id == 1).values({column: invalid}))
        _apply_0034(database)

        detail = native_store_call(database, "read_execution_detail", {"account_id": 2, "record_id": 1})
        assert detail is not None
        _assert_same_evidence(invalid, detail[{"raw_input": "input", "raw_result": "result"}[column]], column)
    finally:
        engine.dispose()


@_run_async
async def test_migration_preserves_manual_asset_and_target_snapshot_semantics(tmp_path):
    """Standalone source observations retain identity and exact nullable weight roles."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            expected_assets, expected_targets = _seed_historical_snapshots(connection)
        _apply_0034(database)
        with engine.begin() as connection:
            inspector = sa.inspect(connection)
            assert "assets" not in {column["name"] for column in inspector.get_columns("account_asset_snapshot")}
            assert not {"raw_weights", "normalized_weights"} & {
                column["name"] for column in inspector.get_columns("target_weight_snapshot")
            }
            references = connection.execute(
                sa.text(
                    "SELECT snapshot.asset_fact_id,binding.asset_id FROM account_asset_snapshot AS snapshot "
                    "JOIN business_asset_binding AS binding ON binding.record_id=4 AND binding.path='/account_assets' "
                    "WHERE snapshot.id=103"
                )
            ).one()
            assert business_evidence(references.asset_fact_id) == business_evidence(references.asset_id)
            metadata = sa.MetaData()
            metadata.reflect(connection, only=["target_weight_snapshot", "target_weight_snapshot_weight"])
            parent = metadata.tables["target_weight_snapshot"]
            child = metadata.tables["target_weight_snapshot_weight"]
            target_heads = {row["id"]: row for row in connection.execute(sa.select(parent)).mappings()}
            target_children = connection.execute(sa.select(child).order_by(child.c.map_key.desc())).mappings().all()
            for source in expected_targets:
                head = target_heads[source["id"]]
                assert business_evidence(
                    {key: head[key] for key in source if key not in {"raw_weights", "normalized_weights"}}
                ) == business_evidence(
                    {key: value for key, value in source.items() if key not in {"raw_weights", "normalized_weights"}}
                )
                weights = {}
                for role in ("raw_weights", "normalized_weights"):
                    assert head[f"{role}_kind"] in {"null", "mapping"}
                    weights[role] = (
                        None
                        if head[f"{role}_kind"] == "null"
                        else {
                            row["map_key"]: row["value"]
                            for row in target_children
                            if row["snapshot_id"] == source["id"] and row["role"] == role
                        }
                    )
                _assert_same_evidence(
                    {key: source[key] for key in ("raw_weights", "normalized_weights")},
                    weights,
                    f"snapshot/{source['id']}/weights",
                )

        with engine.begin() as connection:
            snapshots = sa.Table("account_asset_snapshot", sa.MetaData(), autoload_with=connection)
            heads = {row["id"]: dict(row) for row in connection.execute(sa.select(snapshots)).mappings()}
            facts = sa.Table("business_asset_snapshot", sa.MetaData(), autoload_with=connection)
            observations = {row["asset_id"]: dict(row) for row in connection.execute(sa.select(facts)).mappings()}
        for source in expected_assets:
            snapshot = heads.get(source["id"])
            assert snapshot is not None
            assert snapshot["asset_fact_id"] is not None
            assert business_evidence({key: snapshot[key] for key in source if key != "assets"}) == business_evidence(
                {key: value for key, value in source.items() if key != "assets"}
            )
            assert snapshot["asset_fact_id"] in observations
        unavailable = observations[heads[102]["asset_fact_id"]]
        assert unavailable["account_snapshot_id"] == 102
        assert unavailable["observation_valid"] is False
        assert unavailable["equity_state"] == "null"
        assert unavailable["positions_kind"] == "null"
        assert unavailable["source"] == "unavailable"
        assert unavailable["extra"] == {"native": []}

        # 独立无效观测不会出现在可信资产页，往返必须仍保留全部原文证据。
        _downgrade(database)
        with engine.begin() as connection:
            metadata = sa.MetaData()
            metadata.reflect(connection, only=["account_asset_snapshot", "target_weight_snapshot"])
            for name, expected in (
                ("account_asset_snapshot", expected_assets),
                ("target_weight_snapshot", expected_targets),
            ):
                restored = {
                    row["id"]: dict(row) for row in connection.execute(sa.select(metadata.tables[name])).mappings()
                }
                for source in expected:
                    _assert_same_evidence(source, restored[source["id"]], f"{name}/{source['id']}/roundtrip")
    finally:
        engine.dispose()


def test_migration_stores_valid_stable_domains_in_columns_without_shadow_documents(tmp_path):
    """Evidence reconstruction alone cannot excuse storing whole known trees in JSON."""
    database, engine, expected = _migration_database(tmp_path)
    try:
        _apply_0034(database)
        with engine.begin() as connection:
            for table, expected_count in (
                ("execution_order", 4),
                ("business_trade", 4),
                ("execution_symbol_result", 2),
            ):
                assert business_evidence(
                    connection.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE record_id=4")).scalar_one()
                ) == business_evidence(expected_count)
            assert business_evidence(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM execution_order WHERE record_id=4 AND symbol IS NOT NULL AND volume IS NOT NULL"
                    )
                ).scalar_one()
            ) == business_evidence(4)
            assert business_evidence(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM business_trade WHERE record_id=4 AND trade_volume IS NOT NULL AND trade_price IS NOT NULL"
                    )
                ).scalar_one()
            ) == business_evidence(4)
            assert business_evidence(
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM execution_symbol_tick WHERE record_id=4 AND last_price IS NOT NULL")
                ).scalar_one()
            ) == business_evidence(2)
            assert business_evidence(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM execution_input_target WHERE role='curr_target' "
                        "AND input_id=(SELECT input_id FROM executerecord WHERE id=4) AND value IS NOT NULL"
                    )
                ).scalar_one()
            ) == business_evidence(2)
            assert business_evidence(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM business_asset_position AS position "
                        "JOIN business_asset_binding AS binding ON binding.asset_id=position.asset_id "
                        "WHERE binding.record_id=4 AND binding.path='/account_assets' AND position.volume IS NOT NULL"
                    )
                ).scalar_one()
            ) == business_evidence(2)

            raw_input, result = expected[4]["raw_input"], expected[4]["raw_result"]
            nested = result["symbol_results"]["A/B~C"]
            forbidden_documents = [
                raw_input,
                result,
                result["symbol_results"],
                nested,
                result["orders"],
                result["trades"],
                result["account_assets"],
                result["account_assets"]["positions"],
                nested["orders"],
                nested["trades"],
                nested["first_tick"],
                nested["sizing"],
            ]
            metadata = sa.MetaData()
            names = [
                name
                for name in sa.inspect(connection).get_table_names()
                if name == "executerecord" or name.startswith(("execution_", "business_", "target_weight_snapshot"))
            ]
            metadata.reflect(connection, only=names)
            for table in metadata.tables.values():
                for column in table.c:
                    if not isinstance(column.type, sa.JSON):
                        continue
                    for residual in connection.execute(sa.select(column)).scalars():
                        assert not any(_contains_document(residual, document) for document in forbidden_documents), (
                            f"{table.name}.{column.name} contains a complete valid business subtree"
                        )
    finally:
        engine.dispose()


@_run_async
async def test_migration_freezes_full_summary_and_restores_original_journal_wire(tmp_path):
    """Compact verified stable copies while retaining differing and orphan journal evidence."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            records, originals = _seed_summary_journals(connection)
        _apply_0034(database)
        with engine.begin() as connection:
            table = sa.Table("execution_artifact", sa.MetaData(), autoload_with=connection)
            compacted = {
                row["id"]: dict(row) for row in connection.execute(sa.select(table).order_by(table.c.id)).mappings()
            }
            exact = compacted[531]["content"]
            assert "business_facts_ref" not in exact
            assert compacted[531]["schema_version"] == originals[531]["schema_version"]
            assert "orders" in exact["symbol_results"]["A/B~C"]
            assert "trades" in exact["symbol_results"]["A/B~C"]
            assert exact["reconciliation"] == originals[531]["content"]["reconciliation"]
            assert business_evidence(exact["reconciliation"]["account"]["equity_before"]) == business_evidence(
                123457.25
            )
            _assert_same_evidence(
                originals[532]["content"]["symbol_results"]["A/B~C"]["orders"],
                compacted[532]["content"]["symbol_results"]["A/B~C"]["orders"],
                "different-journal/orders",
            )
            _assert_same_evidence(originals[599], compacted[599], "orphan-journal")

        for artifact_id, record_id in ((531, 31), (532, 32)):
            result = native_store_call(database, "read_execution_result", {"account_id": 2, "record_id": record_id})
            hydrated = native_store_call(
                database, "resolve_execution_artifact", {"artifact": compacted[artifact_id], "account_id": 2}
            )
            hydrated.pop("business_facts_ref", None)
            _assert_same_evidence(originals[artifact_id]["content"], hydrated, "summary-cold-wire")
            resolved = audit_artifacts(database, records[record_id]["execution_id"])["data"][0]["content"]
            resolved.pop("business_facts_ref", None)
            _assert_same_evidence(originals[artifact_id]["content"], resolved, "summary-owned-cold-read")
            _assert_same_evidence(records[record_id]["raw_result"], result, "summary-source-result")

        _downgrade(database)
        with engine.begin() as connection:
            table = sa.Table("execution_artifact", sa.MetaData(), autoload_with=connection)
            restored = {row["id"]: dict(row) for row in connection.execute(sa.select(table)).mappings()}
            for artifact_id, original in originals.items():
                _assert_same_evidence(original, restored[artifact_id], "summary-downgrade-wire")
    finally:
        engine.dispose()


@_run_async
async def test_migration_notification_references_require_matching_frozen_business_context(tmp_path):
    """Reference frozen outbox data without rewriting delivery state or mismatched captures."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            records, original_events, _ = _seed_notification_outbox_evidence(connection)
        _apply_0034(database)
        with engine.begin() as connection:
            metadata = sa.MetaData()
            metadata.reflect(connection, only=["execution_extension", "notification_event"])
            extensions = {
                row["record_id"]: row["extra"]
                for row in connection.execute(sa.select(metadata.tables["execution_extension"])).mappings()
            }
            events = {
                row["sequence"]: dict(row)
                for row in connection.execute(sa.select(metadata.tables["notification_event"])).mappings()
            }
            _assert_same_evidence(original_events, events, "outbox-after-upgrade")
            for record_id, source in records.items():
                captured = extensions[record_id]["server_notification_context"]
                assert "$axile_notification_context" not in captured
                _assert_same_evidence(
                    source["raw_result"]["extra"]["server_notification_context"], captured, "full-notification-capture"
                )
                _assert_same_evidence(
                    source["raw_result"]["extra"]["unrelated_extra"],
                    extensions[record_id]["unrelated_extra"],
                    "notification-extra",
                )

        for record_id, source in records.items():
            result = native_store_call(database, "read_execution_result", {"account_id": 2, "record_id": record_id})
            _assert_same_evidence(source["raw_result"], result, "notification-cold-wire")

        _downgrade(database)
        with engine.begin() as connection:
            metadata = sa.MetaData()
            metadata.reflect(connection, only=["executerecord", "notification_event"])
            restored = {
                row["id"]: row["raw_result"]
                for row in connection.execute(sa.select(metadata.tables["executerecord"])).mappings()
            }
            for record_id, source in records.items():
                _assert_same_evidence(source["raw_result"], restored[record_id], "notification-downgrade-wire")
            events = {
                row["sequence"]: dict(row)
                for row in connection.execute(sa.select(metadata.tables["notification_event"])).mappings()
            }
            _assert_same_evidence(original_events, events, "outbox-after-downgrade")
    finally:
        engine.dispose()


def test_failed_migration_restores_summary_and_notification_evidence_before_retry(tmp_path, monkeypatch):
    """A late failure rolls back already compacted evidence before a successful retry."""
    database, engine, _ = _migration_database(tmp_path)
    try:
        with engine.begin() as connection:
            _seed_summary_journals(connection)
            _seed_notification_outbox_evidence(connection)
            # 版本落盘时失败，已转换的事实、摘要、通知引用和 DDL 必须一并回滚。
            connection.exec_driver_sql(
                "CREATE TRIGGER fail_after_backfill BEFORE UPDATE OF version_num ON alembic_version "
                "WHEN NEW.version_num='0034' "
                "BEGIN SELECT RAISE(ABORT,'synthetic evidence verification failure'); END"
            )
            tables = {
                name: sa.Table(name, sa.MetaData(), autoload_with=connection)
                for name in ("execution_artifact", "notification_event", "executerecord")
            }
            original_rows = {
                name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                for name, table in tables.items()
            }
            original_schema = _schema_signature(connection)
        with pytest.raises(
            require_native_store("migration_upgrade").NativeError, match="synthetic evidence verification failure"
        ):
            _apply_0034(database)
        with engine.begin() as connection:
            assert business_evidence(_schema_signature(connection)) == business_evidence(original_schema)
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0033"
            for name, table in tables.items():
                current = [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                _assert_same_evidence(original_rows[name], current, f"rollback/{name}")
            connection.exec_driver_sql("DROP TRIGGER fail_after_backfill")
        _apply_0034(database)
        with engine.begin() as connection:
            assert "raw_result" not in {
                column["name"] for column in sa.inspect(connection).get_columns("executerecord")
            }
    finally:
        engine.dispose()
