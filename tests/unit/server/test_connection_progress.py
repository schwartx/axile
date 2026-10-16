"""连接中的 IPC 快照和实例隔离；不依赖真实交易渠道。"""

from threading import Event, Thread
from types import SimpleNamespace

from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.connection_progress import ConnectionProgressTracker
from axile.executor.connection_state import ConnectionObservation, connection_monitor, connection_observer_scope
from axile.server import connection_runtime as runtime
from axile.server.connection_state import ConnectionBinding, ConnectionCollector
from axile.server.execution.resident_connection import ResidentConnectionState, ensure_resident_connection
from tests.unit.server._execution_test_support import build_account


def test_blocking_connection_reports_steps_before_request_returns_and_reuses_completed_run():
    state = ResidentConnectionState()
    collector = ConnectionCollector(ConnectionBinding(7, "config", "instance"))
    state.connection_observer = collector.observe_session
    started, proceed, done = Event(), Event(), Event()
    errors = []
    created = []
    account = build_account(id=7, trade_channel="gm")

    class Executor:
        ready = False

        def __init__(self):
            self.monitor = connection_monitor(self)
            created.append(self)

        def _verify_connection(self):
            return self.ready

        def get_account_assets(self):
            return SimpleNamespace(currency="CNY", total_asset=100, available_cash=100, source="live")

        def get_pending_orders(self, _symbol):
            return []

    def initialize(executor):
        executor.monitor.step("environment", "passed")
        executor.monitor.step("runtime", "running")
        started.set()
        assert proceed.wait(3)
        executor.monitor.step("runtime", "passed")
        executor.monitor.step("account_login", "running")
        executor.monitor.step("account_login", "passed")
        executor.ready = True

    def connect():
        with connection_observer_scope(collector.observe):
            return ensure_resident_connection(
                state, account, create=Executor, close=lambda _executor: None, initialize=initialize
            )

    def background():
        try:
            connect()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = Thread(target=background)
    thread.start()
    try:
        assert started.wait(3)
        frame = collector.snapshot()
        assert not done.is_set()
        assert frame["session"]["status"] == "connecting"
        assert frame["progress"]["steps"][1]["status"] == "running"
        first_run = frame["progress"]["run_id"]
    finally:
        proceed.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert collector.snapshot()["progress"]["status"] == "succeeded"
    assert collector.snapshot()["session"]["status"] == "ok"
    assert connect() is created[0]
    assert len(created) == 1
    assert collector.snapshot()["progress"]["run_id"] == first_run


def test_runtime_rejects_old_instances_and_sequences_and_keeps_tables_out_of_public_frames():
    tracker = ConnectionProgressTracker("ctp")
    tracker.step(
        "query_orders",
        "passed",
        {"kind": "table", "title": "订单", "summary": "1 条", "columns": [], "rows": [["0001"]]},
    )
    binding = ConnectionBinding(9137, "config", "first")
    runtime.register_session(binding, lambda: True)
    try:
        runtime.observe_session(
            binding, 2, ConnectionObservation("connecting", clock_now_iso()), progress=tracker.snapshot(tables=True)
        )
        public = runtime.current_session(binding.account_id, binding.config_key)
        assert public.progress.run_id == tracker.run.run_id
        assert "tables" not in public.model_dump()["progress"]
        table = runtime.connection_table(binding.account_id, "config", tracker.run.run_id, "query_orders")
        assert table["rows"] == [["0001"]]
        stale = ConnectionProgressTracker("gm")
        runtime.observe_session(binding, 1, ConnectionObservation("ok", clock_now_iso()), progress=stale.snapshot())
        assert runtime.current_session(binding.account_id, "config").progress.run_id == tracker.run.run_id
        replacement = ConnectionBinding(binding.account_id, "config", "second")
        runtime.register_session(replacement, lambda: True)
        runtime.observe_session(binding, 3, ConnectionObservation("ok", clock_now_iso()), progress=tracker.snapshot())
        assert runtime.current_session(binding.account_id, "config").progress is None
        assert runtime.connection_table(binding.account_id, "config", tracker.run.run_id, "query_orders") is None
    finally:
        runtime.forget_session(binding.account_id)


def test_worker_exit_ends_running_progress_without_discarding_prior_steps():
    tracker = ConnectionProgressTracker("gm")
    tracker.step("environment", "passed")
    tracker.step("runtime", "running")
    binding = ConnectionBinding(9138, "config", "worker")
    runtime.register_session(binding, lambda: True)
    try:
        runtime.observe_session(
            binding, 1, ConnectionObservation("connecting", clock_now_iso()), progress=tracker.snapshot()
        )
        runtime.end_session(binding, "执行器退出", failed=True)
        detail = runtime.current_session(binding.account_id, "config")
        assert detail.status == "unavailable"
        assert detail.progress.status == "failed"
        assert detail.progress.steps[0].status == "passed"
        assert detail.progress.steps[1].status == "failed"
        assert detail.progress.steps[1].error.message == "执行器退出"
    finally:
        runtime.forget_session(binding.account_id)
