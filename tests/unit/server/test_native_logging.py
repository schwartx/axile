"""原生日志队列溢出、轮转和真实文件错误。"""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from axile.common.log_files import iter_file, log_statistics, read_logs
from axile.server._native import _core
from axile.server.core.logging_sink import compress_log


def payload(**changes):
    return json.dumps(
        {
            "level": "INFO",
            "template": "订单 {id}",
            "fields": ["id"],
            "args": [42],
            "context": {"account_id": 7, "execution_id": "E1"},
            "metadata": {"kind": "event"},
            **changes,
        },
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def close_writer():
    _core.logging_close(1000)
    yield
    _core.logging_close(1000)


def test_small_queue_counts_all_rejections_and_survivors(tmp_path):
    _core.logging_start(str(tmp_path), capacity=1)
    item = payload()
    results = [_core.logging_emit(item) for _ in range(10000)]
    _core.logging_close(1000)
    stats = json.loads(_core.logging_stats())
    assert stats["dropped_total"] > 0
    assert stats["accepted_total"] == sum(results)
    assert stats["dropped_by_reason"]["queue_full"] == len(results) - sum(results)
    assert stats["written_total"] == stats["accepted_total"]
    rows = read_logs(tmp_path.glob("*.jsonl"))
    assert sum(not row["metadata"].get("log_stats") for row in rows) == sum(results)
    assert log_statistics(rows)["dropped_total"] == stats["dropped_total"]
    # 重复传入同一份材料，累计计数和事件均不能翻倍。
    assert len(read_logs(list(tmp_path.glob("*.jsonl")) * 2)) == len(rows)


def test_rotation_independent_decode_and_compression_keep_span_identity(tmp_path):
    _core.logging_start(str(tmp_path), capacity=128, max_bytes=300)
    parent = json.loads(_core.logging_span("根过程", '{"execution_id":"E1"}'))
    child = json.loads(_core.logging_span("子过程", '{"execution_id":"E1"}', parent["id"]))
    for _ in range(8):
        assert _core.logging_emit(payload(spans=[parent, child]))
    _core.logging_close(1000)
    before = read_logs(tmp_path.glob("*.jsonl"))
    assert len({row["span_id"] for row in before if row["span_id"] is not None}) == 1
    while path := _core.logging_archive():
        compress_log(Path(path))
    paths = list(tmp_path.glob("*.jsonl")) + list(tmp_path.glob("*.jsonl.gz"))
    assert len(paths) > 1
    for path in paths:
        for row in iter_file(path):
            if row["span_id"] is not None:
                assert [span["id"] for span in row["spans"]] == [parent["id"], child["id"]]
    assert read_logs(paths) == before


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_FSIZE is a Unix fault injection")
def test_real_file_failure_isolated_and_unknown_records_not_called_dropped(tmp_path):
    program = r"""
import json,resource,signal,sys,threading
from axile.server._native import _core
signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE,(1024,1024))
_core.logging_start(sys.argv[1],capacity=1)
result = _core.logging_emit(json.dumps({"level":"INFO","template":"{message}","fields":["message"],"args":["x"*8192]}))
assert result is True
for _ in range(100):
    if json.loads(_core.logging_stats())["stopped"]: break
    threading.Event().wait(0.01)
_core.logging_close(1000)
stats=json.loads(_core.logging_stats())
assert stats["write_errors_total"]==1,stats
assert stats["unconfirmed_total"]==1,stats
assert stats["dropped_total"]==0,stats
assert _core.logging_emit(json.dumps({"level":"INFO","template":"later","fields":[],"args":[]})) is False
assert json.loads(_core.logging_stats())["dropped_by_reason"]["unavailable"]==1
print(json.dumps(stats))
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_invalid_input_count_and_new_run_does_not_overwrite(tmp_path):
    _core.logging_start(str(tmp_path))
    assert not _core.logging_emit(payload(args=[]))
    assert not _core.logging_emit("{bad}")
    assert not _core.logging_emit(payload(template="{0}", fields=["0"], args=[1]))
    assert json.loads(_core.logging_stats())["dropped_by_reason"]["invalid"] == 3
    first = json.loads(_core.logging_stats())["run_id"]
    _core.logging_close(1000)
    original = {path: path.read_bytes() for path in tmp_path.glob("*.jsonl")}
    _core.logging_start(str(tmp_path))
    assert json.loads(_core.logging_stats())["run_id"] != first
    _core.logging_close(1000)
    assert all(path.read_bytes() == content for path, content in original.items())


def test_time_rotation_keeps_sequence_and_run_id(tmp_path):
    _core.logging_start(str(tmp_path), rotation_seconds=1)
    assert _core.logging_emit(payload())
    for _ in range(100):
        if json.loads(_core.logging_stats())["written_total"] == 1:
            break
        threading.Event().wait(0.01)
    assert json.loads(_core.logging_stats())["written_total"] == 1
    threading.Event().wait(1.1)
    assert _core.logging_emit(payload())
    _core.logging_close(1000)
    parts = list(tmp_path.glob("*.jsonl"))
    assert len(parts) >= 2
    records = [row for row in read_logs(parts) if not row["metadata"].get("log_stats")]
    assert len(records) == 2 and records[0]["seq"] < records[1]["seq"]
    assert records[0]["run_id"] == records[1]["run_id"]


def test_preinitialization_drops_survive_recovery_once(tmp_path):
    program = """
import json,sys
from axile.server._native import _core
from axile.common.log_files import read_logs,log_statistics
from pathlib import Path
root=Path(sys.argv[1])
_core.logging_invalid()
assert json.loads(_core.logging_stats())["dropped_total"]==1
_core.logging_start(str(root/"first"))
assert json.loads(_core.logging_stats())["dropped_total"]==1
_core.logging_close(1000)
_core.logging_start(str(root/"second"))
assert json.loads(_core.logging_stats())["dropped_total"]==0
_core.logging_close(1000)
assert log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]==1
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_failed_restart_keeps_saved_drops_once_and_recovers_rejected_gap(tmp_path):
    program = """
import json,sys
from pathlib import Path
from axile.server._native import _core
from axile.common.log_files import read_logs,log_statistics
root=Path(sys.argv[1])
_core.logging_start(str(root/"first"))
_core.logging_invalid()
_core.logging_invalid()
_core.logging_close(1000)
assert log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]==2
blocked=root/"blocked"
blocked.write_text("not a directory")
try:
    _core.logging_start(str(blocked/"logs"))
except ValueError:
    pass
else:
    raise AssertionError("restart must fail")
payload=json.dumps({"level":"INFO","template":"gap","fields":[],"args":[]})
for _ in range(3):
    assert _core.logging_emit(payload) is False
_core.logging_invalid()
assert json.loads(_core.logging_stats())["dropped_total"]==6
_core.logging_start(str(root/"recovered"))
stats=json.loads(_core.logging_stats())
assert stats["dropped_total"]==4,stats
assert stats["dropped_by_reason"]=={"queue_full":0,"unavailable":3,"invalid":1},stats
_core.logging_close(1000)
assert log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]==6
_core.logging_start(str(root/"third"))
assert json.loads(_core.logging_stats())["dropped_total"]==0
_core.logging_close(1000)
assert log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]==6
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="fault injection renames a directory containing open files")
def test_failed_writer_recovers_only_unsaved_drops(tmp_path):
    program = """
