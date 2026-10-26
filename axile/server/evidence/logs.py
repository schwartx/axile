"""有界读取现有 JSONL 日志；日志是独立时点的补充材料。"""

from __future__ import annotations

import gzip
import os
import zlib
from contextlib import nullcontext
from datetime import UTC
from io import BufferedIOBase
from itertools import islice
from pathlib import Path
from typing import Any

from axile.common.log_files import LogDecoder, decode_json, statistics_record
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.evidence.models import EvidenceSnapshot, ExportBudget


def collect_logs(
    directory: Path, snapshot: EvidenceSnapshot, budget: ExportBudget
) -> tuple[list[dict], dict[str, Any]]:
    """只扫描应用自身的日志文件，固定文件大小，不接受客户端路径。"""
    collector = _LogCollector(snapshot, budget)
    report = collector.report
    if not directory.is_dir():
        report["issues"].append("log_directory_unavailable")
    else:
        # 多取一个用于识别上限，不能将未扫描材料描述为完整。
        files = list(
            islice(
                (
                    path
                    for path in directory.glob("axile*.jsonl*")
                    if not path.is_symlink() and path.is_file() and path.name.endswith((".jsonl", ".jsonl.gz"))
                ),
                budget.limits.log_files + 1,
            )
        )
        if not files:
            report["issues"].append("log_files_unavailable")
        if len(files) > budget.limits.log_files:
            report["issues"].append("log_file_limit")
        for path in sorted(files[: budget.limits.log_files]):
            collector.scan(path)
    report["matched_rows"] = len(collector.records)
    if collector.counters:
        report["logger_stats"] = {
            "dropped_total": sum(collector.counters.values()),
            "runs": len(collector.counters),
            "coverage": "last_saved_checkpoint",
        }
    report["read_complete"] = not report["issues"]
    report["issues"] = sorted(set(report["issues"]))
    return collector.records, report


class _LogCollector:
    """在分片之间共享归属、预算及去重状态，不保留文件句柄。"""

    def __init__(self, snapshot: EvidenceSnapshot, budget: ExportBudget) -> None:
        self.ids = {item.execution_id for item in snapshot.iter_contexts() if item.execution_id is not None}
        self.account_id = snapshot.account["id"]
        self.budget = budget
        self.records: list[dict] = []
        self.counters: dict[str, int] = {}
        self.definitions: dict[tuple, object] = {}
        self.seen: dict[tuple, dict] = {}
        self.report: dict[str, Any] = {
            "requested": True,
            "coverage": "best_effort",
            "captured_at": clock_now(tz=UTC).isoformat(),
            "consistency": "separate_from_database_snapshot",
            "scanned_files": 0,
            "scanned_bytes": 0,
            "issues": [],
        }

    def scan(self, path: Path) -> None:
        """按冻结长度读取原始分片，压缩分片按展开后的字节计费。"""
        try:
            with path.open("rb") as raw:
                compressed = path.suffix == ".gz"
                remaining = None if compressed else os.fstat(raw.fileno()).st_size
                self.report["scanned_files"] += 1
                with gzip.GzipFile(fileobj=raw, mode="rb") if compressed else nullcontext(raw) as stream:
                    self._read(stream, remaining)
        except (OSError, EOFError, zlib.error):
            self.report["issues"].append("log_file_unavailable")

    def _read(self, stream: BufferedIOBase, remaining: int | None) -> None:
        """单行也受总扫描预算约束，损坏记录不影响其余材料。"""
        decoder = LogDecoder(self.definitions)
        while remaining is None or remaining > 0:
            self.budget.check()
            available = self.budget.limits.log_scan_bytes - self.report["scanned_bytes"]
            if available <= 0:
                self.report["issues"].append("log_scan_limit")
                return
            limit = min(remaining, available) if remaining is not None else available
            line = stream.readline(limit + 1)
            if not line:
                if remaining is not None:
                    self.report["issues"].append("log_file_shrunk")
                return
            if len(line) > limit:
                self.report["issues"].append("log_scan_limit_or_incomplete_line")
                return
            if remaining is not None:
                remaining -= len(line)
            self.report["scanned_bytes"] += len(line)
            if not line.endswith(b"\n"):
                self.report["issues"].append("log_incomplete_line")
                continue
            try:
                value = decode_json(line)
            except (ValueError, UnicodeDecodeError):
                self.report["issues"].append("log_invalid_json")
                continue
            try:
                row = decoder.feed(value)
                stats = statistics_record(row) if row is not None else None
            except (ValueError, TypeError):
                self.report["issues"].append("log_invalid_protocol")
                continue
            if row is None:
                continue
            if stats is not None:
                run = row["run_id"]
                self.counters[run] = max(self.counters.get(run, 0), stats["dropped_total"])
                if stats["dropped_total"]:
                    self.report["issues"].append("log_dropped_events")
            self._append(row, len(line))

    def _append(self, row: dict, size: int) -> None:
        """只收集归属明确的事件，压缩前后重复材料不重复计入。"""
        context = row["context"]
        if context.get("execution_id") not in self.ids:
            return
        if context.get("account_id") not in (None, self.account_id):
            self.report["issues"].append("log_owner_conflict")
            return
        if "run_id" in row and "seq" in row:
            identity = (row["run_id"], row["seq"])
            if identity in self.seen:
                if self.seen[identity] != row:
                    self.report["issues"].append("log_event_conflict")
                return
            self.seen[identity] = row
        self.budget.check(rows=1, size=size)
        self.records.append(row)
