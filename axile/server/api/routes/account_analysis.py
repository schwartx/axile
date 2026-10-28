"""后端增量分析；按事实修订号读取，不提供旧快照接口。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from axile.server._native import _core
from axile.server.core.db import database_path
from axile.server.db.models.performance import RangeKey
from axile.server.native_store import native_operation
from axile.server.performance_analysis import enqueue
from axile.server.performance_details import CostQuery

router = APIRouter()
AccountId = Annotated[int, Path(gt=0, le=2**63 - 1)]


class AnalysisQuery(BaseModel):
    """请求必须绑定已观察到的事实修订号。"""

    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9]{1,19}$")
    range: RangeKey = "all"


class DetailQuery(AnalysisQuery):
    """单次执行的有界明细读取。"""

    limit: int = Field(default=200, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=2**63 - 1)


async def _query(account_id: int, command: str, **query):
    try:
        return await native_operation("analysis_query", {"account_id": account_id, "command": command, **query})
    except _core.NativeError as error:
        code, message = error.args[:2]
        status = {
            "ACCOUNT_NOT_FOUND": 404,
            "EXECUTION_NOT_FOUND": 404,
            "ANALYSIS_VERSION_CHANGED": 409,
            "ANALYSIS_PENDING": 409,
            "INVALID_REQUEST": 422,
        }.get(code, 503)
        raise HTTPException(status, detail={"code": code, "message": message}) from error


@router.get("/analysis/{account_id}/status")
async def get_status(account_id: AccountId):
    """只读取源版本、计算版本与任务状态。"""
    return await _query(account_id, "status")


@router.get("/analysis/{account_id}")
async def get_chart(account_id: AccountId, query: Annotated[AnalysisQuery, Query()]):
    """返回日级成果及按需读取的展示数据。"""
    return await _query(account_id, "chart", **query.model_dump(exclude_none=True))


@router.get("/analysis/{account_id}/costs")
async def get_costs(account_id: AccountId, query: Annotated[CostQuery, Query()]):
    """按请求范围从规范事实计算成本和明细，不写派生副本。"""
    return await _query(account_id, "costs", **query.model_dump(exclude_none=True))


@router.get("/analysis/{account_id}/execution/{record_id}")
async def get_detail(
    account_id: AccountId, record_id: Annotated[int, Path(gt=0, le=2**63 - 1)], query: Annotated[DetailQuery, Query()]
):
    """选中执行后读取其事实和成本明细。"""
    return await _query(account_id, "detail", record_id=record_id, **query.model_dump(exclude_none=True))


@router.post("/analysis/{account_id}/refresh", status_code=202)
async def refresh(account_id: AccountId):
    """请求重放，不影响交易执行。"""
    await _query(account_id, "status")
    await enqueue(database_path(), account_id, force=True)
    return {"status": "pending", "account_id": account_id}
