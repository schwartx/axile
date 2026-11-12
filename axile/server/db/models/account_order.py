"""当前委托及人工撤单的公开模型；不输出原始渠道载荷或账户凭据。"""

from pydantic import Field

from axile.server.db.models.base import Model


class OrderCancelRequest(Model):
    """带幂等请求标识的单笔撤单；客户端不自动重试。"""

    request_id: str = Field(min_length=1, max_length=100)
    order_id: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=100)
