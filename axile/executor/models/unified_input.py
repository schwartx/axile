"""
统一标准输入数据模型.

主文件只保留 ``UnifiedStandardInput`` 模型与对外导出；账户配置定义与
字典归一化辅助函数分别下沉到专用模块，减少阅读主流程时的上下文跳转。
"""

from collections.abc import Mapping
from typing import override

from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.common.trade_channel import TradeChannel
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.models.unified_input_accounts import (
    AccountConfig,
    BaseAccountConfig,
    CTPAccountConfig,
    GMAccountConfig,
    TQAccountConfig,
    _channel_value,
)
from axile.executor.models.unified_input_support import (
    SymbolAlgorithms,
    TradeAlgorithm,
    TradeRule,
    _algorithm_dict,
    _as_dict,
    _as_float_dict,
    _as_str_list,
    _build_account_config,
    _collect_unified_input_extra,
    _get_channel_default_algorithm,
    _get_default_algorithm,
    _get_default_algorithm_method,
    _normalize_algorithm_config,
    _normalize_symbol_algorithms,
    _normalize_trade_rules_dict,
    _parse_algorithm_params_with_metadata,
    _resolve_input_channel,
    _serialize_account_config,
    _serialize_extra,
    as_timeout_int,
)

__all__ = [
    "DEFAULT_EXECUTION_TIMEOUT_SECONDS",
    "AccountConfig",
    "BaseAccountConfig",
    "CTPAccountConfig",
    "GMAccountConfig",
    "TQAccountConfig",
    "SymbolAlgorithms",
    "TradeAlgorithm",
    "TradeRule",
    "UnifiedStandardInput",
]

DEFAULT_EXECUTION_TIMEOUT_SECONDS = 180
"""执行层总超时的默认秒数。.

Notes
-----
这是「一次执行最多跑多久」的兜底额度，不是算法等成交的时长（那是各算法参数里的
``max_wait_seconds``）。取值需要明显宽于正常一次调仓的耗时，否则会把合法执行截断：
串行下单的渠道，一次执行的总时长约为 ``品种数 × max_wait_seconds``。
"""


# ==================== 主输入模型 ====================


