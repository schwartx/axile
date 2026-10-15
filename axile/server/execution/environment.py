"""执行当时的软件环境快照，不保存路径、配置值或代码原文。"""

from __future__ import annotations

import hashlib
import inspect
import platform
from datetime import UTC
from importlib.metadata import PackageNotFoundError, packages_distributions, version
from typing import Any

from axile.executor.algorithms.utils.clock import clock_now


def package_version(name: str) -> str | None:
    """已安装分发包的版本；源码环境无法识别时保留未知。"""
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def execution_environment(executor: object, channel: str, algorithm: str) -> dict[str, Any]:
    """冻结执行器实现所属包及源码摘要，导出时不冒充历史版本。"""
    executor_type = type(executor)
    module = executor_type.__module__
    distributions = packages_distributions().get(module.split(".")[0], [])
    packages = {name: package_version(name) for name in distributions}
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
