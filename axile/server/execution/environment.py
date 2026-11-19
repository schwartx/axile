"""执行当时的软件环境快照，不保存路径、配置值或代码原文。"""

from __future__ import annotations

import hashlib
import inspect
import json
import platform
import sys
from datetime import UTC
from importlib.metadata import Distribution, PackageNotFoundError, packages_distributions, version
from pathlib import Path
from typing import Any

from axile.channels.registry import get_registered_channel_entry_point
from axile.executor.algorithms.utils.clock import clock_now


def package_version(name: str) -> str | None:
    """已安装分发包的版本；源码环境无法识别时保留未知。"""
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _distribution_contains(distribution: Distribution, filename: str) -> bool:
    """核对 wheel 文件记录或可编辑安装的源码目录。"""
    origin = Path(filename).resolve()
    for record in distribution.files or ():
        if record.name != origin.name:
            continue
        installed_path = distribution.locate_file(record)
        if isinstance(installed_path, Path) and installed_path.resolve() == origin:
            return True

    manifest = distribution.read_text("direct_url.json")
    if not manifest:
        return False
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    location = json.loads(manifest)
    url = urlsplit(location.get("url", ""))
    if not location.get("dir_info", {}).get("editable") or url.scheme != "file" or url.netloc:
        return False
    project = Path(url2pathname(url.path))
    return project.is_absolute() and origin.is_relative_to(project.resolve())


def _registered_executor_package(channel: str, module: str) -> tuple[str, str] | None:
    """发行包和执行器文件归属一致时，返回已安装版本。"""
    entry_point = get_registered_channel_entry_point(channel)
    if entry_point is None:
        return None
    root = module.partition(".")[0]
    if entry_point.module.partition(".")[0] != root or getattr(sys.modules.get(root), "__file__", None) is None:
        return None
    distribution = entry_point.dist
    filename = getattr(sys.modules.get(module), "__file__", None)
    if distribution is None or not isinstance(filename, str):
        return None
    try:
        if _distribution_contains(distribution, filename):
            installed = distribution.metadata
            name, installed_version = installed.get("Name"), installed.get("Version")
            if name and installed_version:
                return name, installed_version
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        # 安装元数据可能不完整。让调用方按模块执行原有查找。
        return None
    return None


def execution_environment(executor: object, channel: str, algorithm: str) -> dict[str, Any]:
    """冻结执行器实现所属包及源码摘要，导出时不冒充历史版本。"""
    executor_type = type(executor)
    module = executor_type.__module__
    root = module.partition(".")[0]
    packages: dict[str, str | None] = {}
    if root != "axile":
        registered = _registered_executor_package(channel, module)
        if registered is not None:
            name, installed_version = registered
            packages[name] = installed_version
        else:
            packages = {name: package_version(name) for name in packages_distributions().get(root, [])}
    packages["axile"] = package_version("axile")
    try:
        digest = hashlib.sha256(inspect.getsource(executor_type).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    return {
        "captured_at": clock_now(tz=UTC).isoformat(),
        "python_version": platform.python_version(),
        "channel": channel,
        "algorithm": algorithm,
        "executor_module": module,
        "executor_class": executor_type.__qualname__,
        "implementation_sha256": digest,
        "packages": packages,
    }
