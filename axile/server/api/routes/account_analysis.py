"""Version-bound, small performance reads without query-time native processes."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from axile.server._native import _core
from axile.server.core.db import analysis_store
from axile.server.db.models.performance import RangeKey
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION

router = APIRouter()
SQLITE_MAX_INTEGER = 2**63 - 1
AccountId = Annotated[int, Path(gt=0, le=SQLITE_MAX_INTEGER)]


def _native_http_error(error: _core.NativeError) -> HTTPException:
    """将原生读取错误映射为 HTTP 契约，不回退旧读取。"""
    if error.args[0] == "ACCOUNT_NOT_FOUND":
        return HTTPException(404, detail="账户不存在")
    status_code = {
        "SNAPSHOT_EXPIRED": 410,
        "SNAPSHOT_NOT_FOUND": 404,
        "EXECUTION_NOT_FOUND": 404,
        "ANALYSIS_READ_VIEW_UNAVAILABLE": 409,
    }.get(error.args[0], 503)
    return HTTPException(status_code, detail={"code": error.args[0], "message": error.args[1]})


async def _op(call, *args):
    """在线程池执行原生查询，保留结构化错误。"""
    try:
        return await run_in_threadpool(call, *args)
    except _core.NativeError as error:
        raise _native_http_error(error) from error


class ChartQuery(BaseModel):
    """Pin every chart read to an explicit retained snapshot."""

    model_config = ConfigDict(extra="forbid")
    snapshot_id: str = Field(min_length=1, max_length=256)
    range: RangeKey = "all"


class SummaryQuery(ChartQuery):
    """Deterministic pagination, optionally restricted to a finite viewport."""

    limit: int = Field(default=200, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=SQLITE_MAX_INTEGER)
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_window(self):
        """Require a complete nonempty viewport in epoch milliseconds."""
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must define a nonempty interval")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start and end must define a nonempty interval")
        return self


class DetailQuery(BaseModel):
    """Select one execution and bounded evidence pages from its fixed snapshot."""

    model_config = ConfigDict(extra="forbid")
    snapshot_id: str = Field(min_length=1, max_length=256)
    record_id: int = Field(gt=0, le=SQLITE_MAX_INTEGER)
    limit: int = Field(default=200, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=SQLITE_MAX_INTEGER)


@router.get("/performance/{account_id}/status")
async def get_status(account_id: AccountId) -> dict:
    """Return small publication status independently of displayed page data."""
    return await _op(_core.status, analysis_store(), account_id, LOGIC_VERSION, ENGINE_VERSION)


@router.get("/performance/{account_id}/chart")
async def get_chart(account_id: AccountId, query: Annotated[ChartQuery, Query()]) -> dict:
    """Read typed curves and flat indexes without historical execution details."""
    return await _op(_core.chart, analysis_store(), account_id, query.snapshot_id, query.range)


@router.get("/performance/{account_id}/execution-summary")
async def get_execution_summary(account_id: AccountId, query: Annotated[SummaryQuery, Query()]) -> dict:
    """Read frozen execution summary rows in a stable page."""
    return await _op(
        _core.execution_summary,
        analysis_store(),
        account_id,
        query.snapshot_id,
        query.range,
        query.limit,
        query.offset,
        query.start,
        query.end,
    )


@router.get("/performance/{account_id}/execution-detail")
async def get_execution_detail(account_id: AccountId, query: Annotated[DetailQuery, Query()]) -> dict:
    """Read only the selected execution's positions, attempts and transactions."""
    return await _op(
        _core.execution_detail,
        analysis_store(),
        account_id,
        query.snapshot_id,
        query.record_id,
        query.limit,
        query.offset,
    )
