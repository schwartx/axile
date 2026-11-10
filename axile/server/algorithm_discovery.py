"""通过单线程 Rust 扫描当前层算法声明，预览时不执行脚本。"""

import os
from pathlib import Path

from pydantic import BaseModel

from axile.server._native import _core


class AlgorithmDeclaration(BaseModel):
    """脚本中直接声明的算法及其来源。"""

    name: str
    function: str
    file: str


class ScanIssue(BaseModel):
    """阻止选择该目录的脚本读取或声明问题。"""

    file: str
    message: str


class AlgorithmDiscovery(BaseModel):
    """当前目录的静态预览，不代表运行时已注册。"""

    files: int
    algorithms: list[AlgorithmDeclaration]
    issues: list[ScanIssue]
    can_use: bool


class DirectoryEntry(BaseModel):
    """可以继续进入的子目录，不扫描其文件。"""

    name: str
    path: str


class AlgorithmDirectory(BaseModel):
    """一次目录枚举与当前层算法扫描的结果。"""

    path: str
    parent: str | None
    entries: list[DirectoryEntry]
    discovery: AlgorithmDiscovery


def discover_directory(directory: Path) -> AlgorithmDirectory:
    """只读扫描指定层级，不导入 Python 模块或寻找项目配置。"""
    path = directory.expanduser().resolve(strict=True)
    if not path.is_dir():
        raise NotADirectoryError(f"路径不是目录：{path}")
    preview = AlgorithmDirectory.model_validate(_core.scan_algorithm_directory(str(path)))
    if os.name == "nt":
        # Rust canonicalize 使用 Windows 扩展路径，界面继续展示普通盘符路径。
        preview.path = str(path)
        preview.parent = str(path.parent) if path.parent != path else None
        for entry in preview.entries:
            entry.path = str(path / entry.name)
    return preview


def require_algorithm_directory(directory: Path) -> AlgorithmDirectory:
    """保存或加载前重新检查，拒绝空目录及不完整扫描结果。"""
    preview = discover_directory(directory)
    if preview.discovery.issues:
        first = preview.discovery.issues[0]
        raise ValueError(f"{directory} / {first.file}：{first.message}")
    if not preview.discovery.can_use:
        raise ValueError(f"{directory} 当前层没有发现算法，请进入存放算法脚本的子目录")
    return preview
