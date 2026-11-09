"""账户配置在 API 和持久化模型之间共享的校验。"""

import math
from typing import Any, Dict, Optional

# 账户级杠杆的通用粗粒度上限；更严格的渠道限制由渠道插件和执行器兜底。
_MAX_LEVERAGE = 125.0


def _validate_weight_precision(value: float | None) -> float | None:
    """校验权重精度为正的 10 整数次幂。

    创建、更新及持久化账户模型共用这一条约束，避免 API 输入校验和
    执行期的除法前提发生漂移。
    """
    if value is None:
        return value
    if value <= 0:
        raise ValueError("weight_precision 必须是正数")
    if not math.log10(value).is_integer():
        raise ValueError("weight_precision 必须是10的负整数次幂, 如 1, 0.1, 0.01, 0.001")
    return value


def _validate_leverage(value: Optional[float]) -> Optional[float]:
    """
    校验杠杆倍数取值范围.

    Parameters
    ----------
    value : float | None
        杠杆倍数；``None``（未设置）与 ``0``（该方向不启用，如「做空杠杆 0 = 只做多」）均放行。

    Returns
    -------
    float | None
        原样返回入参，校验通过。

    Raises
    ------
    ValueError
        杠杆为负或超过 ``_MAX_LEVERAGE`` 时抛出，经 Pydantic 包装为 422。
    """
    if value is None:
        return value
    if value < 0:
        raise ValueError("杠杆必须是非负数")
    if value > _MAX_LEVERAGE:
        raise ValueError(f"杠杆不得超过 {_MAX_LEVERAGE:.0f} 倍")
    return value


def _validate_cron_expr(value: Optional[str]) -> Optional[str]:
    """
    校验定时表达式可被解析.

    Parameters
    ----------
    value : str | None
        crontab 表达式（``|`` 可分隔多个）；``None`` / 空白表示「仅手动触发」，放行。

    Returns
    -------
    str | None
        原样返回入参，校验通过。

    Raises
    ------
    ValueError
        非空但无法解析为合法 crontab 时抛出，经 Pydantic 包装为 422，
        避免非法表达式落库后在「启动」时把调度器打挂。
    """
    from axile.server.cron import is_blank_cron_expr, parse_cron_expr

    if value is None or is_blank_cron_expr(value):
        return value
    parse_cron_expr(value)  # 无法解析会抛 ValueError
    return value


def _validate_algorithm_config(config: Optional[Dict[str, Any]], field_label: str) -> Optional[Dict[str, Any]]:
    """
    按算法注册表校验 ``{"method", "params"}`` 配置的参数是否越界.

    仅当 ``method`` 命中注册表且声明了 ``params_class`` 时才校验 ``params``；未知算法
    留给执行期处理（避免误伤插件/测试用的非注册算法）。

    Parameters
    ----------
    config : dict | None
        算法配置，形如 ``{"method": ..., "params": {...}}``；``None`` 直接放行。
    field_label : str
        字段中文名，用于错误信息（如「下单算法」「清仓算法」）。

    Returns
    -------
    dict | None
        原样返回入参，校验通过。

    Raises
    ------
    ValueError
        ``config`` 非对象、缺 ``method``，或 ``params`` 不满足算法参数模型约束（越界等）
        时抛出，经 Pydantic 包装为 422。
    """
    if config is None:
        return config
    if not isinstance(config, dict):
        raise ValueError(f"{field_label}必须是对象")
    method = config.get("method")
    if not method:
        raise ValueError(f"{field_label}缺少 method")
    params = config.get("params") or {}

    from pydantic import ValidationError

    from axile.executor.algorithms.core.base import get_algorithm_metadata

    try:
        meta = get_algorithm_metadata(str(method))
    except ValueError:
        # 未知算法：边界不拦，交由执行期校验，避免误伤未注册的插件/测试算法。
        return config
    if meta.params_class is not None:
        try:
            meta.params_class.model_validate(params)
        except ValidationError as exc:
            errors = exc.errors()
            detail = errors[0].get("msg", "") if errors else str(exc)
            raise ValueError(f"{field_label}参数不合法：{detail}") from exc
    return config


def _check_algorithm_channel_compat(
    config: Optional[Dict[str, Any]],
    channel: Optional[str],
    field_label: str,
) -> Optional[Dict[str, Any]]:
    """
    校验算法的订单参数模型与渠道声明是否兼容.

    Parameters
    ----------
    config : dict | None
        算法配置，形如 ``{"method": ..., "params": {...}}``；``None`` 直接放行。
    channel : str | None
        生效后的渠道标识；``None`` 直接放行。
    field_label : str
        字段中文名，用于错误信息（如「下单算法」「清仓算法」）。

    Returns
    -------
    dict | None
        原样返回入参，校验通过。

    Raises
    ------
    ValueError
        算法声明了具体参数模型而渠道模型不在其适配集合内时抛出，
        经 Pydantic/路由包装为 422。

    Notes
    -----
    与 ``channels`` 限制正交：channels 约束"能不能跑",本校验约束"跑的时候
    下单参数语义能不能被渠道满足"。未知算法、未注册渠道、渠道未声明模型
    （UNKNOWN）一律放行,交由执行期校验/兜底,避免误伤插件。
    """
    if config is None or channel is None:
        return config
    method = config.get("method") if isinstance(config, dict) else None
    if not method:
        return config

    from axile.channels import get_channel
    from axile.common.order_param_model import OrderParamModel
    from axile.executor.algorithms.core.base import get_algorithm_metadata

    try:
        meta = get_algorithm_metadata(str(method))
    except ValueError:
        return config
    if meta.order_param_models is None:
        return config
    try:
        channel_model = get_channel(str(channel)).order_param_model
    except KeyError:
        return config
    if channel_model is OrderParamModel.UNKNOWN:
        return config
    if str(channel_model) not in meta.order_param_models:
        supported = sorted(meta.order_param_models)
        raise ValueError(
            f"{field_label}{method} 未适配渠道 {channel} 的订单参数模型({channel_model})，该算法仅适配: {supported}"
        )
    return config
