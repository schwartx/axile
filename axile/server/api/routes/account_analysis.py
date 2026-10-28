"""账户事实同步；收益、回测和成本查询由浏览器执行。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field

from axile.server._native import _core
from axile.server.analysis_calendar import ensure_calendar_facts
from axile.server.native_store import native_operation

router = APIRouter()
AccountId = Annotated[int, Path(gt=0, le=2**63 - 1)]


async def _op(name: str, request: dict):
    """账户不存在、事实版本变化分别返回明确的读取错误。"""
    try:
        return await native_operation(name, request)
    except _core.NativeError as error:
        code, message = error.args[:2]
        status = {"ACCOUNT_NOT_FOUND": 404, "SOURCE_CHANGED": 409, "INVALID_REQUEST": 422, "FACT_TOO_LARGE": 413}.get(
            code, 503
        )
        raise HTTPException(status, detail={"code": code, "message": message}) from error


@router.get("/performance/{account_id}/facts-state")
async def get_state(account_id: AccountId) -> Response:
    """只读取修订号、数据源身份及分析设置。"""
    return Response(
        await _op("browser_analysis_state", {"account_id": account_id}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/performance/{account_id}/facts-prepare")
async def prepare_facts(account_id: AccountId) -> Response:
    """在事务外补齐渠道日历，随后返回可用于分页的一致性修订号。"""
    context = await _op("analysis_context", {"account_id": account_id})
    if context is None:
        raise HTTPException(404, detail="账户不存在")
    await ensure_calendar_facts(context["trade_channel"], context["first_day"], context["last_day"])
    return await get_state(account_id)


class FactCursor(BaseModel):
    """按修订号、表序号及行主键恢复分页。"""

    model_config = ConfigDict(extra="forbid")
    seq: str = Field(pattern=r"^[0-9]{1,19}$")
    table: int = Field(ge=0, le=22, strict=True)
    after: list[Annotated[str, Field(max_length=8192)]] | None = Field(default=None, max_length=3)


class FactPageQuery(BaseModel):
    """同步截至指定修订号；表名和列由原生白名单决定。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)
    until: str = Field(pattern=r"^[0-9]{1,19}$")
    cursor: FactCursor


@router.post("/performance/{account_id}/facts")
async def get_facts(account_id: AccountId, query: FactPageQuery) -> Response:
    """在短只读事务中跨执行打包，最多 4096 行、2 MiB；游标支持中断续传。"""
    return Response(
        await _op("browser_analysis_facts", {"account_id": account_id, **query.model_dump()}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
