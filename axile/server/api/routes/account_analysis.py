"""后端增量分析；按事实修订号读取，不提供旧快照接口。"""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from msgspec import Meta

from axile.common.serialization import to_payload
from axile.server._native import _core
from axile.server.api.inputs import query_params
from axile.server.core.db import database_path
from axile.server.db.models.performance import RangeKey
from axile.server.native_store import native_operation
from axile.server.performance_analysis import enqueue
from axile.server.performance_details import CostQuery, CostSelection

router = APIRouter()
AccountId = Annotated[int, Path(gt=0, le=2**63 - 1)]


@dataclass(kw_only=True)
class AnalysisQuery:
    """请求必须绑定已观察到的事实修订号。"""

    version: Annotated[str, Meta(pattern="^[0-9]{1,19}$")]
    range: RangeKey = "all"

    __forbid_unknown__ = True


@dataclass(kw_only=True)
class DetailQuery(AnalysisQuery):
    """单次执行的有界明细读取。"""

    limit: Annotated[int, Meta(ge=1, le=500)] = field(default=200)
    offset: Annotated[int, Meta(ge=0, le=2**63 - 1)] = field(default=0)


@dataclass(kw_only=True)
class InputsQuery(AnalysisQuery, CostSelection):
    """成本输入只读取选区；费率试算保留完整窗口。"""

    cursor: Annotated[str, Meta(max_length=1024)] | None = field(default=None)
    kind: Literal["costs", "fees"] = "costs"

    def fee_window(self):
        """费率重放不能使用局部事实。"""
        if self.kind == "fees" and any(value is not None for value in (self.day, self.start, self.end, self.record_id)):
            raise ValueError("fee inputs require the complete window")
        return self

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()
        self.fee_window()


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
            "ANALYSIS_INPUT_LIMIT": 413,
        }.get(code, 503)
        raise HTTPException(status, detail={"code": code, "message": message}) from error


@router.get("/analysis/{account_id}/status", response_model=None)
async def get_status(account_id: AccountId):
    """只读取源版本、计算版本与任务状态。"""
    return await _query(account_id, "status")


@router.get("/analysis/{account_id}", response_model=None)
async def get_chart(account_id: AccountId, query: Annotated[AnalysisQuery, Depends(query_params(AnalysisQuery))]):
    """返回日级成果及按需读取的展示数据。"""
    return await _query(account_id, "chart", **to_payload(query, exclude_none=True))


@router.get("/analysis/{account_id}/costs", response_model=None)
async def get_costs(account_id: AccountId, query: Annotated[CostQuery, Depends(query_params(CostQuery))]):
    """按请求范围从规范事实计算成本和明细，不写派生副本。"""
    return await _query(account_id, "costs", **to_payload(query, exclude_none=True))


@router.get("/analysis/{account_id}/inputs", response_model=None)
async def get_inputs(account_id: AccountId, query: Annotated[InputsQuery, Depends(query_params(InputsQuery))]):
    """读取冻结事实供浏览器试算，不写入绩效或执行记录。"""
    return await _query(account_id, "inputs", **to_payload(query, exclude_none=True))


@router.get("/analysis/{account_id}/execution/{record_id}", response_model=None)
async def get_detail(
    account_id: AccountId,
    record_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
    query: Annotated[DetailQuery, Depends(query_params(DetailQuery))],
):
    """选中执行后读取其事实和成本明细。"""
    return await _query(account_id, "detail", record_id=record_id, **to_payload(query, exclude_none=True))


@router.post("/analysis/{account_id}/refresh", status_code=202, response_model=None)
async def refresh(account_id: AccountId):
    """请求重放，不影响交易执行。"""
    await _query(account_id, "status")
    await enqueue(database_path(), account_id, force=True)
    return {"status": "pending", "account_id": account_id}
