"""临时文件归档与校验；生成完成后才允许下载。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from axile.server.evidence.models import (
    EvidenceError,
    EvidenceProfile,
    EvidenceSnapshot,
    ExecutionEvidence,
    ExportBudget,
)
from axile.server.evidence.privacy import EvidenceRedactor
from axile.server.evidence.report import (
    account_operation_report,
    control_summary,
    csv_bytes,
    execution_markdown,
    execution_report,
    folder,
    index_markdown,
)
from axile.server.execution.environment import package_version


@dataclass
class EvidenceArchive:
    """拥有临时目录的完整归档，由响应结束或失败路径释放。"""

    directory: TemporaryDirectory
    path: Path
    filename: str

    def close(self) -> None:
        """立即删除临时归档，不持久化新的业务数据。"""
        self.directory.cleanup()


class _Writer:
    """逐个写入文件并记录脱敏后的真实字节校验值。"""

    def __init__(self, archive: ZipFile, budget: ExportBudget) -> None:
        self.archive = archive
        self.budget = budget
        self.files: list[dict[str, Any]] = []

    def write(self, path: str, data: bytes, *, source: str, rows: int | None = None) -> None:
        """先检查上限，再写入 ZIP；清单记录文件来源及行数。"""
        self.budget.check(size=len(data))
        self.archive.writestr(path, data)
        self.files.append(
            {
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "source": source,
                "rows": rows,
            }
        )

    def json(self, path: str, value: Any, *, source: str) -> None:
        """以固定编码写出 JSON。"""
        self.write(path, _json(value), source=source, rows=len(value) if isinstance(value, list) else None)

    def jsonl(self, path: str, values: Iterable[dict], *, source: str) -> None:
        """逐行检查预算并压缩，避免大量账户操作先拼成无界的大字节串。"""
        checksum = hashlib.sha256()
        rows, size = 0, 0
        with self.archive.open(path, "w") as stream:
            for value in values:
                data = _json(value)
                self.budget.check(size=len(data))
                stream.write(data)
                checksum.update(data)
                rows += 1
                size += len(data)
        self.files.append({"path": path, "bytes": size, "sha256": checksum.hexdigest(), "source": source, "rows": rows})


def _json(value: Any) -> bytes:
    """生成稳定的 UTF-8 JSON；数值和未知值保持原义。"""
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise EvidenceError("证据包含无法序列化的数据", code="EVIDENCE_INVALID_DATA", status=409) from exc


def _clean_item(item: ExecutionEvidence, redactor: EvidenceRedactor) -> ExecutionEvidence:
    """先脱敏事实，再从同一材料派生所有表格和摘要。"""
    group = "account_operations" if item.context_kind == "account_operation" else "executions"
    return ExecutionEvidence(
        key=item.key,
        execution_id=item.execution_id,
        selection_reasons=item.selection_reasons,
        context_kind=item.context_kind,
        sources=redactor.clean(item.sources, f"$contexts.{folder(item.key)}.sources"),
        **{
            field: redactor.clean(getattr(item, field), f"{group}.{folder(item.key)}.{field}")
            for field in ("intent", "record", "events", "artifacts", "controls", "counters", "sessions")
        },
    )


def _write_account_operations(writer: _Writer, snapshot: EvidenceSnapshot) -> dict[str, Any]:
    """独立账户操作集中归档，不为每次轮询生成一整套执行目录。"""
    prefix = "account-operations"
    writer.jsonl(
        f"{prefix}/contexts.jsonl",
        (account_operation_report(item) for item in snapshot.account_operations.values()),
        source="derived_from_persisted_account_control",
    )
    for filename, field, source in (
        ("control-events", "controls", "account_control_event"),
        ("counter-deltas", "counters", "account_control_counter_delta"),
        ("control-sessions", "sessions", "account_control_session"),
    ):
        writer.jsonl(
            f"{prefix}/{filename}.jsonl",
            (row for item in snapshot.account_operations.values() for row in getattr(item, field)),
            source=source,
        )
    return {"count": len(snapshot.account_operations), "index": f"{prefix}/contexts.jsonl"}


def _write_execution(writer: _Writer, item: ExecutionEvidence) -> dict[str, Any]:
    """每次执行使用相同包结构，单次与日期导出无需两套实现。"""
    prefix = f"executions/{folder(item.key)}"
    writer.json(f"{prefix}/intent.json", item.intent, source="execution_intent")
    writer.json(f"{prefix}/record.json", item.record, source="executerecord")
    writer.json(f"{prefix}/artifacts.json", item.artifacts, source="execution_artifact")
    writer.json(f"{prefix}/control-sessions.json", item.sessions, source="account_control_session")
    writer.jsonl(f"{prefix}/events.jsonl", item.events, source="execution_event")
    writer.jsonl(f"{prefix}/control-events.jsonl", item.controls, source="account_control_event")
    writer.jsonl(f"{prefix}/counter-deltas.jsonl", item.counters, source="account_control_counter_delta")
    report, orders, trades, recon = execution_report(item)
    for name, rows, fields in (
        (
            "orders",
            orders,
            [
                "symbol",
                "order_id",
                "client_order_id",
                "direction",
                "order_type",
                "price",
                "volume",
                "filled_volume",
                "status",
                "extra",
                "source_path",
                "source_record_id",
            ],
        ),
        (
            "trades",
            trades,
            [
                "symbol",
                "trade_id",
                "order_id",
                "trade_time",
                "trade_price",
                "trade_volume",
                "trade_value",
                "extra",
                "source_path",
                "source_record_id",
            ],
        ),
        (
            "reconciliation",
            recon,
            ["symbol", "status", "before", "target", "after", "filled", "reached", "moved", "drift", "source_path"],
        ),
    ):
        writer.write(
            f"{prefix}/{name}.csv", csv_bytes(rows, fields), source="derived_from_persisted_evidence", rows=len(rows)
        )
    writer.write(
        f"{prefix}/summary.md", execution_markdown(report, recon).encode(), source="derived_from_persisted_evidence"
    )
    report["path"] = prefix
    return report


def build_archive(
    snapshot: EvidenceSnapshot,
    profile: EvidenceProfile,
    budget: ExportBudget,
    *,
    logs: list[dict] | None = None,
    log_report: dict[str, Any] | None = None,
) -> EvidenceArchive:
    """构建成功返回资源所有权；任何异常均清除半成品。"""
    directory = TemporaryDirectory(prefix="axile-evidence-")
    path = Path(directory.name) / "evidence.zip"
    try:
        _build(path, snapshot, profile, budget, logs or [], log_report)
        scope = "account" if snapshot.window or snapshot.selected_execution_ids is not None else "execution"
        return EvidenceArchive(directory=directory, path=path, filename=f"axile-{scope}-evidence.zip")
    except BaseException:
        directory.cleanup()
        raise


def _build(
    path: Path,
    snapshot: EvidenceSnapshot,
    profile: EvidenceProfile,
    budget: ExportBudget,
    logs: list[dict],
    log_report: dict | None,
) -> None:
    """压缩在独立线程运行，数据库事务已结束。"""
    redactor = EvidenceRedactor(profile, snapshot.account["id"], snapshot.account["name"])
    account_name = (
        redactor._text(snapshot.account["name"], "$account") if profile is EvidenceProfile.INTERNAL else "账户 A"
    )
    account = {"id": snapshot.account["id"], "name": account_name, "channel": snapshot.account["channel"]}
    if profile is EvidenceProfile.SHAREABLE:
        account["id"] = "account-A"
    cleaned = EvidenceSnapshot(
        account=account,
        executions={key: _clean_item(item, redactor) for key, item in snapshot.executions.items()},
        account_operations={key: _clean_item(item, redactor) for key, item in snapshot.account_operations.items()},
        window=snapshot.window,
        snapshot_at=snapshot.snapshot_at,
        source_counts=snapshot.source_counts,
        selected_execution_ids=snapshot.selected_execution_ids,
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        writer = _Writer(archive, budget)
        reports = [_write_execution(writer, item) for item in cleaned.executions.values()]
        account_operations = _write_account_operations(writer, cleaned)
        writer.write(
            "summary.md",
            index_markdown(cleaned, reports, redactor._text(account_name, "$account")).encode(),
            source="derived_from_persisted_evidence",
        )
        writer.write(
            "executions.csv",
            csv_bytes(
                reports,
                [
                    "execution_id",
                    "key",
                    "context_kind",
                    "sources",
                    "task_status",
                    "output_status",
                    "business_outcome",
                    "created_at",
                    "finished_at",
                    "orders",
                    "trades",
                    "coverage",
                    "path",
                ],
            ),
            source="derived_from_persisted_evidence",
            rows=len(reports),
        )
        controls = control_summary(cleaned)
        writer.write(
            "control-summary.csv",
            csv_bytes(controls, ["control_date", "operation", "decision", "outcome", "counted", "events"]),
            source="account_control_event; window_only",
            rows=len(controls),
        )
        if log_report and log_report["requested"]:
            writer.jsonl("logs.jsonl", redactor.clean(logs, "$logs"), source="application_log; independent_capture")
        manifest = {
            "schema_version": 2,
            "export_id": uuid4().hex,
            "exporter": {"axile_version": package_version("axile")},
            "account": account,
            "snapshot_at": snapshot.snapshot_at,
            "selection": snapshot.selection_description(),
            "database": {
                "read_complete": True,
                "consistency": "single_explicit_read_transaction",
                "source_counts": snapshot.source_counts,
            },
            "logs": log_report or {"requested": False, "coverage": "not_requested"},
            "executions": reports,
            "account_operations": account_operations,
            "redaction": redactor.describe(),
            "files": writer.files,
            "limitations": [
                "audit_delivery_is_best_effort",
                "execution_success_does_not_prove_fill_or_final_position",
                "orders_are_execution_scoped_not_current_account_inventory",
                "historical_execution_version_unknown_without_environment_snapshot",
            ],
        }
        writer.json("manifest.json", manifest, source="export_manifest")
    budget.check()
