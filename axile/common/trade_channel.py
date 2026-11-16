"""定义开放的交易渠道标识类型."""

from __future__ import annotations

from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


class TradeChannel(str):
    """
    表示可由运行时插件扩展的交易渠道标识.

    接受任意非空字符串，可用渠道由 :mod:`axile.channels` 注册表维护。
    """

    def __new__(cls, value: object) -> TradeChannel:
        """
        创建规范化的渠道标识.

        Parameters
        ----------
        value : object
            原始渠道字符串。

        Returns
        -------
        TradeChannel
            去除首尾空白并转为小写的渠道标识。

        Raises
        ------
        ValueError
            原始值不是字符串或字符串为空时抛出。
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError("交易渠道标识必须是非空字符串")
        return super().__new__(cls, value.strip().lower())

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """
        返回开放字符串对应的 Pydantic 核心校验模式.

        Parameters
        ----------
        _source_type : Any
            Pydantic 请求生成模式的源类型。
        _handler : GetCoreSchemaHandler
            Pydantic 模式生成处理器。

        Returns
        -------
        CoreSchema
            接受非空字符串并构造 ``TradeChannel`` 的核心模式。
        """
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )
