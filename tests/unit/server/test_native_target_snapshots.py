"""目标快照原生边界保留异常证据和空值语义。"""

import asyncio
import math
from unittest.mock import AsyncMock

from axile.server import target_weight_snapshots as snapshots


def test_append_preserves_nonfinite_evidence_and_native_symbol_key(monkeypatch):
    marker = {"invalid_number": "inf"}
    raw_weights = {" SHFE.rb2610 ": float("inf"), "literal_marker": marker}
    value = {
        "id": 3,
        "portfolio_id": 7,
        "account_id": None,
        "raw_weights": {" SHFE.rb2610 ": None, "literal_marker": None},
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
            raw_weights=raw_weights,
            normalized_weights=None,
            source="manual",
        )
    )

    assert saved["raw_weights"] == {" SHFE.rb2610 ": None, "literal_marker": None}
    native.assert_awaited_once_with(
        "target_operation",
        {
            "op": "append",
            "portfolio_id": 7,
            "account_id": None,
            "raw_weights": raw_weights,
            "normalized_weights": None,
            "source": "manual",
            "execution_id": None,
            "calculated_at": "clock-time",
        },
    )
    forwarded = native.await_args.args[1]["raw_weights"]
    assert isinstance(forwarded[" SHFE.rb2610 "], float) and math.isinf(forwarded[" SHFE.rb2610 "])
    assert isinstance(forwarded["literal_marker"], dict) and forwarded["literal_marker"] == marker


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

    value = asyncio.run(snapshots.get_latest_account_target_snapshot(2, 7))
    assert value is not None and value["raw_weights"] == {} and value.get("normalized_weights") is None
    assert snapshots.target_snapshot_public(value, weight_kind="raw").weights == {}
