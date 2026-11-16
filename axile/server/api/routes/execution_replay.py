"""已封口执行录制的只读接口；不连接、刷新或操作交易账户。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.execution.registry import get_execution_status
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.execution.worker_backend.replay import replay_root
from axile.server.execution_replay import reader

router = APIRouter()


class _HeldResponse(Response):
    def __init__(self, body, release, check):
        headers = {"Cache-Control": "no-store"}
        if hasattr(check, "usage"):
            headers["X-Replay-Usage"] = json.dumps(check.usage(), separators=(",", ":"))
        if isinstance(body, bytes):
            headers["Content-Encoding"] = "identity"
        super().__init__(
            body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
            media_type="application/octet-stream" if isinstance(body, bytes) else "application/json",
            headers=headers,
        )
        self.release = release
        self.check = check

    async def __call__(self, scope, receive, send):
        async def transmit(message):
            self.check()
            await asyncio.wait_for(send(message), timeout=2)

        try:
            await transmit({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            for offset in range(0, len(self.body), 64 * 1024):
                await transmit(
                    {"type": "http.response.body", "body": self.body[offset : offset + 64 * 1024], "more_body": True}
                )
            await transmit({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.body = b""
            self.release()


@router.post("/executions/{execution_id}/replay/raw/{operation}", response_model=None)
async def replay_raw(execution_id: str, operation: str, request: Request):
    """验证执行归属后读取新格式原始字节；控制请求不重启读取。"""
    account = await _execution_account(execution_id)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise HTTPException(413, detail="REPLAY_REQUEST_BUDGET")
        body.extend(chunk)
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or len(value) > 32:
            raise ValueError("request object required")
        if operation == "session":
            return reader.readers.open(account, execution_id, replay_root(), value.get("resume"))
        if operation == "control":
            return reader.readers.control(account, execution_id, value)
        if operation not in {
            "manifest",
            "plan",
            "block",
            "blocks",
            "objects",
            "object-streams",
            "object-blocks",
            "members",
            "commands",
            "queries",
            "versions",
            "dependency-boundary",
            "market-origin",
            "object-links",
            "market-objects",
            "market-streams",
            "market-blocks",
            "market-links",
            "range-blocks",
            "business-blocks",
        }:
            raise HTTPException(404, detail="REPLAY_OPERATION")
        result, release, check = await run_in_threadpool(
            reader.read, replay_root(), account, execution_id, {**value, "operation": operation}
        )
        return _HeldResponse(result, release, check)
    except reader.ReadError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.code}) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, detail="REPLAY_REQUEST_INVALID") from exc
    except OSError as exc:
        raise HTTPException(409, detail="REPLAY_RAW_UNAVAILABLE") from exc


async def _execution_account(execution_id: str) -> int:
    """先从业务真源确认执行归属，不信任文件或客户端给出的账户。"""
    if not execution_id or len(execution_id) > 256:
        raise HTTPException(422, detail="无效的执行标识")
    execution = await get_execution_status(execution_id)
    if execution is None:
        raise HTTPException(404, detail="执行不存在")
    account_id = execution.get("account_id")
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        raise HTTPException(409, detail="执行账户归属无法确认")
    return account_id


@router.get("/{account_id}/replay/status", response_model=None)
async def replay_status(account_id: int) -> dict[str, object]:
    """分别返回保存意愿与实际录制状态，不创建 worker。"""
    account = await _get_account_or_404(account_id)
    return await get_worker_backend_manager().replay_status(account)
