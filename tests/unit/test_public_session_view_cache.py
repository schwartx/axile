"""批量会话读取沿用单账户读取的展示时间缓存。"""

import pytest

from axile.executor.connection_state import ConnectionObservation
from axile.server import connection_runtime


@pytest.mark.parametrize(
    ("sampled", "alive", "status"),
    [(0.0, True, "ready"), (-6.0, True, "unknown"), (0.0, False, "disconnected")],
)
def test_batch_reads_keep_transition_timestamp_and_detach_results(monkeypatch, sampled, alive, status):
    timestamp = ["transition-at"]
    monkeypatch.setattr(connection_runtime, "clock_monotonic", lambda: 0.0)
    monkeypatch.setattr(connection_runtime, "clock_now_iso", lambda: timestamp[0])
    session = connection_runtime._LiveSession(
        "config",
        "worker",
        lambda: alive,
        observation=ConnectionObservation("ok", "access-at", "ready"),
        sampled=sampled,
        sampled_at="sampled-at",
    )
    monkeypatch.setattr(connection_runtime, "_sessions", {1: session})

    first = connection_runtime.public_session_views()["1"]["connection"]
    timestamp[0] = "later-read-at"
    second = connection_runtime.public_session_views()["1"]["connection"]

    assert first["status"] == status
    assert first["observed_at"] == second["observed_at"] == "transition-at"
    assert connection_runtime.current_session(1, "config").model_dump(mode="json") == first
    assert session.view_status == status and session.updated_at == "transition-at"
    assert session.observation.observed_at == "access-at" and session.sampled_at == "sampled-at"
    first["message"] = "caller changed"
    assert connection_runtime.public_session_views()["1"]["connection"] == second
