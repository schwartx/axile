"""
定义执行器共享的订单状态常量.

Notes
-----
统一使用语义明确的中文状态字符串表示订单生命周期。

Examples
--------
使用示例::

    from axile.executor.constants.order_status import OrderStatus

    order.status = OrderStatus.PENDING

    if OrderStatus.is_completed(order.status):
        print("订单已完成")
"""


class OrderStatus:
    """
    定义统一的订单状态常量集合.

    Attributes
    ----------
    PENDING : str
        新订单等待成交时使用的状态值。
    SUBMITTED : str
        订单已报送但尚未成交时使用的状态值。
    PARTIALLY_FILLED : str
        订单部分成交时使用的状态值。
    FILLED : str
        订单全部成交时使用的状态值。
    CANCELED : str
        订单已撤销时使用的状态值。
    REJECTED : str
        订单被拒绝时使用的状态值。
    EXPIRED : str
        订单过期失效时使用的状态值。
    ACTIVE_STATUSES : list[str]
        活跃状态集合。
    COMPLETED_STATUSES : list[str]
        完成状态集合。

    Notes
    -----
    设计约束如下：

    - 优先使用长度更统一、语义更清晰的中文状态值。
    - 通过状态分组简化批量状态判断。
    """

    # 活跃状态
    PENDING = "待成交"  # 新订单，等待成交
    SUBMITTED = "已报"  # 已报送（部分渠道使用）
    PARTIALLY_FILLED = "部分成交"  # 部分成交
    UNKNOWN = "状态未知"  # 缺失或未知原生状态，不能推断为终态

    # 完成状态
    FILLED = "已成交"  # 全部成交
    CANCELED = "已撤销"  # 已撤销
    REJECTED = "已拒绝"  # 已拒绝
    EXPIRED = "已过期"  # 已过期

    # 状态分组
    ACTIVE_STATUSES = [PENDING, SUBMITTED, PARTIALLY_FILLED]
    COMPLETED_STATUSES = [FILLED, CANCELED, REJECTED, EXPIRED]

    @classmethod
    def is_active(cls, status: str) -> bool:
        """
        判断订单是否处于活跃状态.

        Parameters
        ----------
        status : str
            待判断的订单状态字符串。

        Returns
        -------
        bool
            若状态属于活跃状态集合则返回 ``True``，否则返回 ``False``。
        """
        return status in cls.ACTIVE_STATUSES

    @classmethod
    def is_completed(cls, status: str) -> bool:
        """
        判断订单是否已完成.

        Parameters
        ----------
        status : str
            待判断的订单状态字符串。

        Returns
        -------
        bool
            若状态属于完成状态集合则返回 ``True``，否则返回 ``False``。
        """
        return status in cls.COMPLETED_STATUSES
