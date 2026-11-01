"""GM 与前端共用真值经真实 MCAP 回读，不导入或连接 GM SDK。"""

import json
from pathlib import Path

import pytest

from axile.channels import get_channel
from axile.server.execution_replay.service import replay_window
from tests.unit.server.test_execution_replay_native import finished
from tests.unit.server.test_execution_replay_native import make_recording as make_recording

FIXTURES = json.loads((Path(__file__).parents[2] / "fixtures/execution_replay/gm-review-regressions.json").read_text())
ORDER_KEY = '["600000.SH","one"]'


def checkpoint(make_recording, samples):
    context = get_channel("gm").descriptor.replay.model_dump(mode="json")
    context["channel"] = "gm"
    recorder, now, root, identity = make_recording(context)
    recorder.begin_execution("E", ["600000.SH"])
    for sample in samples:
        now[0] += 1
        recorder.observe(sample["topic"], sample["data"])
    finished(recorder)
    return replay_window(root, 7, "E", {"recording_id": identity, "start_us": now[0] + 1, "end_us": now[0] + 2})[
        "state_before_start"
    ]


def test_native_checkpoint_keeps_one_order_and_separate_known_position_sides(make_recording):
    state = checkpoint(make_recording, [FIXTURES["order"], FIXTURES["order"], FIXTURES["positions"]])
    assert list(state["orders"]) == [ORDER_KEY]
    assert state["orders"][ORDER_KEY]["confirmed_book"] is True
    assert state["orders"][ORDER_KEY]["field_quality"]["order_id"] == "known"
    for side, quantity in (("LONG", 100), ("SHORT", 20)):
        row = state["observed_positions"][f'["600000.SH","{side}"]']
        assert row["quantity"] == quantity
        assert row["field_quality"]["position_side"] == "known"


def test_nonfill_report_does_not_poison_native_order_remaining_quantity(make_recording):
    state = checkpoint(make_recording, [FIXTURES["order"], FIXTURES["cancel"]])
    order = state["orders"][ORDER_KEY]
    assert order["remaining_quantity"] == 100 and order["remaining_quantity_complete"] is True
    assert state["trade_accounting"]["own_records"] == {}
    assert state["trade_accounting"]["pending"] == {}


@pytest.mark.parametrize("correction", FIXTURES["corrections"], ids=["correct", "trade_cancel"])
def test_native_trade_revision_cannot_silently_reuse_prior_known_fill(make_recording, correction):
    state = checkpoint(make_recording, [FIXTURES["order"], FIXTURES["fill"], correction])
    assert state["orders"][ORDER_KEY]["remaining_quantity"] is None
    assert state["orders"][ORDER_KEY]["remaining_quantity_complete"] is False
    ledger = state["trade_accounting"]
    assert list(ledger["own_records"]) == ["gm:7:600000.SH:fill-1"]
    assert ledger["own_records"]["gm:7:600000.SH:fill-1"]["data"]["quantity"] == 40
    assert ledger["pending"]
