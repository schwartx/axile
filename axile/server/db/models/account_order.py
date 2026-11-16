"""当前委托及人工撤单的公开模型；不输出原始渠道载荷或账户凭据。"""

from dataclasses import dataclass
from typing import Annotated

from msgspec import Meta


@dataclass(kw_only=True)
class OrderCancelRequest:
    """带幂等请求标识的单笔撤单；客户端不自动重试。"""

    request_id: Annotated[str, Meta(min_length=1, max_length=100)]
    order_id: Annotated[str, Meta(min_length=1, max_length=200)]
    symbol: Annotated[str, Meta(min_length=1, max_length=100)]
