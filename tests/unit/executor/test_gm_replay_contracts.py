"""真实 GM 适配输出与前端共用独立真值，防止字段质量及事件路由漂移。"""

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from axile.channels import get_channel
from axile.executor.gm import replay
from axile.server.execution_replay.contracts import freeze_data

FIXTURES = json.loads((Path(__file__).parents[2] / "fixtures/execution_replay/gm-review-regressions.json").read_text())
CASES = [FIXTURES[name] for name in ("order", "positions", "cancel", "fill")] + FIXTURES["corrections"]


def assert_subset(actual, expected):
    if isinstance(expected, dict):
        for key, value in expected.items():
            assert key in actual
            assert_subset(actual[key], value)
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for value, wanted in zip(actual, expected, strict=True):
            assert_subset(value, wanted)
    else:
        assert actual == expected


@pytest.mark.parametrize("case", CASES, ids=["order", "positions", "cancel", "fill", "correct", "trade_cancel"])
def test_actual_gm_capture_matches_frontend_projection_truth(case):
    received = []
    owner = SimpleNamespace(
        _replay_account_id=7,
        _replay_sync_generation=1,
        _replay_observer=lambda topic, data: received.append((topic, data)),
    )
    if case["kind"] == "order":
        build = partial(replay.order, case["input"])
    elif case["kind"] == "account":
        build = partial(replay.account_observation, **case["input"])
    else:
        build = partial(replay.trade, owner, case["input"])
    replay.capture(owner, "/trade" if case["kind"] == "trade" else case["topic"], build)
    assert len(received) == 1
    topic, data = received[0]
    assert topic == case["topic"]
    assert_subset(data, case["data"])
    context = get_channel("gm").descriptor.replay.model_dump(mode="json")
    assert freeze_data(topic, data, context) == data
