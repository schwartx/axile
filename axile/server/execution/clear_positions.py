"""清仓执行场景."""

import loguru

from axile.server.core.log_config import execution_log_context
from axile.server.db.models import Account, ExecuteRecord
from axile.server.db.models.account import AccountContext
from axile.server.execution import backend as execution_backend
from axile.server.execution.execution_algorithms import resolve_empty_positions_algorithm
from axile.server.execution.execution_records_output import sanitize_standard_input_for_audit
from axile.server.execution.notification_assets import load_notification_snapshot
from axile.server.utils import trade_channel_check


async def _run_clear_positions(
    account: AccountContext,
    execution_id: str,
    algorithm: dict[str, object] | None = None,
) -> ExecuteRecord:
    """
    执行队列已认领的清仓任务，并持久化执行记录.

    Parameters
    ----------
    account : Account
        待清仓的账户对象。
    execution_id : str
        当前队列任务的 execution 标识。
    algorithm : dict[str, object] | None, optional
        显式指定的清仓算法配置；为空时使用账户级默认配置。

    Returns
    -------
    ExecuteRecord
        本次清仓对应的执行记录。
    """
    if isinstance(account, Account):
        account = account.snapshot()
    logger = loguru.logger
    with logger.contextualize(
        **execution_log_context(
            account_id=account.id,
            account_name=account.name,
            channel=account.trade_channel,
            execution_id=execution_id,
        )
    ):
        logger.info("清除持仓")
        await trade_channel_check(account, execution_id=execution_id)
        notification_snapshot = await load_notification_snapshot(account)
        request = _build_clear_positions_backend_request(
            account=account,
            algorithm=algorithm,
            execution_id=execution_id,
            logger=logger,
            notification_snapshot=notification_snapshot,
        )
        return await execution_backend.run_clear_positions_via_backend(request=request)


def _build_clear_positions_backend_request(
    *,
    account: AccountContext,
    algorithm: dict[str, object] | None,
    execution_id: str,
    logger: "loguru.Logger",
    notification_snapshot: dict[str, object] | None = None,
) -> execution_backend.ClearPositionsBackendRequest:
    """组装后端执行清仓所需的请求对象。"""
    if isinstance(account, Account):
        account = account.snapshot()
    resolved_algorithm = resolve_empty_positions_algorithm(account, algorithm)
    empty_kwargs = {
        "trade_rules": account.trade_rules,
        "futures_close_policy": account.futures_close_policy.model_dump(),
        "algorithm": resolved_algorithm,
        "feishu_key": account.feishu_key if account.feishu_key else None,
        "execution_notification_code": account.execution_notification_code,
        "feishu_account": {
            "id": account.id,
            "name": account.name,
            "market": account.market,
            "trade_channel": str(account.trade_channel),
            "brokerage": account.brokerage,
            "remark": account.remark,
            "portfolio_id": account.portfolio_id,
            "weight_precision": account.weight_precision,
            "long_leverage": account.long_leverage,
            "short_leverage": account.short_leverage,
        },
        "extra": {
            "audit": {
                "execution_id": execution_id,
                "account_id": account.id,
                "channel": str(account.trade_channel),
                "algorithm": resolved_algorithm.get("method", "SINGLE-MAKER"),
                "trigger_source": "empty_positions",
                "execution_kind": "clear_positions",
            }
        },
    }
    return execution_backend.ClearPositionsBackendRequest(
        account=account,
        resolved_algorithm=resolved_algorithm,
        empty_kwargs=empty_kwargs,
        audit_input=sanitize_standard_input_for_audit(empty_kwargs),
        execution_id=execution_id,
        trigger_source="empty_positions",
        logger=logger,
        notification_snapshot=notification_snapshot,
    )
