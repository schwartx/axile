"""实时结构与录制时间契约共用真值表，不连接交易渠道。"""

import json
from pathlib import Path

import pytest

from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.quote_quality import quote_observation

CASES = json.loads((Path(__file__).parents[3] / "fixtures/execution_replay/quote-quality.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_runtime_and_recording_share_structure_and_receipt_expiry(case, monkeypatch):
    monkeypatch.setattr("axile.executor.quote_quality.clock_now_us", lambda: 1_000_000)

    def level(rows):
        return [dict(price=p, quantity=q) for p, q in rows]

    recorded = quote_observation(level(case["bids"]), level(case["asks"]), validity_us=case["validity_us"])
    runtime = UnifiedPriceData(
        symbol="X", bids=case["bids"], asks=case["asks"], last_price=100, volume=0, timestamp=1, update_time=""
    )
    assert runtime.book_valid is recorded["book_valid"] is case["book_valid"]
    assert recorded["received_time_us"] == 1_000_000
    assert recorded["valid_until_us"] == (None if case["validity_us"] is None else 1_000_000 + case["validity_us"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_malformed_values_cannot_be_a_valid_book(bad):
    from axile.executor.quote_quality import valid_book

    assert not valid_book([(99, bad)], [(101, 1)])
