"""真实子进程的进度经既有监控 IPC 回到主进程。"""

import asyncio
from multiprocessing import get_context

from axile.server.account_control.ipc import RemoteMonitor
from axile.server.connection_state import ConnectionBinding
from axile.server.execution import live
from axile.server.execution.live import ExecutionLiveHub
from axile.server.execution.worker_backend.progress import WorkerExecutionProgress
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
from tests.unit.server._execution_test_support import FakeExecutor, build_account


def _run_worker(connection, control, monitor, released):
    from axile.executor.algorithms.utils.clock import get_default_clock
    from axile.server.execution.worker_backend import worker

    class Executor(FakeExecutor):
        def execute(self, standard_input, **kwargs):
            state.progress.report("live", "planning")
            state.progress.report("live", "executing")
            assert get_default_clock().event_wait(released, 8)
            return super().execute(standard_input, **kwargs)

    def resolve(**kwargs):
        nonlocal state
        state = kwargs["state"]
        return Executor()

    state = None
    worker._resolve_prepared_executor = resolve
    worker._append_trade_pre_execute_audit = lambda **kwargs: None
    worker._append_success_audit = lambda **kwargs: None
    worker._finalize_with_orders = lambda *args: None
    worker.run_worker_backend_loop(connection, 1, control, monitor_connection=monitor)


def _slots(hub, execution_id="live", status="running"):
    hub.publish_slots(
        account_id=1,
        execution_id=execution_id,
        kind="rebalance",
        status=status,
        phase="triggered",
        pending_execution_id=None,
        pending_kind=None,
    )


def test_spawn_worker_progress_arrives_before_business_response(monkeypatch):
    hub = ExecutionLiveHub()
    monkeypatch.setattr(live, "live_hub", hub)
    monkeypatch.setattr(
        "axile.server.connection_runtime.matches_session", lambda binding: binding.instance_id == "worker"
    )
    monkeypatch.setattr("axile.server.connection_state.consume_connection_snapshot", lambda frame: None)
    context = get_context("spawn")
    parent, child = context.Pipe()
    parent_control, child_control = context.Pipe()
    parent_monitor, child_monitor = context.Pipe()
    released = context.Event()
    process = context.Process(target=_run_worker, args=(child, child_control, child_monitor, released))
    process.start()
    child.close()
    child_control.close()
    child_monitor.close()
    remote = RemoteMonitor(parent_monitor, 1, process.is_alive)
    _slots(hub)
    parent.send(
        WorkerBackendRequest(
            request_id="request",
            command="execute_trade",
            execution_id="live",
            account_payload=build_account().model_dump(mode="json"),
            payload={
                "standard_input": {"curr_target": {}, "account_config": build_account().account_config},
                "audit_input": {},
                "trigger_source": "manual",
            },
            connection_binding={
                "account_id": 1,
                "config_key": "test",
                "instance_id": "worker",
                "manually_disconnected": False,
            },
        )
    )

    async def observe():
        hub.bind_loop(asyncio.get_running_loop())
        async with hub.subscribe() as queue:
            while (frame := await asyncio.wait_for(queue.get(), timeout=5))["phase"] != "executing":
                pass
            assert frame["status"] == "running"
            assert not parent.poll()

    try:
        asyncio.run(observe())
        released.set()
        assert parent.poll(5)
        response = parent.recv()
        assert response.kind == "result", response.error
        assert hub.progress_for(1)["status"] == "running"
    finally:
        released.set()
        remote.close()
        parent.send(None)
        process.join(3)
        if process.is_alive():
            process.terminate()
            process.join(2)
        parent.close()
        parent_control.close()


def test_progress_identity_monotonicity_and_freshness(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(live, "clock_monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "axile.server.connection_runtime.matches_session", lambda binding: binding.instance_id == "current"
    )
    hub = ExecutionLiveHub()
    binding = ConnectionBinding(1, "config", "current", False)
    producer = WorkerExecutionProgress()
    producer.begin("live", binding)
    producer.report("live", "executing")
    frame = producer.snapshot()["snapshot"]
    _slots(hub, status="terminating")
    hub.consume_progress(binding, frame)
    assert hub.progress_for(1)["phase"] == "executing"
    assert hub.progress_for(1)["status"] == "terminating"
    producer.begin("next", binding)
    producer.report("live", "settling")
    assert producer.snapshot()["snapshot"]["phase"] == "triggered"
    clock[0] = 6
    assert hub.progress_for(1)["progress_stale"]
    hub.consume_progress(binding, frame)
    assert not hub.progress_for(1)["progress_stale"]
    hub.consume_progress(binding, {**frame, "version": 0, "phase": "settling"})
    assert hub.progress_for(1)["phase"] == "executing"
    _slots(hub, execution_id="next")
    hub.consume_progress(binding, frame)
    hub.consume_progress(ConnectionBinding(1, "config", "old", False), {**frame, "execution_id": "next"})
    assert hub.progress_for(1)["phase"] == "triggered"
    assert hub.execution_progress(1, "live") is None


def test_progress_sampling_failure_keeps_flow_monitor_available(monkeypatch):
    from unittest.mock import Mock

    from axile.server.account_control.ipc import serve

    connection = Mock()
    connection.recv.side_effect = [True, None]
    monkeypatch.setattr("axile.server.account_control.providers.snapshot", lambda account_id: {"healthy": True})

    def broken():
        raise RuntimeError("progress unavailable")

    serve(connection, 1, lambda: None, execution_snapshot=broken)
    connection.send.assert_called_once_with({"flow": {"healthy": True}, "connection": None})
    connection.close.assert_called_once()
