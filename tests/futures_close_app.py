"""平仓设置浏览器验收服务：使用真实 DTO，不启动柜台、数据库或调度。"""

from fastapi import FastAPI, HTTPException

from axile.server.api.routes.account_crud import _account_public
from axile.server.api.routes.algorithms import router as algorithms_router
from axile.server.api.routes.capabilities import router as capabilities_router
from axile.server.db.models import Account, AccountUpdate

app = FastAPI()
app.include_router(algorithms_router, prefix="/api/v1")
app.include_router(capabilities_router, prefix="/api/v1")
_accounts: dict[int, Account] = {}


def sync():
    """生成无交易含义的已同步状态。"""
    return {"status": "synchronized", "revision": 1, "attempts": 1}


@app.post("/api/v1/test/reset")
def reset():
    """仅重置内存中的无凭证示例账户。"""
    _accounts.clear()
    for account_id, channel in ((1, "ctp"), (2, "tq")):
        _accounts[account_id] = Account(
            id=account_id,
            name=f"{channel.upper()} 平仓规则验收",
            market="期货",
            trade_channel=channel,
            account_control_preset="default",
            account_config={},
            is_started=False,
            cron_expr="",
            remark=None,
            brokerage="fixture",
            weight_precision=0.01,
            algorithm={"method": "TARGET-POS-TASK", "params": {}},
            feishu_key=None,
            trade_rules=None,
            forbidden_symbols=None,
            risk_symbols=None,
        )
    return {"ok": True}


@app.get("/api/v1/account/dashboard")
def dashboard():
    """页面保存后的列表刷新不产生任何交易操作。"""
    return {"data": [], "count": 0}


@app.get("/api/v1/account/{account_id}/runtime-sync")
def runtime_sync(account_id: int):
    """无需 worker 的测试同步状态。"""
    return sync()


@app.get("/api/v1/account/{account_id}")
def account_info(account_id: int):
    """真实公开响应模型必须保留平仓规则。"""
    return _account_public(_accounts[account_id]).model_dump(mode="json") | {"runtime_sync": sync()}


@app.patch("/api/v1/account/{account_id}")
def update_account(account_id: int, patch: AccountUpdate):
    """校验后仅修改内存账户；不存在任何柜台连接。"""
    account = _accounts[account_id]
    try:
        policy = patch.futures_close_policy or account.futures_close_policy
        policy.validate_channel(account.trade_channel)
        _accounts[account_id] = Account.model_validate(account.model_dump() | patch.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return account_info(account_id)


reset()