import json,sys,threading
from pathlib import Path
from axile.server._native import _core
from axile.common.log_files import read_logs,log_statistics
root=Path(sys.argv[1])
active=root/"active"
_core.logging_start(str(active),capacity=32,max_bytes=1)
_core.logging_invalid()
_core.logging_invalid()
payload=json.dumps({"level":"INFO","template":"event","fields":[],"args":[]})
assert _core.logging_emit(payload) is True
for _ in range(200):
    if json.loads(_core.logging_stats())["written_total"]==1: break
    threading.Event().wait(0.01)
assert json.loads(_core.logging_stats())["written_total"]==1
active.rename(root/"closed")
active.write_text("rotation cannot create a file here")
for _ in range(3): _core.logging_invalid()
assert _core.logging_emit(payload) is True
for _ in range(200):
    if json.loads(_core.logging_stats())["stopped"]: break
    threading.Event().wait(0.01)
assert json.loads(_core.logging_stats())["write_errors_total"]==1
for _ in range(2): assert _core.logging_emit(payload) is False
_core.logging_invalid()
_core.logging_close(1000)
assert json.loads(_core.logging_stats())["dropped_total"]==8
saved=log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]
_core.logging_start(str(root/"recovered"))
stats=json.loads(_core.logging_stats())
assert stats["dropped_total"]==8-saved,(stats,saved)
_core.logging_close(1000)
assert log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]==8
_core.logging_start(str(root/"third"))
assert json.loads(_core.logging_stats())["dropped_total"]==0
_core.logging_close(1000)
assert log_statistics(read_logs(root.rglob("*.jsonl")))["dropped_total"]==8
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
