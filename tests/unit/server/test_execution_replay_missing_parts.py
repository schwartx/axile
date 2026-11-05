"""真实接口对缺失或截断录制文件的处理。"""

import json

import pytest

from axile.server._native import _core
from axile.server.execution_replay.service import replay_manifest, replay_window

TIME = 1_793_404_800_000_000
MINUTE = 60_000_000


@pytest.mark.parametrize("damage", ("missing", "truncated"))
def test_missing_or_truncated_file_has_no_selectable_coverage(tmp_path, damage):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "missing-parts", '{"schema_version":1}')
    for sequence, offset in enumerate((0, MINUTE, 2 * MINUTE), 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "recording_id": "missing-parts",
                    "capture_seq": sequence,
                    "time_us": TIME + offset,
                    "topic": "/execution",
                    "data": {"execution_id": "E", "phase": "started"},
                }
            )
        )
    writer.finish()
    files = list(tmp_path.rglob("*.mcap"))
    assert len(files) == 1
    original = replay_manifest(tmp_path, 7, "E")["recordings"][0]
    assert original["coverage"] == [{"start_us": TIME, "end_us": TIME + 2 * MINUTE + 1}]
    if damage == "missing":
        files[0].unlink()
    else:
        files[0].write_bytes(files[0].read_bytes()[:-1])

    manifest = replay_manifest(tmp_path, 7, "E")
    recording = manifest["recordings"][0]
    assert manifest["availability"] == recording["availability"] == "partial"
    assert recording["coverage"] == []
    assert recording["parts"] == original["parts"]
    assert {"reason": "file_missing_or_changed"} in recording["gaps"]
    with pytest.raises(_core.NativeError):
        replay_window(tmp_path, 7, "E", {"recording_id": "missing-parts", "start_us": TIME, "end_us": TIME + 1})
