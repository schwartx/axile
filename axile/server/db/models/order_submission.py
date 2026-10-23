"""原生报单流水，与额度计数独立持久化，不从历史放行记录推测发送。"""

from typing import Any

from axile.server.db.models.base import Model


class OrderSubmission(Model):
    """以调用唯一身份去重，并保留最新版本的同步及异步结果。"""

    submission_uid: str
    account_id: int
    occurred_at_ms: int
    revision: int
    data: dict[str, Any]


class OrderSubmissionMonitor(Model):
    """账户原生调用监测阈值；不参与风控额度放行。"""

    account_id: int
    data: dict[str, Any]


class OrderSubmissionWarning(Model):
    """阈值当笔的不可变预警；同配置版本及自然日仅产生一次。"""

    event_uid: str
    account_id: int
    occurred_at_ms: int
    data: dict[str, Any]
