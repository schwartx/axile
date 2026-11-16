"""渠道调度配置校验；具体结构由插件声明。"""

from typing import Any

from axile.channels.registry import get_channel


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
