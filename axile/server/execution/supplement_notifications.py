"""补发开跑前的日历判断；终态通知在原生 intent 事务内产生。"""

from axile.server.db.models.supplement import SupplementStep
from axile.server.execution.supplements import calendar_context
from axile.server.native_store import native_operation


async def execution_steps(execution_id: str) -> list[SupplementStep]:
    """读取执行承接的全部触发关联。"""
    return [
        SupplementStep.model_validate(step)
        for step in await native_operation("supplement_execution_steps", {"execution_id": execution_id})
    ]


async def validate_execution_requests(execution_id: str) -> bool:
    """事务外判断日历，原子撤销失效补发并保留共享的独立请求。"""
    snapshot = await native_operation("supplement_execution_requests", {"execution_id": execution_id})
    if not snapshot["steps"]:
        return True
    result = await native_operation(
        "supplement_validate_requests", {"execution_id": execution_id, **calendar_context(snapshot["channel"])}
    )
    from axile.server.execution.notification_outbox import wake_notifications

    if result["account_id"] is not None:
        wake_notifications(result["account_id"])
    return result["valid"]
