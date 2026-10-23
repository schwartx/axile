"""目标快照原生边界保留异常证据和空值语义。"""

import asyncio
from unittest.mock import AsyncMock

from axile.server import target_weight_snapshots as snapshots


def test_append_preserves_nonfinite_evidence_and_native_symbol_key(monkeypatch):
    value = {
        "id": 3,
        "portfolio_id": 7,
        "account_id": None,
        "raw_weights": {" SHFE.rb2610 ": None},
        "normalized_weights": None,
        "source": "manual",
        "execution_id": None,
        "calculated_at": "clock-time",
    }
    native = AsyncMock(return_value=value)
    monkeypatch.setattr(snapshots, "native_operation", native)
    monkeypatch.setattr(snapshots, "now_str", lambda: "clock-time")

    saved = asyncio.run(
        snapshots.append_target_weight_snapshot(
            portfolio_id=7,
            account_id=None,
            raw_weights={" SHFE.rb2610 ": float("inf")},
            normalized_weights=None,
            source="manual",
        )
    )

    assert saved.raw_weights == {" SHFE.rb2610 ": None}
    native.assert_awaited_once_with(
        "target_operation",
        {
            "op": "append",
            "portfolio_id": 7,
            "account_id": None,
            "raw_weights": {" SHFE.rb2610 ": {"invalid_number": "inf"}},
            "normalized_weights": None,
            "source": "manual",
            "execution_id": None,
            "calculated_at": "clock-time",
        },
    )


def test_execution_without_identity_skips_database_and_empty_target_is_preserved(monkeypatch):
    native = AsyncMock(return_value={})
    monkeypatch.setattr(snapshots, "native_operation", native)

    assert asyncio.run(snapshots.read_target_snapshot_weights(account_id=2, execution_id=None)) is None
    native.assert_not_awaited()
    assert asyncio.run(snapshots.read_target_snapshot_weights(account_id=2, execution_id="e1")) == {}
    native.assert_awaited_once_with(
        "target_operation",
        {
            "op": "read_execution",
            "account_id": 2,
            "execution_id": "e1",
        },
    )


def test_latest_snapshot_preserves_absent_normalized_weights(monkeypatch):
    native = AsyncMock(
        return_value={
            "id": 1,
            "portfolio_id": 7,
            "account_id": None,
            "raw_weights": {},
            "source": "manual",
            "execution_id": None,
            "calculated_at": "t",
        }
    )
    monkeypatch.setattr(snapshots, "native_operation", native)

    value = asyncio.run(snapshots.get_latest_portfolio_target_snapshot(7))
    assert value is not None and value.raw_weights == {} and value.normalized_weights is None
    assert snapshots.target_snapshot_public(value, weight_kind="raw").weights == {}
