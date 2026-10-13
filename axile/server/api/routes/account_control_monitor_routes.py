"""账户流控实时监控读接口。"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from axile.server.account_control.monitor import monitor
from axile.server.account_control.schema import AccountControlMonitorPublic
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404

router = APIRouter()


def _frame(data: dict) -> str:
    return f"event: {data['kind']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/control/stream")
async def control_stream(request: Request) -> StreamingResponse:
    """一条连接共享所有账户的只读快照，重连从全量开始。"""
    monitor.start()

    async def events() -> AsyncIterator[str]:
        async with monitor.subscribe() as queue:
            await monitor.refresh()
            yield _frame(monitor.envelope(list(monitor.frames.values())))
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _frame(data)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/{account_id}/control/monitor", response_model=AccountControlMonitorPublic)
async def control_monitor(account_id: int, session: SessionDep) -> dict:
    """单次读取与 SSE 相同的缓存模型，不创建交易实例。"""
    await _get_account_or_404(session, account_id)
    await monitor.refresh()
    return monitor.frames[account_id]
