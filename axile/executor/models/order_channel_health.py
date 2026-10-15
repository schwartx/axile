"""订单回报通道健康度契约.

定义跨渠道通用的订单回报通道（例如 WebSocket 用户数据流）健康度枚举，
供执行器上报、会话代理透传，以及常驻账户判断是否需要恢复核对。放在中立的
``models`` 层，避免通用组件（``ExecutionSession`` / ``OrderTracker``）反向依赖具体
渠道实现。
"""

from __future__ import annotations

from enum import Enum


class OrderChannelHealth(str, Enum):
    """
    订单回报通道健康度.

    Attributes
    ----------
    HEALTHY : str
        通道活跃，持续接收订单回报。
    DEGRADED : str
        通道可疑（心跳陈旧，或下单后迟迟收不到该单回报）；委托状态需要核对。
    DOWN : str
        通道确认失联；等待恢复订阅并核对账户基线。
    UNKNOWN : str
        执行器未提供健康度信息；连接状态由对应渠道维护。
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"
