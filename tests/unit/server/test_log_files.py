"""日志协议、过程树、压缩与命令行读取。"""

import gzip
import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from axile.cli.main import app
from axile.common.log_files import LogDecodeError, LogDecoder, iter_file, read_logs, render_record, render_tree
from axile.server.core.logging_sink import compress_log


def lines():
    return [
        ["H", 1, {"run_id": "R1", "time_base": "2026-10-26T00:00:00+00:00", "time_unit": "us"}],
        ["T", 1, "INFO", "订单 {id}", ["id", "extra"], {"component": "执行", "kind": "event"}],
        ["T", 2, "INFO", "过程开始", [], {"kind": "span_start"}],
        ["T", 3, "INFO", "过程结束", ["outcome"], {"kind": "span_end"}],
        ["C", 1, {"account_id": 7, "execution_id": "E1"}],
        ["S", 1, None, "检查", 1, 0],
        ["E", 1, 0, 2, 1, 1, []],
        ["E", 2, 1, 1, 1, 1, [42, {"中文": [True, None, '换行\n"引号"']}]],
        ["E", 3, 2, 3, 1, 1, ["ok"]],
    ]


def write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def test_types_unused_args_and_text_roundtrip(tmp_path):
    records = read_logs([write(tmp_path / "a.jsonl", lines())])
    assert records[1]["args"] == [42, {"中文": [True, None, '换行\n"引号"']}]
    assert 'extra={"中文":' in records[1]["message"]
    assert "[执行] [账户#7 E1]" in render_record(records[1])
    assert "检查 #1 · ok · 0.002ms" in render_tree(records)


@pytest.mark.parametrize(
    "bad",
    [
        ["E", 4, 3, 99, 1, 1, []],
        ["E", 4, 3, 1, 99, 1, [1, 2]],
        ["E", 4, 3, 1, 1, 99, [1, 2]],
        ["E", 4, 3, 1, 1, 1, [1]],
        ["E", 4, 3, 3, 1, 1, ["unknown"]],
        ["T", 1, "INFO", "冲突", [], {}],
        ["C", 1, {"account_id": 8}],
        ["S", 1, None, "不同名字", 1, 0],
        ["T", 4, "INFO", "{secret.private}", ["secret"], {}],
        ["T", 4, "INFO", "{0}", ["0"], {}],
        ["S", 2, 2, "循环", 1, 0],
    ],
)
def test_invalid_references_conflicts_and_args_are_explicit(tmp_path, bad):
    with pytest.raises(LogDecodeError):
        list(iter_file(write(tmp_path / "bad.jsonl", lines() + [bad])))


def test_partial_tail_and_middle_corruption_are_reported(tmp_path):
    path = write(tmp_path / "a.jsonl", lines())
    with path.open("ab") as stream:
        stream.write(b'["E",')
    with pytest.raises(LogDecodeError, match="incomplete"):
        read_logs([path])
    path.write_bytes(b"{bad}\n" + path.read_bytes())
    with pytest.raises(LogDecodeError, match="a.jsonl:1"):
        read_logs([path])


def test_same_name_nested_orphan_and_unfinished_spans(tmp_path):
    rows = lines()[:-1] + [
        ["S", 2, None, "检查", 1, 1],
        ["S", 3, 2, "子过程", 1, 2],
        ["S", 4, 999, "孤立过程", 1, 3],
        ["E", 3, 2, 2, 1, 2, []],
        ["E", 4, 3, 1, 1, 3, [7, None]],
        ["E", 5, 4, 1, 1, 4, [8, None]],
        ["E", 6, 5, 1, 0, None, [9, None]],
    ]
    tree = render_tree(read_logs([write(tmp_path / "tree.jsonl", rows)]))
    assert "检查 #1" in tree and "检查 #2" in tree and "子过程 #3" in tree
    assert "孤立过程 #4" in tree and "父过程缺失" in tree and "未观察到结束" in tree
    assert "订单 9" in tree


def test_gzip_integrity_and_failure_preserves_original(tmp_path, monkeypatch):
    original = write(tmp_path / "axile-a.jsonl", lines())
    content = original.read_bytes()
    compress_log(original)
    compressed = original.with_name(original.name + ".gz")
    assert not original.exists() and gzip.decompress(compressed.read_bytes()) == content
    assert len(read_logs([compressed])) == 3
    another = write(tmp_path / "axile-b.jsonl", lines())

    def fail(*args):
        raise OSError("failure")

    monkeypatch.setattr("axile.server.core.logging_sink.os.link", fail)
    with pytest.raises(OSError):
        compress_log(another)
    assert another.read_bytes() == content


