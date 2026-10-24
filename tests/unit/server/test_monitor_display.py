"""监控展示保留默认值与字段边界，缓存和 HTTP 不再重复校验。"""

from dataclasses import is_dataclass

from axile.server.account_control.schema import (
    AccountControlMonitorPublic,
    ControlRecordPublic,
    ControlRulePublic,
    ControlWaitingPublic,
    public_frame,
)
from axile.server.order_submissions import OrderSubmissionSummary
from axile.server.submission_monitor import (
    SubmissionMetric,
    SubmissionMonitorPublic,
    SubmissionMonitorSettings,
    SubmissionWarning,
)


def test_monitor_display_keeps_defaults_and_filters_nested_internal_fields():
    rule = dict(
        id="account:place_order:per_day",
        operation="place_order",
        scope="account",
        symbol=None,
        rule="per_day",
        value=0,
        limit=None,
        remaining=None,
        estimated_wait_ms=0,
        on_trigger=None,
        internal_counter="private",
    )
    values = dict(
        account_id=1,
        execution_id=None,
        instance_id=None,
        seq=0,
        status="idle",
        sampled_at_ms=0,
        timezone="Asia/Shanghai",
        control_date="2026-10-24",
        minute_start=None,
        stale=False,
        incomplete=False,
        pending_policy=False,
        actual_policy=None,
        actual_fingerprint=None,
        saved_policy={},
        saved_fingerprint="saved",
        rules=[rule],
        metrics=[],
        waiting_total=1,
        waiting=[
            dict(
                id=1,
                operation="place_order",
                symbol=None,
                priority=0,
                entered_at_ms=0,
                waited_ms=0,
                kind="queue",
                internal_queue="private",
            )
        ],
        records=[
            dict(
                event_uid="e",
                occurred_at_ms=0,
                operation="place_order",
                symbol=None,
                decision="allowed",
                outcome="submitted",
                metadata={"zero": 0},
                account_id=1,
            )
        ],
        order_submissions=dict(recorded=1, internal_submission="private"),
        submission_monitor=dict(
            settings=dict(order_limit=1, internal_config="private"),
            control_date="2026-10-24",
            sampled_at_ms=0,
            metrics=[dict(metric="order_calls", value=1, unknown=0, limit=1, state="reached", internal_metric=1)],
            warnings=[
                dict(
                    event_uid="w",
                    account_id=1,
                    metric="order_calls",
                    value=1,
                    limit=1,
                    control_date="2026-10-24",
                    timezone="Asia/Shanghai",
                    occurred_at_ms=0,
                    revision=1,
                    source="configuration",
                    internal_warning="private",
                )
            ],
        ),
        quota_warnings=[{"internal": "private"}],
    )
    result = public_frame(values)
    assert "quota_warnings" not in result
    assert result["history_incomplete"] is False and result["counter_integrity"] == "known"
    assert result["rules"] == [
        {key: value for key, value in rule.items() if key != "internal_counter"} | {"label": None}
    ]
    assert result["waiting"][0]["estimated_wait_ms"] is None and result["waiting"][0]["hits"] == []
    assert "internal_queue" not in result["waiting"][0]
    assert "account_id" not in result["records"][0]
    assert result["records"][0]["metadata"] == {"zero": 0}
    assert result["order_submissions"]["recorded"] == 1 and result["order_submissions"]["unknown"] == 0
    assert "internal_submission" not in result["order_submissions"]
    monitor = result["submission_monitor"]
    assert monitor["settings"] == dict(
        order_limit=1, combined_limit=None, timezone="Asia/Shanghai", revision=0, updated_at_ms=None
    )
    assert "internal_metric" not in monitor["metrics"][0]
    assert "internal_warning" not in monitor["warnings"][0]
    assert monitor["warnings"][0]["submission_uid"] is None
    assert monitor["warnings"][0]["execution_id"] is None
    assert "internal_counter" in values["rules"][0]
    assert "account_id" in values["records"][0]
    for model in (
        AccountControlMonitorPublic,
        ControlRulePublic,
        ControlWaitingPublic,
        ControlRecordPublic,
        OrderSubmissionSummary,
        SubmissionMonitorPublic,
        SubmissionMonitorSettings,
        SubmissionMetric,
        SubmissionWarning,
    ):
        assert is_dataclass(model) and "__slots__" in vars(model)
