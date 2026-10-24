"""正常通知与 Rust 投影遵守冻结的 Python 上下文契约。"""

import json
import math
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from axile.executor import feishu_notifications
from axile.executor.models.unified_output import UnifiedStandardOutput
from tests.unit.executor.test_feishu_notifications import _NotificationSource

_FIXTURE = json.loads((Path(__file__).parents[2] / "fixtures/notification_context.json").read_text())


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=lambda case: case["name"])
def test_notification_projection_preserves_frozen_context(case, monkeypatch):
    monkeypatch.setattr(feishu_notifications, "clock_now", lambda: datetime.fromisoformat(_FIXTURE["now"]))
    output = UnifiedStandardOutput.model_validate(case["result"])
    before = output.model_dump(mode="json")
    context = feishu_notifications.build_execution_notification_context(
        cast("feishu_notifications.FeishuNotificationSource", _NotificationSource()),
        output,
        case["notification_snapshot"],
        is_test=case["is_test"],
    )
    assert context == case["expected"]
    assert output.model_dump(mode="json") == before


def test_notification_projection_does_not_serialize_private_runtime_objects(monkeypatch):
    monkeypatch.setattr(feishu_notifications, "clock_now", lambda: datetime.fromisoformat(_FIXTURE["now"]))
    case = _FIXTURE["cases"][0]
    output = UnifiedStandardOutput.model_validate(case["result"])
    opaque = object()
    output.extra["runtime"] = opaque
    output.memory["runtime"] = opaque
    assert output.inputs is not None
    output.inputs.extra["runtime"] = opaque
    output.account_assets.extra["runtime"] = opaque
    for position in output.account_assets.positions:
        position.extra["runtime"] = opaque
    for result in output.symbol_results.values():
        result.memory["runtime"] = opaque
        if result.first_tick is not None:
            result.first_tick.extra["runtime"] = opaque
        for row in [*result.orders, *result.trades]:
            row.extra["runtime"] = opaque

    context = feishu_notifications.build_execution_notification_context(
        cast("feishu_notifications.FeishuNotificationSource", _NotificationSource()), output, is_test=case["is_test"]
    )
    assert context == case["expected"]
    assert output.extra["runtime"] is opaque


@pytest.mark.parametrize("value,label", [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")])
def test_notification_projection_keeps_nonfinite_values(value, label):
    output = UnifiedStandardOutput.model_validate(_FIXTURE["cases"][0]["result"])
    output.account_assets.total_asset = value
    output.account_assets.available_cash = value
    output.account_assets.market_value = value
    for result in output.symbol_results.values():
        for trade in result.trades:
            trade.trade_value = value

    context = feishu_notifications.build_execution_notification_context(
        cast("feishu_notifications.FeishuNotificationSource", _NotificationSource()), output
    )
    assets, summary, defaults = context["assets"], context["summary"], context["default_feishu_variables"]
    assert isinstance(assets, dict) and isinstance(summary, dict) and isinstance(defaults, dict)
    for observed in (assets["total_assets"], summary["trade_value"]):
        assert math.isnan(observed) if math.isnan(value) else observed == value
    assert defaults["total_assets"] == label
