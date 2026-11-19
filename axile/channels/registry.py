"""提供进程内交易渠道插件注册表与入口点加载能力."""

from __future__ import annotations

from importlib import metadata
from threading import RLock

from axile.channels.contracts import ChannelPlugin

ENTRY_POINT_GROUP = "axile.channels"


class DuplicateChannelError(ValueError):
    """同一渠道标识被重复注册时抛出."""


class ChannelPluginLoadError(RuntimeError):
    """渠道入口点无法加载或返回非法对象时抛出."""


_registry: dict[str, ChannelPlugin] = {}
_registered_entry_points: dict[str, metadata.EntryPoint] = {}
_lock = RLock()
_builtins_loaded = False
_entry_points_loaded = False


def _register_unchecked(plugin: ChannelPlugin, *, source: str) -> None:
    """在持有注册表锁时登记一个已验证插件."""
    channel = plugin.descriptor.channel
    existing = _registry.get(channel)
    if existing is not None:
        raise DuplicateChannelError(
            f"交易渠道 {channel!r} 重复注册：已有 {existing.create_executor!r}，新来源为 {source}"
        )
    _registry[channel] = plugin


def register_channel(plugin: ChannelPlugin) -> ChannelPlugin:
    """
    向当前进程注册一个交易渠道插件.

    Parameters
    ----------
    plugin : ChannelPlugin
        待注册的完整渠道插件。

    Returns
    -------
    ChannelPlugin
        原样返回插件，便于模块级声明直接赋值。

    Raises
    ------
    TypeError
        参数不是 ``ChannelPlugin`` 时抛出。
    DuplicateChannelError
        渠道标识已经存在时抛出。
    """
    if not isinstance(plugin, ChannelPlugin):
        raise TypeError(f"plugin 必须是 ChannelPlugin，实际为 {type(plugin).__name__}")
    with _lock:
        _register_unchecked(plugin, source="register_channel")
    return plugin


def _load_builtins() -> None:
    """按稳定顺序加载公开包内置渠道."""
    global _builtins_loaded
    if _builtins_loaded:
        return

    from axile.channels.builtins import builtin_channel_plugins

    snapshot = dict(_registry)
    try:
        for plugin in builtin_channel_plugins():
            _register_unchecked(plugin, source="公开内置渠道")
    except Exception:
        _registry.clear()
        _registry.update(snapshot)
        raise
    _builtins_loaded = True


def _consume_entry_point_result(result: object, *, source: str) -> ChannelPlugin | None:
    """消费入口点加载结果并按约定完成注册."""
    if isinstance(result, ChannelPlugin):
        _register_unchecked(result, source=source)
        return result
    if callable(result):
        returned = result()
        if returned is None:
            return None
        if isinstance(returned, ChannelPlugin):
            _register_unchecked(returned, source=source)
            return returned
    raise TypeError("入口点必须导出 ChannelPlugin，或导出返回 ChannelPlugin/None 的无参函数")


def _load_entry_points() -> None:
    """从 ``axile.channels`` 入口点加载当前环境安装的外部渠道."""
    global _entry_points_loaded
    if _entry_points_loaded:
        return

    entry_points = metadata.entry_points().select(group=ENTRY_POINT_GROUP)
    ordered = sorted(entry_points, key=lambda item: (item.name, item.value))
    snapshot = dict(_registry)
    origins: dict[str, metadata.EntryPoint] = {}
    try:
        for entry_point in ordered:
            source = f"entry point {entry_point.name}={entry_point.value}"
            try:
                plugin = _consume_entry_point_result(entry_point.load(), source=source)
                if plugin is not None:
                    origins[plugin.descriptor.channel] = entry_point
            except DuplicateChannelError:
                raise
            except Exception as exc:
                raise ChannelPluginLoadError(f"加载渠道插件失败：{source}：{exc}") from exc
    except Exception:
        _registry.clear()
        _registry.update(snapshot)
        raise
    _registered_entry_points.update(origins)
    _entry_points_loaded = True


def _ensure_loaded() -> None:
    """确保当前进程已加载内置渠道与所有渠道入口点."""
    with _lock:
        _load_builtins()
        _load_entry_points()


def get_channel(channel: str) -> ChannelPlugin:
    """
    返回指定标识对应的渠道插件.

    Parameters
    ----------
    channel : str
        渠道标识。

    Returns
    -------
    ChannelPlugin
        已注册的渠道插件。

    Raises
    ------
    KeyError
        渠道未注册时抛出。
    """
    _ensure_loaded()
    channel_id = str(channel).strip().lower()
    try:
        return _registry[channel_id]
    except KeyError as exc:
        raise KeyError(f"未注册交易渠道: {channel_id}") from exc


def list_channels() -> tuple[ChannelPlugin, ...]:
    """
    按稳定注册顺序返回当前进程的全部渠道插件.

    Returns
    -------
    tuple[ChannelPlugin, ...]
        内置渠道在前，外部入口点按入口点名称排序后追加。
    """
    _ensure_loaded()
    return tuple(_registry.values())


def get_registered_channel_entry_point(channel: str) -> metadata.EntryPoint | None:
    """查找已注册渠道的入口点，供执行环境归属核验。"""
    with _lock:
        return _registered_entry_points.get(channel.strip().lower())


def _reset_registry_for_tests() -> None:
    """清空注册状态，供隔离入口点加载测试使用."""
    global _builtins_loaded, _entry_points_loaded
    with _lock:
        _registry.clear()
        _registered_entry_points.clear()
        _builtins_loaded = False
        _entry_points_loaded = False
