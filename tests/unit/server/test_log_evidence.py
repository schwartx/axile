"""新日志格式的证据导出预算、归属、去重与脱敏。"""

import gzip
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from axile.server.evidence.archive import build_archive
from axile.server.evidence.logs import collect_logs
from axile.server.evidence.models import EvidenceError, EvidenceLimits, EvidenceProfile, EvidenceSnapshot, ExportBudget
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
    budget = ExportBudget()
    rows, report = collect_logs(tmp_path, snapshot(), budget)
    assert len(rows) == 1
    assert budget.rows == 1
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


@pytest.mark.parametrize("compressed", [False, True])
def test_reused_context_is_charged_for_every_expanded_event(tmp_path, compressed):
    rows = records()[:2] + [
        ["C", 1, {"account_id": 7, "execution_id": "E1", "summary": "x" * 4096}],
        ["E", 1, 1, 1, 1, None, ["first"]],
        ["E", 2, 2, 1, 1, None, ["second"]],
    ]
    content = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path = tmp_path / ("axile-a.jsonl.gz" if compressed else "axile-a.jsonl")
    path.write_bytes(gzip.compress(content) if compressed else content)
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=5000))
    assert len(content) < budget.limits.bytes
    with pytest.raises(EvidenceError) as caught:
        collect_logs(tmp_path, snapshot(), budget)
    assert caught.value.code == "EVIDENCE_TOO_LARGE"
    assert budget.rows == 2


def test_repeated_template_fields_are_bounded_before_message_expansion(tmp_path):
    rows = records()[:1] + [
        ["T", 1, "INFO", "{value}" * 5000, ["value"], {}],
        ["C", 1, {"account_id": 7, "execution_id": "E1"}],
        ["E", 1, 1, 1, 1, None, ["x" * 1024]],
    ]
    (tmp_path / "axile-large.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=1000))
    with pytest.raises(EvidenceError) as caught:
        collect_logs(tmp_path, snapshot(), budget)
    assert caught.value.code == "EVIDENCE_TOO_LARGE"


def test_unrelated_events_do_not_consume_export_budget(tmp_path):
    rows = records()[:1] + [
        ["T", 1, "INFO", "{value}" * 5000, ["value"], {}],
        ["C", 1, {"account_id": 7, "execution_id": "unrelated"}],
        ["E", 1, 1, 1, 1, None, ["x" * 1024]],
    ]
    (tmp_path / "axile-unrelated.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=0))
    result, report = collect_logs(tmp_path, snapshot(), budget)
    assert result == [] and report["read_complete"]
    assert budget.rows == budget.bytes == 0


def test_header_utc_overflow_does_not_hide_other_material(tmp_path):
    header = records()[0]
    header[2]["time_base"] = "0001-01-01T00:00:00+01:00"
    (tmp_path / "axile-a.jsonl").write_text(json.dumps(header) + "\n")
    (tmp_path / "axile-b.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records()))
    rows, report = collect_logs(tmp_path, snapshot(), ExportBudget())
    assert len(rows) == 1
    assert "log_invalid_protocol" in report["issues"]


@pytest.mark.parametrize("argument", ["1e400", '"\\ud800"'])
def test_unexportable_argument_is_reported_without_charging_or_hiding_valid_logs(tmp_path, argument):
    rows = records()[:3] + [["E", 1, 1, 1, 1, None, ["invalid-argument"]]]
    content = "".join(json.dumps(row) + "\n" for row in rows).replace('"invalid-argument"', argument)
    (tmp_path / "axile-a.jsonl").write_text(content, encoding="utf-8")
    (tmp_path / "axile-b.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records()))
    budget = ExportBudget()
    result, report = collect_logs(tmp_path, snapshot(), budget)
    assert len(result) == budget.rows == 1
    assert {"log_invalid_json", "log_invalid_protocol"} & set(report["issues"])
    assert not report["read_complete"]


def test_archive_stops_redacting_logs_when_budget_is_exhausted(monkeypatch):
    first = {"context": {"summary": "x" * 20_000}}
    second = {"context": {"summary": "later"}}
    cleaned = []
    original = EvidenceRedactor.clean

    def clean(self, value, *args, **kwargs):
        assert value is not second, "later logs were copied before checking the export budget"
        if value is first:
            cleaned.append(value)
        return original(self, value, *args, **kwargs)

    monkeypatch.setattr(EvidenceRedactor, "clean", clean)
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=20_000))
    source = EvidenceSnapshot({"id": 7, "name": "account", "channel": "ctp"}, {})
    with pytest.raises(EvidenceError) as caught:
        build_archive(source, EvidenceProfile.SHAREABLE, budget, logs=[first, second], log_report={"requested": True})
    assert caught.value.code == "EVIDENCE_TOO_LARGE"
    assert cleaned == [first]


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


@pytest.mark.parametrize("difference", [0, -1])
def test_blocked_budget_keeps_exact_utf8_size_and_newline(tmp_path, difference):
    row = {
        "context": {"account_id": 7, "execution_id": "E1", "details": ["汉🙂"] * 12_000},
        "message": "记录",
    }
    content = json.dumps(row, ensure_ascii=False) + "\n"
    (tmp_path / "axile-large.jsonl").write_text(content, encoding="utf-8")
    size = len(content.encode("utf-8"))
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=size + difference))
    if difference:
        with pytest.raises(EvidenceError) as caught:
            collect_logs(tmp_path, snapshot(), budget)
        assert caught.value.code == "EVIDENCE_TOO_LARGE"
    else:
        rows, report = collect_logs(tmp_path, snapshot(), budget)
        assert rows == [row] and report["read_complete"]
        assert budget.rows == 1 and budget.bytes == size


@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_large_record_keeps_interrupt_checks_between_blocks(tmp_path, monkeypatch, reason):
    now = [0.0]
    monkeypatch.setattr("axile.server.evidence.models.get_default_clock", lambda: SimpleNamespace(time=lambda: now[0]))

    class InterruptedBudget(ExportBudget):
        def check(self, *, rows=0, size=0):
            super().check(rows=rows, size=size)
            if size > 1024:
                if reason == "cancelled":
                    self.cancelled.set()
                else:
                    now[0] = 2.0

    row = {"context": {"account_id": 7, "execution_id": "E1", "details": [0] * 50_000}, "message": "记录"}
    content = json.dumps(row, ensure_ascii=False) + "\n"
    (tmp_path / "axile-large.jsonl").write_text(content, encoding="utf-8")
    budget = InterruptedBudget(limits=replace(EvidenceLimits(), seconds=1.0))
    with pytest.raises(EvidenceError) as caught:
        collect_logs(tmp_path, snapshot(), budget)
    assert caught.value.code == {"cancelled": "EVIDENCE_CANCELLED", "timeout": "EVIDENCE_TIMEOUT"}[reason]
    assert budget.bytes < len(content.encode("utf-8"))
