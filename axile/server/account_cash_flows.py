"""账户流水同步复用已就绪 worker，不发起转账。"""

from axile.channels import get_channel
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.server.connection_state import connection_config_key
from axile.server.db.models.account import AccountContext
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.native_store import native_operation


async def sync_cash_flows(account: AccountContext) -> dict:
    """读取渠道可查询的流水，并在单个事务中去重落库。"""
    if not get_channel(str(account.trade_channel)).descriptor.cash_flow_sync:
        raise NotImplementedError("该渠道暂不支持流水同步，请手工录入")
    rows = await get_worker_backend_manager().get_account_cash_flows(account)
    return await native_operation(
        "cash_flow_operation",
        {
            "op": "sync",
            "account_id": account.id,
            "rows": rows,
            "now": clock_now_iso(),
            "config_key": connection_config_key(account.trade_channel, account.account_config),
        },
    )
