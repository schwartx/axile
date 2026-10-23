"""执行及账户日期范围的只读证据导出接口。"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from starlette.types import Receive, Scope, Send

from axile.common.config import settings
from axile.server.evidence.archive import EvidenceArchive, build_archive
from axile.server.evidence.collect import collect_evidence
from axile.server.evidence.logs import collect_logs
from axile.server.evidence.models import EvidenceError, EvidenceProfile, EvidenceWindow, ExportBudget

router = APIRouter()
_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "完整证据 ZIP",
        "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
    },
    404: {"description": "执行或账户不存在"},
    408: {"description": "生成超时"},
    409: {"description": "证据归属冲突或无效数据"},
    413: {"description": "超出导出规模"},
}


class EvidenceExecutionSelection(BaseModel):
    """限定同一账户的明确执行清单，防止同日范围无法拆分。"""

    model_config = ConfigDict(extra="forbid")
    execution_ids: list[StrictStr] = Field(min_length=1, max_length=100)

    @field_validator("execution_ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        """拒绝重复、空白及过长标识，不修改历史标识。"""
        if any(not value.strip() or len(value) > 256 for value in values) or len(set(values)) != len(values):
            raise ValueError("execution_ids 须为不重复的非空标识，每项最多 256 字符")
        return values


class EvidenceFileResponse(FileResponse):
    """正常下载、断开连接或发送失败均释放临时文件。"""

    def __init__(self, archive: EvidenceArchive) -> None:
        self.archive = archive
        super().__init__(
            archive.path,
            filename=archive.filename,
            media_type="application/zip",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """释放动作覆盖整个 ASGI 响应生命周期。"""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.archive.close()


async def _export(profile: EvidenceProfile, include_logs: bool, **selection: Any) -> EvidenceFileResponse:
    """异步采集，线程打包；取消时等待线程释放半成品后退出。"""
    budget = ExportBudget()
    task = None
    delivered = False
    try:
        async with asyncio.timeout(budget.limits.seconds):
            snapshot = await collect_evidence(budget, **selection)
            logs, report = [], None
            if include_logs:
                logs, report = await asyncio.to_thread(collect_logs, settings.app_log_dir, snapshot, budget)
            task = asyncio.create_task(
                asyncio.to_thread(build_archive, snapshot, profile, budget, logs=logs, log_report=report)
            )
            archive = await asyncio.shield(task)
            response = EvidenceFileResponse(archive)
            delivered = True
            return response
    except EvidenceError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
    except TimeoutError as exc:
        raise HTTPException(408, detail={"code": "EVIDENCE_TIMEOUT", "message": "证据导出超时，请缩小范围"}) from exc
    finally:
        budget.cancelled.set()
        if task is not None and not delivered:
            try:
                archive = await asyncio.shield(task)
                archive.close()
            except Exception:
                # 原始异常由上面的 await 传播，取消清理不能覆盖它。
                pass


@router.get("/executions/{execution_id}/evidence", response_class=FileResponse, responses=_RESPONSES)
async def export_execution_evidence(
    execution_id: str,
    profile: EvidenceProfile = EvidenceProfile.SHAREABLE,
    include_logs: bool = False,
) -> FileResponse:
    """导出单次执行的全部已持久化证据，包括运行中和结果未知的执行。"""
    return await _export(profile, include_logs, execution_id=execution_id)


@router.get("/{account_id}/evidence", response_class=FileResponse, responses=_RESPONSES)
async def export_account_evidence(
    account_id: int,
    start_date: date,
    end_date: date,
    timezone: str = "Asia/Shanghai",
    legacy_timezone: str | None = None,
    profile: EvidenceProfile = EvidenceProfile.SHAREABLE,
    include_logs: bool = False,
) -> FileResponse:
    """导出日期范围内有活动的执行；结束日期包含在选择中，最长 31 天。"""
    try:
        window = EvidenceWindow(start_date, end_date, timezone, legacy_timezone)
    except EvidenceError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
    return await _export(profile, include_logs, account_id=account_id, window=window)


@router.post("/{account_id}/evidence", response_class=FileResponse, responses=_RESPONSES)
async def export_selected_execution_evidence(
    account_id: int,
    selection: EvidenceExecutionSelection,
    profile: EvidenceProfile = EvidenceProfile.SHAREABLE,
    include_logs: bool = False,
) -> FileResponse:
    """只读导出同一账户的 1～100 个执行，不扫描整天或触发交易。"""
    return await _export(profile, include_logs, account_id=account_id, execution_ids=selection.execution_ids)
