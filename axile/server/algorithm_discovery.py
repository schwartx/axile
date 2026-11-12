"""通过单线程 Rust 扫描当前层算法声明，预览时不执行脚本。"""

import os
from pathlib import Path
from typing import Any

from axile.server._native import _core


def discover_directory(directory: Path) -> dict[str, Any]:
    """只读扫描指定层级，不导入 Python 模块或寻找项目配置。"""
    path = directory.expanduser().resolve(strict=True)
    if not path.is_dir():
        raise NotADirectoryError(f"路径不是目录：{path}")
    preview = _core.scan_algorithm_directory(str(path))
    if os.name == "nt":
        # Rust canonicalize 使用 Windows 扩展路径，界面继续展示普通盘符路径。
        preview["path"] = str(path)
        preview["parent"] = str(path.parent) if path.parent != path else None
        for entry in preview["entries"]:
            entry["path"] = str(path / entry["name"])
    return preview


def require_algorithm_directory(directory: Path) -> dict[str, Any]:
    """保存或加载前重新检查，拒绝空目录及不完整扫描结果。"""
    preview = discover_directory(directory)
    if preview["discovery"]["issues"]:
        first = preview["discovery"]["issues"][0]
        raise ValueError(f"{directory} / {first['file']}：{first['message']}")
    if not preview["discovery"]["can_use"]:
        raise ValueError(f"{directory} 当前层没有发现算法，请进入存放算法脚本的子目录")
    return preview
