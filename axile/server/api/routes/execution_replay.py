"""已封口执行录制的只读接口；不连接、刷新或操作交易账户。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from axile.server._native import _core
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.execution.intents import get_intent
from axile.server.execution.registry import get_execution_status
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.execution.worker_backend.replay import replay_root
from axile.server.execution_replay import service

router = APIRouter()
_SAFE_INTEGER = 9_007_199_254_740_991


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


async def _read(function, account_id: int, execution_id: str, *args) -> dict[str, object]:
    """隔离文件读取错误；归属冲突不能变成另一账户的可读数据。"""
    try:
        result = await run_in_threadpool(function, replay_root(), account_id, execution_id, *args)
    except _core.NativeError as exc:
        code = exc.args[0] if exc.args else "REPLAY_READ"
        status, message = {
            "REPLAY_BUDGET": (413, "读取超过预算，请缩短范围或选择其他录制段"),
            "REPLAY_INVALID_REQUEST": (422, "回放窗口参数无效"),
            "REPLAY_INVALID_CURSOR": (409, "分页游标与所选窗口不符，请重新加载"),
            "REPLAY_VIEW_CHANGED": (409, "录制文件已变化，请重新加载窗口"),
            "REPLAY_NOT_RECORDED": (404, "本次执行中没有所选录制段"),
            "REPLAY_INCOMPLETE": (409, "录制基线或已封口覆盖不完整"),
            "REPLAY_SCHEMA": (409, "当前版本无法解释此录制格式"),
            "REPLAY_CORRUPT": (409, "录制文件校验失败"),
            "REPLAY_IO": (503, "录制文件暂时无法读取"),
        }.get(code, (409, "录制文件不可读"))
        raise HTTPException(
            status, detail={"code": code if code.startswith("REPLAY_") else "REPLAY_READ", "message": message}
        ) from exc
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(409, detail="录制文件不可读、窗口失效或超出读取预算") from exc
    if result.get("account_id", account_id) != account_id or result.get("execution_id", execution_id) != execution_id:
        raise HTTPException(409, detail="录制文件归属与执行不符")
    return {**result, "account_id": account_id, "execution_id": execution_id}


@router.get("/{account_id}/replay/status")
async def replay_status(account_id: int) -> dict[str, object]:
    """分别返回保存意愿与实际录制状态，不创建 worker。"""
    account = await _get_account_or_404(account_id)
    return await get_worker_backend_manager().replay_status(account)


@router.get("/executions/{execution_id}/replay")
async def replay_manifest(execution_id: str) -> dict[str, object]:
    """读取历史录制段与覆盖原因，不调用当前渠道解释旧数据。"""
    account_id = await _execution_account(execution_id)
    intent = await get_intent(execution_id)
    start_us = None
    end_us = None
    if intent is not None and intent.account_id != account_id:
        raise HTTPException(409, detail="执行意图归属与执行不符")
    if intent is not None and intent.started_at is not None:
        delta = datetime.fromisoformat(intent.started_at) - datetime(1970, 1, 1, tzinfo=UTC)
        start_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    if intent is not None and intent.finished_at is not None:
        delta = datetime.fromisoformat(intent.finished_at) - datetime(1970, 1, 1, tzinfo=UTC)
        end_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    cutoff_us = end_us if end_us is not None and 0 <= end_us <= _SAFE_INTEGER else None
    result = await _read(service.replay_manifest, account_id, execution_id, cutoff_us)
    return {**result, "execution_start_us": start_us, "execution_end_us": end_us}


@router.get("/executions/{execution_id}/replay/window")
async def replay_window(
    execution_id: str,
    recording_id: Annotated[str, Query(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")],
    start_us: Annotated[int, Query(ge=0, le=_SAFE_INTEGER)],
    end_us: Annotated[int, Query(ge=0, le=_SAFE_INTEGER)],
    symbols: Annotated[str | None, Query(max_length=4096)] = None,
    cursor: Annotated[str | None, Query(max_length=512 * 1024)] = None,
) -> dict[str, object]:
    """读取单个 recording 的固定窗口，游标绑定由原生层验证。"""
    if end_us <= start_us:
        raise HTTPException(422, detail="窗口结束必须晚于开始")
    request: dict[str, object] = {"recording_id": recording_id, "start_us": start_us, "end_us": end_us}
    if symbols is not None:
        selected = symbols.split(",")
        if len(selected) > 128 or any(
            not symbol or len(symbol) > 256 or any(char < " " for char in symbol) for symbol in selected
        ):
            raise HTTPException(422, detail="品种筛选无效")
        request["symbols"] = selected
    if cursor is not None:
        request["cursor"] = cursor
    account_id = await _execution_account(execution_id)
    result = await _read(service.replay_window, account_id, execution_id, request)
    if result.get("recording_id") != recording_id:
        raise HTTPException(409, detail="录制段与请求不符")
    return result
