"""新日志格式的证据导出预算、归属、去重与脱敏。"""

import gzip
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from axile.server.evidence.logs import collect_logs
from axile.server.evidence.models import EvidenceError, EvidenceLimits, EvidenceProfile, ExportBudget
from axile.server.evidence.privacy import EvidenceRedactor


def records():
    return [
        ["H", 1, {"run_id": "R1", "time_base": "2026-10-26T00:00:00+00:00", "time_unit": "us"}],
        ["T", 1, "WARNING", "{message}", ["message"], {"kind": "event"}],
        ["T", 2, "INFO", "{stats}", ["stats"], {"log_stats": True}],
        ["C", 1, {"account_id": 7, "execution_id": "E1"}],
        ["C", 2, {"account_id": 8, "execution_id": "E1"}],
        ["E", 1, 1, 1, 1, None, ["password=log-secret"]],
        ["E", 2, 2, 1, 2, None, ["wrong-account"]],
        ["E", 3, 3, 2, 0, None, [{"dropped_total": 5, "secret": "do-not-export"}]],
    ]


def snapshot():
    return SimpleNamespace(account={"id": 7}, iter_contexts=lambda: [SimpleNamespace(execution_id="E1")])


def test_new_raw_and_gzip_logs_keep_owner_privacy_and_do_not_double_count(tmp_path):
    content = "".join(json.dumps(row) + "\n" for row in records()).encode()
    (tmp_path / "axile-a.jsonl").write_bytes(content)
    (tmp_path / "axile-a.jsonl.gz").write_bytes(gzip.compress(content))
    rows, report = collect_logs(tmp_path, snapshot(), ExportBudget())
    assert len(rows) == 1
    assert "log_owner_conflict" in report["issues"] and "log_dropped_events" in report["issues"]
    assert report["logger_stats"]["dropped_total"] == 5
    shared = EvidenceRedactor(EvidenceProfile.SHAREABLE, 7, "account").clean(rows, "$logs")
    assert "log-secret" not in json.dumps(shared)
    assert "do-not-export" not in json.dumps(report)


def test_compressed_scan_budget_uses_expanded_bytes(tmp_path):
    content = "".join(json.dumps(row) + "\n" for row in records()).encode()
    (tmp_path / "axile-a.jsonl.gz").write_bytes(gzip.compress(content))
    limits = replace(EvidenceLimits(), log_scan_bytes=100)
    rows, report = collect_logs(tmp_path, snapshot(), ExportBudget(limits=limits))
    assert not rows and not report["read_complete"]
    assert "log_scan_limit_or_incomplete_line" in report["issues"]


def test_protocol_damage_reported_as_incomplete(tmp_path):
    rows = records() + [["E", 4, 4, 999, 0, None, []]]
    (tmp_path / "axile-a.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    _, report = collect_logs(tmp_path, snapshot(), ExportBudget())
    assert not report["read_complete"] and "log_invalid_protocol" in report["issues"]


def test_corrupt_gzip_does_not_hide_other_log_material(tmp_path):
    (tmp_path / "axile-a.jsonl.gz").write_bytes(bytes.fromhex("1f8b08000000000000ff07") + bytes(8))
    (tmp_path / "axile-b.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records()))
    rows, report = collect_logs(tmp_path, snapshot(), ExportBudget())
    assert len(rows) == 1 and report["scanned_files"] == 2
    assert not report["read_complete"] and "log_file_unavailable" in report["issues"]


def test_older_file_cannot_overwrite_a_later_cumulative_counter(tmp_path):
    for name, seq, dropped in [("a", 2, 7), ("z", 1, 3)]:
        rows = records()[:1] + [records()[2], ["E", seq, seq, 2, 0, None, [{"dropped_total": dropped}]]]
        (tmp_path / f"axile-{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    _, report = collect_logs(tmp_path, snapshot(), ExportBudget())
    assert report["logger_stats"]["dropped_total"] == 7


def test_log_collection_preserves_cancellation(tmp_path):
    (tmp_path / "axile-a.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records()))
    budget = ExportBudget()
    budget.cancelled.set()
    with pytest.raises(EvidenceError) as caught:
        collect_logs(tmp_path, snapshot(), budget)
    assert caught.value.code == "EVIDENCE_CANCELLED"
