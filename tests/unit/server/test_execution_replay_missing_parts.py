"""缺失文件只影响相应覆盖；后续真实 MCAP 检查点仍可导航和读取。"""

import json

import pytest

from axile.server._native import _core
from axile.server.execution_replay.service import replay_manifest, replay_window

TIME = 1_793_404_800_000_000
MINUTE = 60_000_000


def write_recording(root):
    writer = _core.ExecutionReplayWriter(str(root), 7, "missing-parts", '{"schema_version":1}')
    # 每个 part 自然轮转，且相邻覆盖连续，原先会合成一个不可跳出的时段。
    for sequence, offset in enumerate((0, MINUTE - 1, MINUTE, 2 * MINUTE - 1, 2 * MINUTE), 1):
        writer.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "recording_id": "missing-parts",
                    "capture_seq": sequence,
                    "time_us": TIME + offset,
                    "topic": "/execution",
                    "data": {"execution_id": "E", "phase": "started", "target_quantity": sequence},
                }
            )
        )
    writer.finish()
    return sorted(root.rglob("*.mcap"))


def window(root, start, end, cursor=None):
    request = {"recording_id": "missing-parts", "start_us": start, "end_us": end}
    if cursor is not None:
        request["cursor"] = cursor
    return replay_window(root, 7, "E", request)


@pytest.mark.parametrize("part_number", (0, 1, 2))
@pytest.mark.parametrize("damage", ("missing", "truncated"))
def test_manifest_excludes_unreadable_part_without_hiding_later_checkpoint(tmp_path, part_number, damage):
    files = write_recording(tmp_path)
    assert len(files) == 3
    original = replay_manifest(tmp_path, 7, "E")["recordings"][0]
    assert original["coverage"] == [{"start_us": TIME, "end_us": TIME + 2 * MINUTE + 1}]
    selected = files[part_number]
    if damage == "missing":
        selected.unlink()
    else:
        selected.write_bytes(selected.read_bytes()[:-1])

    manifest = replay_manifest(tmp_path, 7, "E")
    recording = manifest["recordings"][0]
    assert manifest["availability"] == recording["availability"] == "partial"
    assert {"reason": "file_missing_or_changed"} in recording["gaps"]
    assert recording["parts"] == original["parts"]
    expected = {
        0: [{"start_us": TIME + MINUTE, "end_us": TIME + 2 * MINUTE + 1}],
        1: [
            {"start_us": TIME, "end_us": TIME + MINUTE},
            {"start_us": TIME + 2 * MINUTE, "end_us": TIME + 2 * MINUTE + 1},
        ],
        2: [{"start_us": TIME, "end_us": TIME + 2 * MINUTE}],
    }
    assert recording["coverage"] == expected[part_number]
    for interval in recording["coverage"]:
        page = window(tmp_path, interval["start_us"], interval["end_us"])
        assert page["complete"] is True
        assert page["messages"]
        if interval["start_us"] > TIME:
            assert page["state_before_start"]["state_through_seq"] == (2 if part_number == 0 else 4)

    # 显式读取损坏范围仍报错，不能改为伪造空窗或跳过所需事实。
    with pytest.raises(_core.NativeError):
        window(tmp_path, TIME + part_number * MINUTE, TIME + part_number * MINUTE + 1)


def test_all_missing_parts_keep_partial_metadata_and_no_selectable_coverage(tmp_path):
    for path in write_recording(tmp_path):
        path.unlink()
    manifest = replay_manifest(tmp_path, 7, "E")
    recording = manifest["recordings"][0]
    assert manifest["availability"] == "partial"
    assert recording["coverage"] == []
    assert len(recording["parts"]) == 3
    assert {"reason": "file_missing_or_changed"} in recording["gaps"]


@pytest.mark.parametrize("same_time_parts", (1, 2))
def test_missing_same_microsecond_prefix_starts_after_checkpoint_time(tmp_path, same_time_parts):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "missing-parts", '{"schema_version":1}')
    for sequence in range(1, same_time_parts + 3):
        offset = 10 if sequence == same_time_parts + 2 else 0
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
        if sequence <= same_time_parts:
            writer.seal()
    writer.finish()
    sorted(tmp_path.rglob("*.mcap"))[0].unlink()
    recording = replay_manifest(tmp_path, 7, "E")["recordings"][0]
    assert recording["coverage"] == [{"start_us": TIME + 1, "end_us": TIME + 11}]
    page = window(tmp_path, TIME + 1, TIME + 11)
    assert page["state_before_start"]["state_through_seq"] == same_time_parts + 1
    assert [row["capture_seq"] for row in page["messages"]] == [same_time_parts + 2]
    with pytest.raises(_core.NativeError):
        window(tmp_path, TIME, TIME + 11)


@pytest.mark.parametrize("remove_descriptor", (False, True))
def test_missing_same_microsecond_middle_clips_both_sides_of_gap(tmp_path, remove_descriptor):
    writer = _core.ExecutionReplayWriter(str(tmp_path), 7, "missing-parts", '{"schema_version":1}')
    for sequence, offset in ((1, -10), (2, 0), (3, 0), (4, 0), (5, 10)):
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
        if sequence in (2, 3):
            writer.seal()
    writer.finish()
    sorted(tmp_path.rglob("*.mcap"))[1].unlink()
    if remove_descriptor:
        catalog_path = next(tmp_path.rglob("catalog.json"))
        catalog = json.loads(catalog_path.read_text())
        catalog["parts"] = [part for part in catalog["parts"] if part["part"] != 2]
        catalog_path.write_text(json.dumps(catalog))
    recording = replay_manifest(tmp_path, 7, "E")["recordings"][0]
    assert recording["coverage"] == [
        {"start_us": TIME - 10, "end_us": TIME},
        {"start_us": TIME + 1, "end_us": TIME + 11},
    ]
    before = window(tmp_path, TIME - 10, TIME)
    assert [row["capture_seq"] for row in before["messages"]] == [1]
    after = window(tmp_path, TIME + 1, TIME + 11)
    assert after["state_before_start"]["state_through_seq"] == 4
    assert [row["capture_seq"] for row in after["messages"]] == [5]
    with pytest.raises(_core.NativeError):
        window(tmp_path, TIME - 10, TIME + 1)
