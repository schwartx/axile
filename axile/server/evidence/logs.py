"""有界读取现有 JSONL 日志；日志是独立时点的补充材料。"""

from __future__ import annotations

import json
import os
from datetime import UTC
from pathlib import Path
from typing import Any

from axile.executor.algorithms.utils.clock import clock_now
from axile.server.evidence.models import EvidenceSnapshot, ExportBudget


def collect_logs(
    directory: Path, snapshot: EvidenceSnapshot, budget: ExportBudget
) -> tuple[list[dict], dict[str, Any]]:
    """只扫描应用自身的日志文件，固定文件大小，不接受客户端路径。"""
    report: dict[str, Any] = {
        "requested": True,
        "read_complete": True,
        "coverage": "best_effort",
        "captured_at": clock_now(tz=UTC).isoformat(),
        "consistency": "separate_from_database_snapshot",
        "scanned_files": 0,
        "scanned_bytes": 0,
        "matched_rows": 0,
        "issues": [],
    }
    records: list[dict] = []
    if not directory.is_dir():
        report.update(read_complete=False, issues=["log_directory_unavailable"])
        return records, report
    # 多取一个用于识别上限，不能将未扫描材料描述为完整。
    files = []
    for path in directory.glob("axile*.jsonl"):
        if not path.is_symlink() and path.is_file():
            files.append(path)
        if len(files) > budget.limits.log_files:
            report["issues"].append("log_file_limit")
            break
    ids = {item.execution_id for item in snapshot.iter_contexts() if item.execution_id is not None}
    for path in sorted(files[: budget.limits.log_files]):
        try:
            with path.open("rb") as stream:
                remaining = os.fstat(stream.fileno()).st_size
                report["scanned_files"] += 1
                _scan(stream, remaining, ids, snapshot.account["id"], records, report, budget)
        except OSError:
            report["issues"].append("log_file_unavailable")
    report["matched_rows"] = len(records)
    report["read_complete"] = not report["issues"]
    report["issues"] = sorted(set(report["issues"]))
    if not files:
        report.update(read_complete=False, issues=["log_files_unavailable"])
    return records, report


def _scan(
    stream: Any, remaining: int, ids: set[str], account_id: int, target: list[dict], report: dict, budget: ExportBudget
) -> None:
    """按冻结长度扫描，单行也受总扫描预算约束。"""
    while remaining > 0:
        budget.check()
        available = budget.limits.log_scan_bytes - report["scanned_bytes"]
        if available <= 0:
            report["issues"].append("log_scan_limit")
            return
        line = stream.readline(min(remaining, available) + 1)
        if not line:
            report["issues"].append("log_file_shrunk")
            return
        if len(line) > min(remaining, available):
            report["issues"].append("log_scan_limit_or_incomplete_line")
            return
        remaining -= len(line)
        report["scanned_bytes"] += len(line)
        if not line.endswith(b"\n"):
            report["issues"].append("log_incomplete_line")
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            report["issues"].append("log_invalid_json")
            continue
        if not isinstance(row, dict) or not isinstance(row.get("context"), dict):
            continue
        context = row["context"]
        if context.get("execution_id") not in ids:
            continue
        if context.get("account_id") not in (None, account_id):
            report["issues"].append("log_owner_conflict")
            continue
        budget.check(rows=1, size=len(line))
        target.append(row)
