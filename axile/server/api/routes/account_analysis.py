"""Version-bound, small performance reads without query-time native processes."""

from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.server.analysis_views import read_chart, read_execution_detail, read_execution_summary, read_status
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models.performance import RangeKey

router = APIRouter()
SQLITE_MAX_INTEGER = 2**63 - 1
AccountId = Annotated[int, Path(gt=0, le=SQLITE_MAX_INTEGER)]


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
async def get_status(session: SessionDep, account_id: AccountId) -> dict:
    """Return small publication status independently of displayed page data."""
    await _get_account_or_404(session, account_id)
    return await read_status(session, account_id)


@router.get("/performance/{account_id}/chart")
async def get_chart(session: SessionDep, account_id: AccountId, query: Annotated[ChartQuery, Query()]) -> dict:
    """Read typed curves and flat indexes without historical execution details."""
    await _get_account_or_404(session, account_id)
    return await read_chart(session, account_id, query.snapshot_id, query.range)


@router.get("/performance/{account_id}/execution-summary")
async def get_execution_summary(
    session: SessionDep, account_id: AccountId, query: Annotated[SummaryQuery, Query()]
) -> dict:
    """Read frozen execution summary rows in a stable page."""
    await _get_account_or_404(session, account_id)
    return await read_execution_summary(
        session,
        account_id,
        query.snapshot_id,
        query.range,
        limit=query.limit,
        offset=query.offset,
        start=query.start,
        end=query.end,
    )


@router.get("/performance/{account_id}/execution-detail")
async def get_execution_detail(
    session: SessionDep, account_id: AccountId, query: Annotated[DetailQuery, Query()]
) -> dict:
    """Read only the selected execution's positions, attempts and transactions."""
    await _get_account_or_404(session, account_id)
    return await read_execution_detail(
        session,
        account_id,
        query.snapshot_id,
        query.record_id,
        limit=query.limit,
        offset=query.offset,
    )
