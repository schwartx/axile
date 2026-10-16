"""服务级内存合约目录管理接口，沿用顶层 API 鉴权。"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from axile.executor.instrument_catalog import CatalogError
from axile.server.api.deps import SessionDep
from axile.server.db.models import Account
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.execution.worker_backend.manager import get_worker_backend_manager

router = APIRouter(prefix="/instrument-catalog", tags=["instrument-catalog"])


class InstrumentQuery(BaseModel):
    """已连接账户的有界精确查询，不隐式登录或全量刷新。"""

    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    symbols: list[StrictStr] = Field(min_length=1, max_length=50)
    timeout_seconds: float = Field(default=5, gt=0, le=15)
    require_specs: bool = False

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        """只接受明确单个代码，拒绝空白、重复或过长输入。"""
        if any(not value.strip() or value != value.strip() or len(value) > 96 for value in values) or len(
            set(values)
        ) != len(values):
            raise ValueError("symbols 须为不重复的非空合约代码，每项最多 96 字符")
        return values


@router.post("/query")
async def query_instruments(query: InstrumentQuery, session: SessionDep) -> dict[str, Any]:
    """通过已连接账户精确查询静态资料，负查询不回收健康交易会话。"""
    account = await session.get(Account, query.account_id)
    if account is None:
        raise HTTPException(404, detail={"code": "ACCOUNT_NOT_FOUND", "message": "账户不存在"})
    try:
        rows = await get_worker_backend_manager().query_instruments(
            account.snapshot(), query.symbols, timeout=query.timeout_seconds, require_specs=query.require_specs
        )
    except CatalogError as exc:
        status = {"CATALOG.NOT_FOUND": 404, "CATALOG.AMBIGUOUS": 409, "CATALOG.TIMEOUT": 408}.get(exc.reason_code, 503)
        raise HTTPException(
            status, detail={"code": exc.reason_code, "message": str(exc), "causes": exc.causes}
        ) from exc
    except AccountConnectionUnavailable as exc:
        raise HTTPException(503, detail={"code": "CHANNEL_NOT_READY", "message": str(exc)}) from exc
    return {"data": rows, "count": len(rows)}


@router.get("")
def catalog_status() -> dict[str, Any]:
    """读取目录状态，不查询柜台。"""
    return get_worker_backend_manager().instrument_catalog.status()


@router.post("/refresh", status_code=202)
def refresh_catalog() -> dict[str, Any]:
    """异步启动或复用现有全量任务。"""
    return get_worker_backend_manager().instrument_catalog.refresh()


@router.get("/refresh/{task_id}")
def refresh_status(task_id: str) -> dict[str, Any]:
    """查询刷新进度，任务只保存在本进程内存中。"""
    task = get_worker_backend_manager().instrument_catalog.task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="合约目录刷新任务不存在")
    return task
