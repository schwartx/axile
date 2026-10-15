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
    cancellations: list[OrderCancelReceipt] = Field(default_factory=list)
