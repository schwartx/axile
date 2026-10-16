"""当前委托及人工撤单的公开模型；不输出原始渠道载荷或账户凭据。"""

from typing import Literal

from sqlmodel import Field, SQLModel


class AccountOrderPublic(SQLModel):
    """一笔当前委托及可公开的原生身份。"""

    order_id: str
    symbol: str
    direction: str
    offset: str | None = None
    order_type: str
    price: float
    volume: float
    filled_volume: float
    remaining_volume: float
    status: str
    create_time: str | None = None
    execution_id: str | None = None
    native_identity: dict[str, str] = Field(default_factory=dict)
    uncertain: bool = False
    update_time: str | None = None
    reason: str | None = None


class OrderCancelRequest(SQLModel):
    """带幂等请求标识的单笔撤单；客户端不自动重试。"""

    request_id: str = Field(min_length=1, max_length=100)
    order_id: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=100)


class OrderCancelReceipt(SQLModel):
    """请求受理与订单实际终态分别表达。"""

    request_id: str
    order_id: str
    symbol: str
    state: Literal["submitting", "submitted", "unknown", "failed", "terminal"]
    message: str
    requested_at: str
    order: AccountOrderPublic | None = None


class AccountOrdersPublic(SQLModel):
    """当前委托快照；不完整时不发布确定的笔数。"""

    data: list[AccountOrderPublic]
    count: int | None
    sampled_at: str
    complete: bool = True
    instance_id: str | None = None
    version: int = 0
    error: str | None = None
    cancellations: list[OrderCancelReceipt] = Field(default_factory=list)


class AccountTradePublic(SQLModel):
    """账户逐笔成交的公开字段，不输出渠道原始载荷。"""

    trade_id: str
    order_id: str
    symbol: str
    direction: str
    offset: str | None = None
    time: str
    price: float
    volume: float
    value: float
    trading_day: str | None = None
    hedge_flag: str | None = None
    exchange_trade_id: str | None = None
    amount: float | None = None
    commission: float | None = None
    commission_asset: str | None = None
    cost: float | None = None
    realized_pnl: float | None = None
    is_maker: bool | None = None
    position_side: str | None = None


class AccountOperationFailure(SQLModel):
    """操作失败记录与订单终态独立，失败不意味着订单已撤销。"""

    event_id: str
    operation: str
    outcome: str = "unknown"
    symbol: str
    order_id: str | None = None
    time: str
    message: str


class AccountActivityPublic(SQLModel):
    """近期委托、成交和操作失败；查询范围与采集范围显式公开。"""

    channel: str = ""
    orders: list[AccountOrderPublic]
    ended_orders: list[AccountOrderPublic] = Field(default_factory=list)
    trades: list[AccountTradePublic]
    failures: list[AccountOperationFailure]
    scope: str
    started_at: str
    sampled_at: str
    queried_at: str | None = None
    errors: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    limit: int
    truncated: bool = False
