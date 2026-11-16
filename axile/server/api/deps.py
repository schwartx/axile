"""Axile API 路由共用的依赖提供器."""

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends
from msgspec import Meta

from axile.server.api.inputs import query_params
from axile.server.core.scheduler import Scheduler, scheduler


def get_scheduler() -> Scheduler:
    """返回应用共用的调度器实例."""
    return scheduler


SchedDep = Annotated[Scheduler, Depends(get_scheduler)]


@dataclass(kw_only=True)
class HistoryPagination:
    """历史列表接口共用的偏移分页参数。"""

    skip: Annotated[int, Meta(ge=0)] = field(default=0)
    limit: Annotated[int, Meta(ge=1, le=500)] = field(default=100)


HistoryPaginationDep = Annotated[HistoryPagination, Depends(query_params(HistoryPagination))]
