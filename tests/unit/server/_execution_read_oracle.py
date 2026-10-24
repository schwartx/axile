"""历史执行列表与详情的响应模型。"""

from typing import Any

from axile.server.db.models.execution import ExecuteRecordPublic


class ExecuteRecordSummaryPublic(ExecuteRecordPublic):
    """历史列表响应契约。"""

    symbol_results: dict[str, dict[str, Any]] | None = None
    trade_count: int | None = None
    symbol_count: int | None = None


class ExecuteRecordDetailPublic(ExecuteRecordSummaryPublic):
    """历史详情响应契约。"""

    input: Any
    result: Any
