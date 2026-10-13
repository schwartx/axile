"""监控通路不排在业务队列后，断连不等价于交易进程崩溃。"""

from multiprocessing import Pipe
from threading import Thread

from axile.server.account_control import ipc, providers
from tests.unit.executor.test_account_control_monitor import make_guard


def test_independent_pipe_reads_guard_without_business_command(monkeypatch):
    monkeypatch.setattr(providers, "_guards", {})
    monkeypatch.setattr(providers, "_final", {})
    guard = make_guard()
    providers.register(guard)
    parent, child = Pipe()
    thread = Thread(target=ipc.serve, args=(child, 1), daemon=True)
    thread.start()
    try:
        parent.send(True)
        assert parent.poll(2)
        frame = parent.recv()
        assert frame["instance_id"] == guard.instance_id
        assert next(m["value"] for m in frame["metrics"] if m["operation"] == "place_order") == 120
        assert guard.flush_records() == ([], [])
    finally:
        parent.send(None)
        thread.join(timeout=2)
        parent.close()
    assert not thread.is_alive()


def test_transport_failure_does_not_report_living_worker_as_crashed(monkeypatch):
    monkeypatch.setattr(ipc, "_remotes", {})
    parent, child = Pipe()
    child.close()
    monitor = ipc.RemoteMonitor(parent, 1, process_alive=lambda: True)
    monitor.thread.join(timeout=2)
    assert monitor.failed
    assert ipc.sample(1)[2] is False
    monitor.process_alive = lambda: False
    assert ipc.sample(1)[2] is True
    monitor.close()
