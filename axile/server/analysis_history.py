"""从规范业务事实组装单条分析记录。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    """一次活写入或显式冷重建使用的输入，不绑定 ORM 隐式文档属性。"""

    id: int
    account_id: int
    execution_id: str | None
    created_at: str
    is_success: int
    input: object
    result: object

    @classmethod
    def from_detail(cls, detail: Mapping[str, Any]) -> "AnalysisRecord":
        """接收规范业务仓库显式组装的单条详情。"""
        return cls(**{name: detail[name] for name in cls.__dataclass_fields__})


async def read_analysis_record(record: Any) -> AnalysisRecord:
    """为冷补齐或显式不支持的兼容计算，组装一条规范业务记录。"""
    from axile.server.business_facts.repository import read_execution_detail

    return AnalysisRecord.from_detail(await read_execution_detail(record))
