"""Axile API 路由共用的依赖提供器."""

from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, Field

from axile.server.core.scheduler import Scheduler, scheduler


def get_scheduler() -> Scheduler:
    """返回应用共用的调度器实例."""
    return scheduler


SchedDep = Annotated[Scheduler, Depends(get_scheduler)]


class HistoryPagination(BaseModel):
    """历史列表接口共用的偏移分页参数。"""

    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


HistoryPaginationDep = Annotated[HistoryPagination, Depends()]
