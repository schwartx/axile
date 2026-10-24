"""资产可信度、历史占位和原生过滤口径保持一致。"""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest

from axile.server._native import _core
from axile.server.asset_observations import is_asset_observation
from tests.unit.server.test_native_asset_operations import _call, _save
from tests.unit.server.test_native_asset_operations import asset_database as asset_database


def legacy():
    return dict(
        account_assets=dict(available_cash=0, total_asset=0, market_value=0, positions=[], extra={}, source="real"),
        status="FAILED",
        extra={"worker_error": {"type": "ValueError", "message": "任意错误"}},
        inputs=None,
        execution_time=0,
        symbol_results={},
    )


def cases():
    base = legacy()
    yield base, False
    for key in ("inputs", "symbol_results"):
        yield {**base, key: "{}"}, True
    for key, value in [("positions", "[]"), ("extra", "{}")]:
        yield {**base, "account_assets": {**base["account_assets"], key: value}}, True
    missing = deepcopy(base)
    del missing["account_assets"]["source"]
    yield missing, False
    for key, value in [
        ("extra", {}),
        ("inputs", {"curr_target": {}}),
        ("execution_time", 1),
        ("status", "SUCCEEDED"),
        ("symbol_results", {"A": {}}),
    ]:
        yield {**base, key: value}, True
    for key, value, valid in [
        ("total_asset", 100, True),
        ("extra", {"balance": 0}, True),
        ("positions", [{}], True),
        ("source", "unavailable", False),
        ("source", None, True),
        ("source", 0, True),
        ("available_cash", "0", True),
        ("market_value", False, True),
    ]:
        yield {**base, "account_assets": {**base["account_assets"], key: value}}, valid
    blocked = {
        **base,
        "status": "BLOCKED",
        "outcome": "BLOCKED",
        "outcome_reason": "当前不在交易时间",
        "error": "当前不在交易时间",
        "memory": {"message": "当前不在交易时间"},
        "inputs": {"curr_target": {}},
        "execution_time": 0.25,
    }
    yield blocked, False
    yield {**blocked, "execution_time": -1}, True
    yield {**blocked, "memory": {"message": "当前不在交易时间", "other": 1}}, True


@pytest.mark.parametrize("result,expected", list(cases()))
def test_native_observation_validity_matches_boundary_cases(asset_database, result, expected):
    snapshot = _call(
        asset_database,
        "asset_snapshot_write",
        {
            "account_id": 1,
            "created_at": "observed-time",
            "account_assets": result["account_assets"],
            "result": result,
        },
    )
    with closing(sqlite3.connect(asset_database)) as connection, connection:
        valid = connection.execute(
            "SELECT observation_valid FROM business_asset_snapshot WHERE asset_id=?", (snapshot["asset_fact_id"],)
        ).fetchone()[0]
    assert bool(valid) == expected


@pytest.mark.parametrize(
    "value,valid",
    [
        (0, True),
        (100000, True),
        (None, False),
        ("0", False),
        (False, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
def test_asset_validity(value, valid):
    assert is_asset_observation({"total_asset": value}) == valid
    for source in ("assumed", "error", "unavailable"):
        assert not is_asset_observation({"total_asset": value, "source": source})


def test_placeholders_are_excluded_before_latest_selection_and_pagination(asset_database):
    for account_id in (1, 2):
        _save(asset_database, {"total_asset": 100000, "positions": []}, account_id=account_id)
        for index in range(25):
            result = legacy()
            _call(
                asset_database,
                "asset_snapshot_write",
                {
                    "account_id": account_id,
                    "created_at": str(index),
                    "account_assets": result["account_assets"],
                    "result": result,
                },
            )
        latest = _call(asset_database, "asset_snapshot_latest", {"account_id": account_id})
        assert latest["assets"]["total_asset"] == 100000
    store = _core.Store(str(asset_database))
    assert json.loads(_core.asset_snapshots(store, 1, 0, 1))["count"] == 1
    assert json.loads(_core.asset_snapshots(store, 1, 1, 1))["data"] == []
    zero = _save(asset_database, {"total_asset": 0})
    _save(asset_database, {"total_asset": 500, "source": "unavailable"})
    latest = _call(asset_database, "asset_snapshot_latest", {"account_id": 1})
    assert latest["id"] == zero["id"]
    assert latest["assets"]["total_asset"] == 0
    assert json.loads(_core.asset_snapshots(store, 1, 0, 1))["count"] == 2


def test_non_trading_producer_does_not_claim_zero_balance():
    from types import SimpleNamespace

    from axile.common.trade_channel import TradeChannel
    from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
    from axile.executor.models.unified_input import GMAccountConfig, UnifiedStandardInput

    executor = SimpleNamespace(
        channel_type=TradeChannel.GM,
        require_execution_runtime=lambda: SimpleNamespace(elapsed_seconds=lambda: 0.1),
    )
    result = AbstractExecutorCapabilityMixin._create_non_trading_output(
        executor,
        UnifiedStandardInput(
            channel_type=TradeChannel.GM,
            account_config=GMAccountConfig(
                account_id="test", token="test", connection_mode="terminal", terminal_path="C:/gm"
            ),
            curr_target={"SHSE.600000": 1},
        ),
    )
    assert result.status == "BLOCKED"
    assert result.error == "非交易时段"
    assert result.reason_code == "COMMON.SESSION.CLOSED"
    assert result.account_assets.source == "unavailable"
    assert not is_asset_observation(result.account_assets.model_dump())
