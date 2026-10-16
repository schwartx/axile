"""真实连接和诊断的共用进度契约，不连接外部交易服务。"""

from types import SimpleNamespace

import pytest

from axile.executor.connection_progress import (
    ConnectionProgressTracker,
    connection_plan,
    connection_progress_scope,
    connection_step_error,
)
from axile.executor.connection_state import ConnectionMonitor, connection_observer_scope
from axile.server.connection_state import ConnectionBinding, ConnectionCollector


@pytest.mark.parametrize("channel", ["gm", "ctp", "tq"])
def test_validation_and_normal_connection_share_steps_except_diagnostic_sample(channel):
    normal = [step.id for step in connection_plan(channel)]
    validation = [step.id for step in connection_plan(channel, "validation")]
    assert [step for step in validation if step != "instruments"] == normal
    assert normal[-3:] == ["account_assets", "pending_orders", "ready"]
    assert len(normal) == len(set(normal))


def test_failed_step_retains_prior_results_and_rejects_late_callbacks():
    tracker = ConnectionProgressTracker("ctp")
    tracker.step("trader_connection", "running")
    tracker.step("trader_connection", "passed")
    tracker.step("authenticate", "running")
    tracker.finish("failed", connection_step_error(ValueError("柜台拒绝认证")))
    before = tracker.snapshot()
    tracker.step("authenticate", "passed")
    assert tracker.snapshot() == before
    assert before["steps"][0]["status"] == "passed"
    assert before["steps"][1]["status"] == "failed"
    assert before["steps"][1]["error"]["details"] == "柜台拒绝认证"
    assert all(step["status"] == "skipped" for step in before["steps"][2:])


def test_tables_are_separate_from_public_snapshots_and_details_are_copied():
    tracker = ConnectionProgressTracker("ctp")
    details = {
        "kind": "table",
        "title": "订单",
        "summary": "1 条",
        "columns": [{"key": "id", "label": "编号"}],
        "rows": [["0001"]],
    }
    tracker.step("query_orders", "running")
    tracker.step("query_orders", "passed", details)
    details["rows"][0][0] = "changed"
    public = tracker.snapshot()
    assert "tables" not in public
    step = next(step for step in public["steps"] if step["id"] == "query_orders")
    assert "rows" not in step["details"]
    assert tracker.snapshot(tables=True)["tables"]["query_orders"]["rows"] == [["0001"]]


def test_monitor_captures_constructor_scope_and_recovery_stays_incomplete_until_reconciled():
    frames = []
    tracker = ConnectionProgressTracker("gm")
    for step in tracker.run.steps:
        tracker.step(step.id, "passed")
    tracker.finish()
    with connection_observer_scope(frames.append), connection_progress_scope(tracker):
        monitor = ConnectionMonitor()
    monitor.failed(ConnectionError("断线"))
    # An access failure alone is not evidence of SDK recovery having started.
    assert monitor.progress.run.status == "succeeded"
    monitor.begin_recovery()
    monitor.succeeded()
    recovery = monitor.progress
    assert recovery is not tracker
    assert recovery.run.purpose == "recovery"
    assert recovery.run.status == "running"
    assert recovery.run.steps[0].status == "passed"
    assert recovery.run.steps[1].status == "pending"
    assert frames[-2].kind == "progress"
    monitor.close()
    before = len(frames)
    monitor.begin_recovery()
    monitor.step("account_assets", "passed")
    monitor.failed(RuntimeError("迟到故障"))
    assert len(frames) == before


def test_step_progress_does_not_replace_the_collectors_session_readiness():
    from axile.executor.algorithms.utils.clock import clock_now_iso
    from axile.executor.connection_state import ConnectionObservation

    collector = ConnectionCollector(ConnectionBinding(7, "config", "instance"))
    collector.observe_session(ConnectionObservation("connecting", clock_now_iso()))
    tracker = ConnectionProgressTracker(
        "gm",
        observer=lambda data: collector.observe(
            ConnectionObservation("connecting", clock_now_iso(), kind="progress", progress=data)
        ),
    )
    tracker.step("environment", "passed")
    snapshot = collector.snapshot()
    assert snapshot["session"]["status"] == "connecting"
    assert snapshot["progress"]["steps"][0]["status"] == "passed"
    assert snapshot["observation"]["kind"] == "session"


def test_complete_status_requires_final_account_readiness():
    tracker = ConnectionProgressTracker("gm")
    for step in tracker.run.steps[:-1]:
        tracker.step(step.id, "passed")
    tracker.finish()
    assert tracker.run.status == "failed"
    assert tracker.run.error.reason_code == "CONNECTION.INCOMPLETE"


@pytest.mark.parametrize("channel", ["gm", "tq"])
def test_generic_validation_observes_constructor_and_closes_after_account_checks(monkeypatch, channel):
    from axile.executor import connection_validation as validation
    from axile.executor.connection_state import connection_monitor

    calls, events = [], []
    executor = SimpleNamespace(
        _verify_connection=lambda: True,
        get_account_assets=lambda: SimpleNamespace(currency="CNY", available_cash=1, total_asset=1, source="live"),
        get_pending_orders=lambda _symbol: calls.append("orders") or [],
        close=lambda: calls.append("close"),
    )

    def create(config):
        calls.append("create")
        for step in connection_plan(channel)[:-3]:
            connection_monitor(executor).step(step.id, "running")
            connection_monitor(executor).step(step.id, "passed")
        return executor

    monkeypatch.setattr(validation, "get_channel", lambda _channel: SimpleNamespace(create_executor=create))
    validation.validate_channel_connection(
        channel, object(), lambda step, status, details=None: events.append((step, status))
    )
    assert calls == ["create", "orders", "close"]
    assert events[0] == (connection_plan(channel)[0].id, "running")
    assert events[-1] == ("ready", "passed")


def test_propagated_failure_adds_traceback_without_replacing_native_reason():
    tracker = ConnectionProgressTracker("ctp")
    tracker.step("authenticate", "running")
    native = ValueError("柜台原始原因")
    tracker.finish("failed", connection_step_error(native))
    try:
        raise RuntimeError("宿主包装错误") from native
    except RuntimeError as wrapped:
        tracker.finish("failed", connection_step_error(wrapped))
    assert tracker.run.error.details == "柜台原始原因"
    assert "宿主包装错误" in tracker.run.error.traceback
    assert tracker.run.steps[1].error == tracker.run.error