class UnifiedStandardInput(BaseModel):
    """
    统一标准输入数据模型.

    Attributes
    ----------
    channel_type : TradeChannel
        交易渠道类型。
    account_config : AccountConfig
        账户配置对象。
    curr_target : dict[str, float]
        当前目标仓位映射。
    algorithm : TradeAlgorithm
        默认算法配置。
    symbol_algorithms : SymbolAlgorithms
        按品种覆盖的算法配置。
    trade_rules : dict[str, TradeRule]
        各品种交易规则。
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)  # 允许额外的字段，并校验运行期赋值

    # === 核心必填字段 ===
    channel_type: TradeChannel = Field(..., description="交易渠道类型")
    account_config: BaseAccountConfig = Field(..., description="账户配置")
    curr_target: dict[str, float] = Field(..., description="当前目标持仓权重")

    # === 可选字段 ===
    last_target: dict[str, float] = Field(default={}, description="上次目标持仓权重")

    # 算法配置
    algorithm: TradeAlgorithm = Field(
        default_factory=_get_default_algorithm,
        description="交易算法配置",
    )
    symbol_algorithms: SymbolAlgorithms = Field(
        default={},
        description="按品种覆盖的交易算法配置",
    )

    # 交易规则
    trade_rules: dict[str, TradeRule] = Field(default={}, description="各品种的交易规则")
    futures_close_policy: FuturesClosePolicy = Field(default_factory=FuturesClosePolicy)

    # 风控配置
    forbidden_symbols: list[str] = Field(default=[], description="禁止交易的品种列表")
    risk_symbols: list[str] = Field(default=[], description="风险品种列表")

    # 通知配置
    feishu_key: str | None = Field(None, description="飞书通知key")
    execution_notification_code: str | None = Field(default=None, description="执行通知函数源码；空值不通知")
    feishu_account: dict[str, object] = Field(default={}, description="通知可使用的脱敏账户元数据")

    # 执行配置
    execution_timeout: int = Field(
        default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        description="执行层总超时时间（秒）；<=0 表示不启用。与算法级 max_wait_seconds 不是一个层次",
    )

    # 渠道特定参数
    extra: dict[str, object] = Field(default={}, description="渠道特定的额外参数")

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        channel = _channel_value(self.channel_type)
        algorithm_method = _algorithm_dict(self.algorithm).get("method", "Unknown")
        return f"UnifiedStandardInput(channel={channel}, symbols={len(self.curr_target)}, algorithm={algorithm_method})"

    @override
    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> "UnifiedStandardInput":
        """
        复制当前输入对象，并对更新后的结果重新执行完整校验.

        Parameters
        ----------
        update : Mapping[str, object] | None, default=None
            需要覆盖到副本上的字段。
        deep : bool, default=False
            是否对嵌套对象执行深拷贝。

        Returns
        -------
        UnifiedStandardInput
            经过完整校验后的副本对象。

        Raises
        ------
        ValidationError
            当更新后的字段不满足模型约束时抛出。
        ValueError
            当字段组合不一致时抛出。
        """
        copied = super().model_copy(update=update, deep=deep)
        payload = dict(copied.__dict__)
        if copied.model_extra:
            payload.update(copied.model_extra)
        return type(self).model_validate(payload)

    @model_validator(mode="before")
    @classmethod
    def validate_channel_account_config(cls, value: object) -> object:
        """
        使用显式渠道对应的插件模型校验账户配置.

        Parameters
        ----------
        value : object
            原始模型载荷。

        Returns
        -------
        object
            已将账户配置转换为插件模型的载荷。
        """
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        channel = _resolve_input_channel(payload, None)
        plugin_defaults = _get_channel_default_algorithm(channel)
        if not payload.get("algorithm"):
            payload["algorithm"] = plugin_defaults
        raw_config = payload.get("account_config", {})
        if isinstance(raw_config, BaseAccountConfig):
            account_config_data: dict[str, object] | BaseAccountConfig = raw_config
        else:
            account_config_data = _as_dict(raw_config)
        payload["account_config"] = _build_account_config(channel, account_config_data)
        return payload

    # ==================== 验证器 ====================

    @model_validator(mode="after")
    def validate_consistency(self) -> "UnifiedStandardInput":
        """
        校验输入对象的一致性并补齐派生字段.

        Returns
        -------
        UnifiedStandardInput
            校验完成后的当前输入对象。
        """
        self.futures_close_policy.validate_channel(self.channel_type)
        channel_value = _channel_value(self.channel_type)

        # 验证账户配置与渠道类型匹配
        if hasattr(self.account_config, "channel_type"):
            if self.account_config.channel_type != self.channel_type:
                raise ValueError(
                    f"账户配置渠道类型({self.account_config.channel_type}) 与指定渠道类型({self.channel_type})不匹配"
                )

        # 让 extra 中的 channel_type 始终与主字段保持同步。
        extra = _as_dict(self.extra)
        if extra.get("channel_type") != channel_value:
            extra["channel_type"] = channel_value
            object.__setattr__(self, "extra", extra)

        if self.symbol_algorithms:
            normalized: SymbolAlgorithms = {}
            for symbol, config in self.symbol_algorithms.items():
                normalized[symbol] = _normalize_algorithm_config(
                    config,
                    fallback_method=str(self.algorithm.get("method", "SINGLE-MAKER")),
                    fallback_algorithm=self.algorithm,
                )
            if normalized != self.symbol_algorithms:
                object.__setattr__(self, "symbol_algorithms", normalized)

        return self

    @classmethod
    def from_dict(
        cls,
        input_dict: dict[str, object],
        channel_type: TradeChannel | None = None,
        **_kwargs: object,
    ) -> "UnifiedStandardInput":
        """
        从字典格式创建统一输入模型.

        Parameters
        ----------
        input_dict : dict[str, object]
            原始标准输入字典。
        channel_type : TradeChannel | None, default=None
            显式指定的交易渠道类型；为空时必须由 ``input_dict`` 提供
            ``channel_type``，不会根据账户凭据字段猜测。
        **_kwargs : object
            预留的额外参数。

        Returns
        -------
        UnifiedStandardInput
            规范化后的统一输入对象。

        Raises
        ------
        ValueError
            当未明确提供渠道类型或渠道尚未注册时抛出。
        """
        # 复制字典以避免修改原始数据
        data = input_dict.copy()
        channel = _resolve_input_channel(data, channel_type)
        account_config = _build_account_config(channel, _as_dict(data.get("account_config", {})))
        default_algorithm_method = _get_default_algorithm_method(channel)
        default_algorithm = _get_channel_default_algorithm(channel)
        algorithm = _normalize_algorithm_config(
            data.get("algorithm", {}),
            fallback_method=default_algorithm_method,
            fallback_algorithm=default_algorithm,
        )
        symbol_algorithms = _normalize_symbol_algorithms(
            data.get("symbol_algorithms", {}),
            fallback_method=default_algorithm_method,
            fallback_algorithm=algorithm,
        )
        trade_rules = _normalize_trade_rules_dict(data.get("trade_rules", {}))

        # 创建实例
        feishu_key_obj = data.get("feishu_key")
        notification_code_obj = data.get("execution_notification_code")
        execution_timeout_obj = data.get("execution_timeout", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
        extra = _collect_unified_input_extra(data)

        return cls(
            channel_type=channel,
            account_config=account_config,
            curr_target=_as_float_dict(data.get("curr_target", {})),
            last_target=_as_float_dict(data.get("last_target", {})),
            algorithm=algorithm,
            symbol_algorithms=symbol_algorithms,
            trade_rules=trade_rules,
            futures_close_policy=FuturesClosePolicy.model_validate(data.get("futures_close_policy") or {}),
            forbidden_symbols=_as_str_list(data.get("forbidden_symbols", [])),
            risk_symbols=_as_str_list(data.get("risk_symbols", [])),
            feishu_key=feishu_key_obj if isinstance(feishu_key_obj, str) else None,
            execution_notification_code=notification_code_obj if isinstance(notification_code_obj, str) else None,
            feishu_account=_as_dict(data.get("feishu_account", {})),
            execution_timeout=as_timeout_int(execution_timeout_obj, DEFAULT_EXECUTION_TIMEOUT_SECONDS),
            extra=extra,
        )

    # ==================== 便捷方法 ====================

    def to_dict(self) -> dict[str, object]:
        """
        转换为标准输入字典.

        Returns
        -------
        dict[str, object]
            标准输入字典。
        """
        result = {
            "channel_type": _channel_value(self.channel_type),
            "account_config": _serialize_account_config(self.account_config),
            "curr_target": self.curr_target,
            "last_target": self.last_target,
            "algorithm": self.algorithm,
            "symbol_algorithms": self.symbol_algorithms,
            "trade_rules": self.trade_rules,
            "futures_close_policy": self.futures_close_policy.model_dump(),
            "forbidden_symbols": self.forbidden_symbols,
            "risk_symbols": self.risk_symbols,
            "execution_timeout": self.execution_timeout,
            "feishu_account": self.feishu_account,
        }

        # 添加可选字段
        if self.feishu_key:
            result = {**result, "feishu_key": self.feishu_key}
        if self.execution_notification_code:
            result["execution_notification_code"] = self.execution_notification_code

        # 添加额外字段
        if self.extra:
            extra = _serialize_extra(self.extra, channel_type=self.channel_type)
            if extra:
                result["extra"] = extra

        return result

    def parse_algorithm_params(self, algorithm_name: str) -> None:
        """
        解析默认算法配置中的参数模型.

        Parameters
        ----------
        algorithm_name : str
            算法名称，例如 ``"SINGLE-MAKER"``。

        Notes
        -----
        若 ``algorithm["params"]`` 已经是模型实例，则保持原样。
        """
        self.algorithm = self._parse_algorithm_config_params(self.algorithm, algorithm_name)

    def get_symbol_algorithm(self, symbol: str) -> TradeAlgorithm:
        """
        返回指定品种的最终算法配置.

        Parameters
        ----------
        symbol : str
            需要解析的品种代码。

        Returns
        -------
        TradeAlgorithm
            已融合默认算法与品种覆盖配置后的算法映射。
        """
        if symbol in self.symbol_algorithms:
            return _normalize_algorithm_config(
                self.symbol_algorithms[symbol],
                fallback_method=str(self.algorithm.get("method", "SINGLE-MAKER")),
                fallback_algorithm=self.algorithm,
            )
        return _normalize_algorithm_config(
            self.algorithm,
            fallback_method=str(self.algorithm.get("method", "SINGLE-MAKER")),
        )

    def get_resolved_symbol_algorithm(self, symbol: str) -> TradeAlgorithm:
        """
        返回已完成参数模型解析的品种算法配置.

        Parameters
        ----------
        symbol : str
            需要解析的品种代码。

        Returns
        -------
        TradeAlgorithm
            已解析参数模型的品种算法配置。
        """
        algorithm = self.get_symbol_algorithm(symbol)
        algorithm_name = str(algorithm.get("method", self.algorithm.get("method", "SINGLE-MAKER")))
        return self._parse_algorithm_config_params(algorithm, algorithm_name)

    def _parse_algorithm_config_params(
        self,
        algorithm: TradeAlgorithm,
        algorithm_name: str,
    ) -> TradeAlgorithm:
        """
        在可能的情况下解析算法配置中的参数模型.

        Parameters
        ----------
        algorithm : TradeAlgorithm
            待解析的算法配置映射。
        algorithm_name : str
            算法名称。

        Returns
        -------
        TradeAlgorithm
            若解析成功则返回带模型参数的配置，否则返回原配置。
        """
        return _parse_algorithm_params_with_metadata(algorithm, algorithm_name)
