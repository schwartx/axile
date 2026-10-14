"""订单参数模型能力位回归测试."""

from __future__ import annotations

from typing import Any, cast

from axile.channels import get_channel
from axile.channels.contracts import ChannelPlugin
from axile.common.order_param_model import OrderParamModel
from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
from axile.executor.execution_session import ExecutionSession


class _Logger:
    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        _ = message, args, kwargs


def test_session_reads_real_executor_capability() -> None:
    class Owner(AbstractExecutorCapabilityMixin):
        channel_type = TradeChannel.CTP
        logger = _Logger()

    session = ExecutionSession(owner=cast("Any", Owner()), symbol="rb2610")
    assert session.order_param_model is OrderParamModel.OFFSET


def test_channel_plugin_defaults_to_unknown() -> None:
    """ChannelPlugin 未声明时默认 UNKNOWN(外部插件注册后自声明)."""
    gm = get_channel(TradeChannel.GM)
    plugin = ChannelPlugin(
        descriptor=gm.descriptor,
        account_config_model=gm.account_config_model,
        create_executor=gm.create_executor,
        target_transform=gm.target_transform,
    )
    assert plugin.order_param_model is OrderParamModel.UNKNOWN


def test_builtin_channels_declare_models() -> None:
    """内置三渠道声明正确的订单参数模型."""
    assert get_channel(TradeChannel.CTP).order_param_model is OrderParamModel.OFFSET
    assert get_channel(TradeChannel.TQ).order_param_model is OrderParamModel.OFFSET
    assert get_channel(TradeChannel.GM).order_param_model is OrderParamModel.DIRECTIONAL


def test_session_order_param_model_duck_typed_forward() -> None:
    """ExecutionSession 鸭子类型转发 owner 声明;替身未提供时回退 UNKNOWN."""

    class _Owner:
        channel_type = TradeChannel.CTP

        def __init__(self, model: object) -> None:
            self.logger = _Logger()
            self._model = model

        @property
        def order_param_model(self) -> object:
            return self._model

    session = ExecutionSession(owner=cast("Any", _Owner(OrderParamModel.OFFSET)), symbol="rb2610")
    assert session.order_param_model is OrderParamModel.OFFSET

    session_none = ExecutionSession(owner=cast("Any", _Owner(None)), symbol="rb2610")
    assert session_none.order_param_model is OrderParamModel.UNKNOWN

    class _BareOwner:
        channel_type = TradeChannel.CTP

        def __init__(self) -> None:
            self.logger = _Logger()

    session_bare = ExecutionSession(owner=cast("Any", _BareOwner()), symbol="rb2610")
    assert session_bare.order_param_model is OrderParamModel.UNKNOWN
