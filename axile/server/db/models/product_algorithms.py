"""账户品种的固定调仓算法配置。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from axile.server.db.models.account_validation import _validate_algorithm_config


class ProductAlgorithm(BaseModel):
    """每个品种独立保存完整算法，不继承账户算法参数。"""

    model_config = ConfigDict(extra="forbid")
    algorithm: dict[str, Any]

    @field_validator("algorithm")
    @classmethod
    def validate_algorithm(cls, value: dict[str, Any]) -> dict[str, Any]:
        """复用完整算法的参数校验。"""
        _validate_algorithm_config(value, "品种默认算法")
        return {**value, "params": value.get("params") or {}}
