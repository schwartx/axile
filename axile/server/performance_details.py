"""Version-bound cost pagination over compact indexed projections."""

import base64
import hashlib
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.server.db.models.performance import RangeKey


class CostQuery(BaseModel):
    """An immutable snapshot plus exact filter and ordering scope."""

    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    range: RangeKey = "all"
    dimension: Literal["execution", "symbol", "trade", "summary"] = "execution"
    sort: Literal["time", "cost"] = "time"
    day: str | None = None
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)
    record_id: int | None = None
    symbol: str | None = None
    symbol_search: str | None = None
    side: Literal["buy", "sell", "none"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def boundaries(self):
        """Reject ambiguous or malformed time selectors."""
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must define a nonempty interval")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start and end must define a nonempty interval")
        if self.day:
            if self.start is not None:
                raise ValueError("day and interval cannot be combined")
            from datetime import date

            if date.fromisoformat(self.day).isoformat() != self.day:
                raise ValueError("day must be YYYY-MM-DD")
        return self


def _cursor(query: CostQuery, account_id: int) -> tuple[str, int]:
    scope = query.model_dump(exclude={"cursor"}) | {"account_id": account_id}
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    offset = 0
    if query.cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(query.cursor))
            offset = payload["offset"]
            if payload["scope"] != digest or type(offset) is not int or offset < 0:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(
                400, detail={"code": "INVALID_COST_CURSOR", "message": "游标与查询条件不匹配"}
            ) from None
    return digest, offset
