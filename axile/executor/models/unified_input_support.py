"""
统一输入模型的构造与规范化辅助函数.

该模块只负责把原始字典转换为 ``UnifiedStandardInput`` 需要的中间结构，
避免主模型文件同时承载账户定义、归一化规则和实例方法。
"""

from __future__ import annotations

from copy import deepcopy

from pydantic import BaseModel, ValidationError

from axile.channels import get_channel
from axile.common.trade_channel import TradeChannel
from axile.executor.models.unified_input_accounts import (
    BaseAccountConfig,
    _channel_value,
)

TradeRule = dict[str, object]
TradeAlgorithm = dict[str, object]
SymbolAlgorithms = dict[str, TradeAlgorithm]


class AlgorithmParamsError(Exception):
    """
    算法参数无法通过参数模型校验时抛出.

    Notes
    -----
    该异常刻意不继承 ``ValueError``，避免被上层泛化的 ``except ValueError``
    再次静默吞掉。参数非法应当在 planning 阶段就明确失败，从而让执行走
    ``EXECUTION_FAILED`` 审计路径，而不是把非法 ``dict`` 拖进算法内部炸成
    面目全非的 ``AttributeError``。
    """


def _get_default_algorithm() -> dict[str, object]:
    """返回默认算法配置."""
    return {"method": "SINGLE-MAKER", "params": {}}


def _coerce_channel_type(value: object) -> TradeChannel | None:
    """尽可能将输入解析为当前模块使用的渠道枚举."""
    if isinstance(value, TradeChannel):
        return value

    raw_value = getattr(value, "value", value)
    if not isinstance(raw_value, str):
        return None

    try:
        return TradeChannel(raw_value)
    except ValueError:
        return None


def _as_dict(value: object) -> dict[str, object]:
    """尽可能将输入转换为浅层字典."""
    return dict(value) if isinstance(value, dict) else {}


def _as_float_dict(value: object) -> dict[str, float]:
    """从输入中筛选 ``str -> float`` 映射."""
    if not isinstance(value, dict):
        return {}

    result: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(raw, (int, float)):
            result[key] = float(raw)
    return result


