"""管理 UI 的应用级业务实时订阅入口。"""

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from axile.server.event_stream import stream_response

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/stream")
async def events_stream(request: Request) -> StreamingResponse:
    """合并执行态与账户流控，各域分别发送全量、增量和错误。"""
    return stream_response(request)
