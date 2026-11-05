"""渠道调度配置的校验与旧字段兼容；具体结构由插件声明。"""

from typing import Any

from pydantic import BaseModel

from axile.channels.registry import get_channel, list_channels


def legacy_schedule_fields() -> dict[str, str]:
    """返回旧字段到所属渠道的映射。"""
    return {
        plugin.legacy_schedule_field: plugin.descriptor.channel
        for plugin in list_channels()
        if plugin.legacy_schedule_field is not None
    }


def normalize_schedule_options(
    values: dict[str, Any], *, current: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """接收旧 JSON 或扁平补丁，统一存为按渠道分组的配置。"""
    result = dict(values)
    raw_options = result.get("channel_options", current or {})
    if not isinstance(raw_options, dict):
        raise ValueError("channel_options 必须是对象")
    options = dict(raw_options)
    for field, channel in legacy_schedule_fields().items():
        if field not in result:
            continue
        value = result.pop(field)
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if "channel_options" in values and channel in options and options[channel] != value:
            raise ValueError(f"{field} 与 channel_options 配置冲突")
        if value is None:
            options.pop(channel, None)
        else:
            options[channel] = value
    result["channel_options"] = options
    return result


def validate_schedule_options(options: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """通过注册插件校验结构，拒绝未知渠道和未声明的配置。"""
    validated = {}
    for channel, value in options.items():
        try:
            plugin = get_channel(channel)
            model = plugin.schedule_settings_model
        except KeyError as exc:
            raise ValueError(f"未注册交易渠道: {channel}") from exc
        if channel != plugin.descriptor.channel:
            raise ValueError(f"渠道配置须使用规范标识: {plugin.descriptor.channel}")
        if model is None:
            raise ValueError(f"渠道 {channel} 不支持专属调度配置")
        validated[channel] = model.model_validate(value).model_dump(mode="json")
    return validated


def legacy_schedule_values(options: dict[str, dict[str, Any]]) -> dict[str, BaseModel | None]:
    """在旧账户 API 与执行快照边界恢复渠道模型。"""
    values: dict[str, BaseModel | None] = {}
    for plugin in list_channels():
        field = plugin.legacy_schedule_field
        model = plugin.schedule_settings_model
        if field is not None and model is not None:
            channel = plugin.descriptor.channel
            values[field] = model.model_validate(options[channel]) if channel in options else None
    return values