def _as_str_list(value: object) -> list[str]:
    """返回只包含字符串元素的列表."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def as_timeout_int(value: object, default: int) -> int:
    """
    将来路不明的总超时取值收敛为整数秒.

    Parameters
    ----------
    value : object
        原始取值；仅接受 ``int`` / ``float``，其余一律视为缺省。
    default : int
        无法识别时返回的默认秒数。

    Returns
    -------
    int
        规范化后的总超时秒数。

    Notes
    -----
    调仓与清仓两条入口的默认额度不同，但对"什么算合法取值"必须给同一个答案：
    各写一份的话，某天有人只在一处放宽对字符串的处理，两条路径就会对同一份输入
    给出不同的额度。
    """
    return int(value) if isinstance(value, (int, float)) else default


def _algorithm_dict(value: object) -> TradeAlgorithm:
    """返回一个可变的算法配置映射."""
    return _as_dict(value)


def _normalize_algorithm_config(
    value: object,
    *,
    fallback_method: str,
    fallback_algorithm: TradeAlgorithm | None = None,
) -> TradeAlgorithm:
    """规范化单个算法配置映射."""
    algorithm = _algorithm_dict(value)
    if fallback_algorithm is not None:
        fallback = _algorithm_dict(fallback_algorithm)
        if not algorithm:
            algorithm = fallback
        else:
            if "method" not in algorithm and "method" in fallback:
                algorithm["method"] = fallback["method"]
            if "params" not in algorithm and "params" in fallback:
                algorithm["params"] = deepcopy(fallback["params"])

    if "method" not in algorithm:
        algorithm["method"] = fallback_method
    if "params" not in algorithm:
        algorithm["params"] = {}
    return algorithm


def _normalize_symbol_algorithms(
    value: object,
    *,
    fallback_method: str,
    fallback_algorithm: TradeAlgorithm,
) -> SymbolAlgorithms:
    """返回规范化后的按品种算法配置映射."""
    if not isinstance(value, dict):
        return {}

    result: SymbolAlgorithms = {}
    for symbol, config in value.items():
        if not isinstance(symbol, str):
            continue
        result[symbol] = _normalize_algorithm_config(
            config,
            fallback_method=fallback_method,
            fallback_algorithm=fallback_algorithm,
        )
    return result


def _resolve_input_channel(
    data: dict[str, object],
    explicit_channel: object | None,
) -> TradeChannel:
    """解析显式提供的交易渠道类型，禁止根据凭据字段猜测."""
    coerced_explicit_channel = _coerce_channel_type(explicit_channel)
    if coerced_explicit_channel is not None:
        data["channel_type"] = coerced_explicit_channel
        return coerced_explicit_channel

    raw_channel = _coerce_channel_type(data.get("channel_type"))
    if raw_channel is not None:
        data["channel_type"] = raw_channel
        return raw_channel

    raise ValueError("必须明确指定 channel_type；系统不会根据账户凭据字段推断渠道")


def _build_account_config(
    channel: TradeChannel,
    account_config_data: dict[str, object] | BaseAccountConfig,
) -> BaseAccountConfig:
    """使用渠道插件声明的模型构造账户配置."""
    plugin = get_channel(channel)
    if isinstance(account_config_data, plugin.account_config_model):
        return account_config_data

    if isinstance(account_config_data, BaseAccountConfig):
        normalized_account_config = dict(account_config_data.model_dump(mode="python"))
    else:
        normalized_account_config = account_config_data.copy()
    normalized_account_config["channel_type"] = channel
    return plugin.account_config_model.model_validate(normalized_account_config)


def _get_default_algorithm_method(channel: TradeChannel) -> str:
    """返回指定渠道的默认算法名称."""
    return get_channel(channel).descriptor.defaults.trade_algorithm.method


def _get_channel_default_algorithm(channel: TradeChannel) -> TradeAlgorithm:
    """返回指定渠道的默认调仓算法配置副本."""
    default = get_channel(channel).descriptor.defaults.trade_algorithm
    return dict(default.model_dump(mode="python"))


def _normalize_trade_rules_dict(value: object) -> dict[str, TradeRule]:
    """返回规范化后的交易规则映射."""
    return {symbol: _as_dict(rule) for symbol, rule in _as_dict(value).items() if isinstance(symbol, str)}


def _collect_unified_input_extra(data: dict[str, object]) -> dict[str, object]:
    """汇总标准输入中的额外字段."""
    reserved_fields = {
        "channel_type",
        "account_config",
        "curr_target",
        "last_target",
        "algorithm",
        "symbol_algorithms",
        "trade_rules",
        "futures_close_policy",
        "forbidden_symbols",
        "risk_symbols",
        "feishu_key",
        "execution_notification_code",
        "feishu_account",
        "execution_timeout",
        "extra",
    }
    extra = _as_dict(data.get("extra", {}))
    extra.update({key: value for key, value in data.items() if key not in reserved_fields})
    return extra


def _serialize_account_config(account_config: BaseAccountConfig) -> dict[str, object]:
    """返回账户配置的标准序列化字典."""
    return dict(account_config.model_dump(mode="json", exclude_none=True))


def _serialize_extra(
    extra: dict[str, object],
    *,
    channel_type: TradeChannel,
) -> dict[str, object]:
    """移除可由主字段推导出的冗余 extra 内容."""
    serialized = dict(extra)
    if serialized.get("channel_type") == _channel_value(channel_type):
        serialized.pop("channel_type", None)
    return serialized


def _parse_algorithm_params_with_metadata(
    algorithm: TradeAlgorithm,
    algorithm_name: str,
) -> TradeAlgorithm:
    """
    在可能的情况下将算法参数解析为参数模型实例.

    Parameters
    ----------
    algorithm : TradeAlgorithm
        待解析的算法配置映射，通常含 ``method`` 与 ``params``。
    algorithm_name : str
        算法名称，用于查找对应的参数模型元数据。

    Returns
    -------
    TradeAlgorithm
        解析成功时 ``params`` 为模型实例；无参数模型可用时原样返回。

    Raises
    ------
    AlgorithmParamsError
        当存在参数模型但传入的 ``params`` 无法通过校验时抛出。

    Notes
    -----
    「找不到算法元数据 / 该算法无参数模型」属于宽松场景，直接原样返回；
    只有「有参数模型却校验失败」才视为用户配置错误并抛出异常。
    """
    algorithm_config = _algorithm_dict(algorithm)
    params_raw = algorithm_config.get("params")
    if params_raw is not None and isinstance(params_raw, BaseModel):
        return algorithm_config

    try:
        from axile.executor.algorithms.core.base import get_algorithm_metadata

        meta = get_algorithm_metadata(algorithm_name)
        params_class = meta.params_class
    except (ImportError, ValueError):
        return algorithm_config

    if params_class is None:
        return algorithm_config

    if not isinstance(params_raw, dict):
        params_raw = {}
    params_dict: dict[str, object] = params_raw
    try:
        algorithm_config["params"] = params_class(**params_dict)
    except (ValidationError, TypeError) as exc:
        raise AlgorithmParamsError(f"算法 {algorithm_name!r} 的参数非法: {exc}") from exc
    return algorithm_config