def test_preexisting_compression_temporary_file_preserved(tmp_path):
    original = write(tmp_path / "axile-a.jsonl", lines())
    stale = original.with_name(original.name + ".gz.tmp")
    stale.write_bytes(b"previous attempt")
    with pytest.raises(FileExistsError):
        compress_log(original)
    assert original.exists() and stale.read_bytes() == b"previous attempt"


def test_cli_timeline_tree_and_damage_error(tmp_path):
    path = write(tmp_path / "a.jsonl", lines())
    runner = CliRunner()
    timeline = runner.invoke(app, ["logs", str(path)])
    assert timeline.exit_code == 0, timeline.output
    assert timeline.output.strip() == "\n".join(render_record(row) for row in read_logs([path]))
    tree = runner.invoke(app, ["logs", str(path), "--tree"])
    assert tree.exit_code == 0 and "检查 #1" in tree.output
    path.write_text("{bad}\n")
    damaged = runner.invoke(app, ["logs", str(path)])
    assert damaged.exit_code != 0 and "a.jsonl:1" in damaged.output


def test_header_and_monotonic_order_required():
    with pytest.raises(LogDecodeError, match="header"):
        LogDecoder().feed(lines()[1])
    decoder = LogDecoder()
    for row in lines():
        decoder.feed(row)
    with pytest.raises(LogDecodeError, match="monotonic"):
        decoder.feed(["E", 3, 3, 1, 1, None, [1, 2]])


def test_conflicts_across_individually_valid_fragments_rejected(tmp_path):
    first = write(tmp_path / "a.jsonl", lines())
    changed = lines()
    changed[1] = ["T", 1, "INFO", "不同模板 {id}", ["id", "extra"], {"component": "执行"}]
    second = write(tmp_path / "b.jsonl", changed[:-3] + [["E", 4, 4, 1, 1, None, [7, None]]])
    with pytest.raises(LogDecodeError, match="across files"):
        read_logs([first, second])


def test_non_finite_json_rejected(tmp_path):
    path = write(tmp_path / "bad.jsonl", lines()[:-3])
    with path.open("ab") as stream:
        stream.write(b'["E",1,1,1,1,null,[NaN,null]]\n')
    with pytest.raises(LogDecodeError, match="non-finite"):
        read_logs([path])


def test_offset_header_preserves_cached_span_times_and_record_independence(tmp_path):
    rows = lines()
    rows[0][2]["time_base"] = "2026-10-26T08:00:00+08:00"
    records = read_logs([write(tmp_path / "offset.jsonl", rows)])
    assert records[0]["timestamp"] == "2026-10-26T00:00:00.000000+00:00"
    assert records[0]["spans"][0]["start_time"] == records[0]["timestamp"]
    records[0]["spans"][0]["name"] = "edited copy"
    assert records[1]["spans"][0]["name"] == "检查"


def test_tree_handles_deep_nesting_without_recursive_text_copies():
    depth = sys.getrecursionlimit() + 10
    stamp = "2026-10-26T00:00:00+00:00"
    record = {
        "run_id": "R1",
        "seq": 1,
        "timestamp": stamp,
        "span_id": depth,
        "spans": [
            {"id": i, "parent": i - 1 if i > 1 else None, "name": f"步骤{i}", "start_time": stamp}
            for i in range(1, depth + 1)
        ],
        "template": "{message}",
        "fields": ["message"],
        "args": ["最深处"],
    }
    tree = render_tree([record])
    assert len(tree.splitlines()) == depth + 1
    assert f"步骤{depth} #{depth}" in tree and tree.endswith("最深处")


def test_corrupt_deflate_reports_a_named_log_error(tmp_path):
    path = tmp_path / "broken.jsonl.gz"
    path.write_bytes(bytes.fromhex("1f8b08000000000000ff07") + bytes(8))
    with pytest.raises(LogDecodeError, match="broken.jsonl.gz"):
        read_logs([path])


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_compression_does_not_widen_private_log_permissions(tmp_path):
    path = write(tmp_path / "private.jsonl", lines())
    path.chmod(0o600)
    previous = os.umask(0o022)
    try:
        compress_log(path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.with_name(path.name + ".gz").stat().st_mode) & 0o077 == 0


def test_tree_preserves_span_before_loose_event_on_equal_sort_keys():
    stamp = "2026-10-26T00:00:00+00:00"
    loose = {"run_id": "R1", "seq": 1, "timestamp": stamp, "message": "独立事件"}
    span = {"id": 1, "parent": None, "name": "根过程", "start_time": stamp}
    owned = {"run_id": "R1", "seq": 2, "timestamp": stamp, "span_id": 1, "spans": [span], "message": "过程内事件"}
    tree = render_tree([loose, owned])
    assert tree.startswith("根过程 #1")
    assert tree.index("过程内事件") < tree.index("独立事件")
