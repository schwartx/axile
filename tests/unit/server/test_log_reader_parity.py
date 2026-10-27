"""冻结纯 Python 结果，核验日志读取与文本兼容。"""

import gzip
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from axile.cli.main import app
from axile.common.log_files import LogDecoder, iter_file, log_statistics, read_logs, render_record, render_tree

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/log_reader_parity.json"


def files(tmp_path, fixture, compression):
    paths = []
    for item in fixture["files"]:
        content = item["jsonl"].encode("utf-8")
        if compression == "gzip":
            content = gzip.compress(content, mtime=0)
        elif compression == "members":
            midpoint = len(content) // 2
            content = gzip.compress(content[:midpoint], mtime=0) + gzip.compress(content[midpoint:], mtime=0)
        path = tmp_path / (item["name"] + (".gz" if compression != "raw" else ""))
        path.write_bytes(content)
        paths.append(path)
    return paths


@pytest.mark.parametrize("compression", ["raw", "gzip", "members"])
def test_reader_matches_frozen_python_types_and_statistics(tmp_path, compression):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = read_logs(files(tmp_path, fixture, compression))
    # 普通字典相等会把 10 和 10.0 视为相同，JSON 比较同时检查数值类型。
    assert json.dumps(records, ensure_ascii=False, sort_keys=True) == json.dumps(
        fixture["expected"]["records"], ensure_ascii=False, sort_keys=True
    )
    assert log_statistics(records) == fixture["expected"]["statistics"]


@pytest.mark.parametrize("entrypoint", ["read", "iter", "decoder"])
def test_definitions_stay_shared_without_sharing_mutable_event_or_span_dicts(tmp_path, entrypoint):
    rows = [
        ["H", 1, {"run_id": "shared", "time_base": "2026-10-27T00:00:00Z", "time_unit": "us"}],
        ["T", 1, "INFO", "事件 {detail}", ["detail"], {"component": "测试", "source": {"file": "fixture"}}],
        ["C", 1, {"account_id": 7, "execution_id": "E1", "nested": {"values": [1, 2]}}],
        ["S", 1, None, "过程", 1, 0],
        ["E", 1, 1, 1, 1, 1, [{"value": 1}]],
        ["E", 2, 2, 1, 1, 1, [{"value": 1}]],
    ]
    path = tmp_path / "shared.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if entrypoint == "read":
        records = read_logs([path])
    elif entrypoint == "iter":
        records = list(iter_file(path))
    else:
        decoder = LogDecoder()
        records = [record for row in rows if (record := decoder.feed(row)) is not None]
    first, second = records
    for field in ("context", "metadata", "fields", "run_id", "source"):
        assert first[field] is second[field], field
    assert first["spans"][0]["context"] is first["context"]
    first["spans"][0]["name"] = "edited copy"
    first["args"][0]["value"] = 99
    assert second["spans"][0]["name"] == "过程"
    assert second["args"][0]["value"] == 1
    fresh = read_logs([path])
    assert fresh[0]["context"] is not first["context"]
    assert fresh[0]["metadata"] is not first["metadata"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="系统不支持进程本地时区切换")
def test_cli_and_record_rendering_match_frozen_python_text(tmp_path, monkeypatch):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    paths = files(tmp_path, fixture, "raw")
    try:
        with monkeypatch.context() as environment:
            environment.setenv("TZ", "UTC")
            time.tzset()
            records = read_logs(paths)
            runner = CliRunner()
            timeline = runner.invoke(app, ["logs", *map(str, paths)])
            assert timeline.exit_code == 0, timeline.output
            assert timeline.stdout == fixture["expected"]["timeline_utc"] + "\n"
            assert (
                "\n".join(render_record(row) for row in records if not row.get("metadata", {}).get("log_stats"))
                == fixture["expected"]["timeline_utc"]
            )
            tree = runner.invoke(app, ["logs", *map(str, paths), "--tree"])
            assert tree.exit_code == 0, tree.output
            assert tree.stdout == fixture["expected"]["tree_utc"] + "\n"
            assert render_tree(records) == fixture["expected"]["tree_utc"]
    finally:
        time.tzset()
