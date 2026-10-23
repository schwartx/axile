"""原生证据回执与 Python 归档预算、选择口径的交接。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import date

import pytest

from axile.server.evidence import collect
from axile.server.evidence.models import (
    EvidenceError,
    EvidenceLimits,
    EvidenceWindow,
    ExecutionEvidence,
    ExportBudget,
)


def _receipt():
    return {
        "account": {"id": 7, "name": "account", "channel": "ctp"},
        "executions": {"e1": asdict(ExecutionEvidence(key="e1", execution_id="e1"))},
        "account_operations": {},
        "source_counts": {"execution_event": 3},
        "rows": 3,
        "bytes": 100,
    }


def test_single_native_snapshot_becomes_archive_dto_and_charges_shared_budget(monkeypatch):
    calls = []

    async def read(request, cancelled):
        calls.append(("collect_evidence", request))
        return _receipt()

    monkeypatch.setattr(collect, "native_evidence_operation", read)
    budget = ExportBudget(rows=2, bytes=20)
    snapshot = asyncio.run(collect.collect_evidence(budget, account_id=7, execution_ids=["e1"]))
    assert len(calls) == 1
    assert calls[0][0] == "collect_evidence"
    assert calls[0][1]["limits"]["rows"] == budget.limits.rows - 2
    assert calls[0][1]["limits"]["bytes"] == budget.limits.bytes - 20
    assert snapshot.executions["e1"].execution_id == "e1"
    assert snapshot.selection_description() == {"basis": "explicit_execution_ids", "execution_ids": ["e1"]}
    assert (budget.rows, budget.bytes) == (5, 120)


@pytest.mark.parametrize(
    ("code", "status"),
    [("EVIDENCE_OWNER_CONFLICT", 409), ("EVIDENCE_NOT_FOUND", 404), ("EVIDENCE_TOO_LARGE", 413)],
)
def test_native_owner_and_budget_failures_preserve_public_error_codes(monkeypatch, code, status):
    async def fail(request, cancelled):
        raise collect._core.NativeError(code, "failure")

    monkeypatch.setattr(collect, "native_evidence_operation", fail)
    with pytest.raises(EvidenceError) as error:
        asyncio.run(collect.collect_evidence(ExportBudget(), execution_id="e1"))
    assert (error.value.code, error.value.status) == (code, status)


def test_cancelled_budget_never_starts_native_query(monkeypatch):
    calls = []

    async def read(request, cancelled):
        calls.append("collect_evidence")
        return _receipt()

    monkeypatch.setattr(collect, "native_evidence_operation", read)
    budget = ExportBudget(limits=replace(EvidenceLimits(), rows=1))
    budget.cancelled.set()
    with pytest.raises(EvidenceError) as error:
        asyncio.run(collect.collect_evidence(budget, execution_id="e1"))
    assert error.value.code == "EVIDENCE_CANCELLED"
    assert calls == []


def test_dst_window_is_fixed_before_crossing_to_native():
    window = EvidenceWindow(date(2026, 3, 8), date(2026, 3, 8), "America/New_York", "America/New_York")
    request = collect._window_request(window)
    assert request["end_ms"] - request["start_ms"] == 23 * 3600 * 1000
    assert request["legacy_timezone"] == "America/New_York"
    assert request["local_offsets"] == []


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="系统不支持进程本地时区切换")
@pytest.mark.parametrize(
    ("zone", "day", "boundary", "offset"),
    [
        ("America/New_York", "2026-03-08", "2026-03-08T03:00:00", -4 * 3600),
        ("Australia/Lord_Howe", "2026-10-04", "2026-10-04T02:30:00", 11 * 3600),
    ],
)
def test_server_local_interpretation_preserves_dst_gap_and_half_hour_boundary(zone, day, boundary, offset):
    program = """import json,time
from datetime import date
from axile.server.evidence.collect import _window_request
from axile.server.evidence.models import EvidenceWindow
time.tzset()
day=date.fromisoformat(__import__('sys').argv[1])
print(json.dumps(_window_request(EvidenceWindow(day,day))['local_offsets']))
"""
    offsets = json.loads(
        subprocess.check_output(
            [sys.executable, "-c", program, day],
            env={**os.environ, "TZ": zone},
            text=True,
            timeout=5,
        )
    )
    assert [boundary, offset] in offsets
