"""交易算法注册表查询路由."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from axile.executor.algorithms.core.base import AlgorithmMetadata, list_algorithms_metadata
from axile.server.algorithm_discovery import discover_directory

router = APIRouter(prefix="/algorithms", tags=["algorithms"])


@router.get("/discover", response_model=None)
def discover_algorithms(path: str | None = None) -> dict[str, Any]:
    """返回当前层目录和算法声明，浏览期间不加载或执行用户脚本。"""
    try:
        return discover_directory(Path.cwd() if path is None else Path(path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="目录不存在") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="路径不是目录") from exc
    except OSError as exc:
        raise HTTPException(status_code=403, detail="无法读取该目录") from exc


# 内置算法所在包前缀；据此区分内置与用户自定义算法。
_BUILTIN_MODULE_PREFIX = "axile.executor.algorithms.defaults"


def _to_public(meta: AlgorithmMetadata) -> dict[str, Any]:
    """
    将注册表元数据转换为公开字段映射.

    Parameters
    ----------
    meta : AlgorithmMetadata
        注册表内的算法元数据。

    Returns
    -------
    dict[str, Any]
        面向前端的算法元数据。
    """
    module = getattr(meta.func, "__module__", "") or ""
    return {
        "name": meta.name,
        "label": meta.label,
        "description": meta.description,
        "default_params": meta.default_params,
        "params_schema": meta.params_schema,
        "channels": None if meta.channels is None else sorted(meta.channels),
        "slots": None if meta.slots is None else sorted(meta.slots),
        "builtin": module.startswith(_BUILTIN_MODULE_PREFIX),
    }


@router.get("", response_model=None)
def list_registered_algorithms() -> list[dict[str, Any]]:
    """
    返回全部已注册算法的元数据.

    Returns
    -------
    list[dict[str, Any]]
        按名称排序的算法元数据列表，含内置与用户自定义算法。

    Notes
    -----
    渠道与槽位过滤交由前端按当前账户上下文完成；本接口只提供结构化事实。
    """
    algorithms = [_to_public(meta) for meta in list_algorithms_metadata()]
    algorithms.sort(key=lambda a: a["name"])
    return algorithms


class LoadAlgorithmFile(BaseModel):
    """用户明确选择加载的算法文件。"""

    directory: str
    file: str


@router.post("/load", response_model=None)
def load_algorithm_file(request: LoadAlgorithmFile) -> list[dict[str, Any]]:
    """显式加载当前层声明文件，返回其实际注册结果。"""
    import sys

    from axile.executor.algorithms.core.loader import load_algorithm_modules
    from axile.server.app import _directory_modules

    try:
        preview = discover_directory(Path(request.directory))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法读取目录：{exc}") from exc
    declarations = [item for item in preview["discovery"]["algorithms"] if item["file"] == request.file]
    if not declarations:
        raise HTTPException(status_code=400, detail="该文件未发现算法注册入口")
    preview["discovery"]["algorithms"] = declarations
    preview["discovery"]["issues"] = [
        issue for issue in preview["discovery"]["issues"] if issue["file"] == request.file
    ]
    preview["discovery"]["can_use"] = not preview["discovery"]["issues"]
    try:
        modules = _directory_modules(preview)
        list_algorithms_metadata()
        load_algorithm_modules(modules)
        expected = (Path(preview["path"]) / request.file).resolve()
        result = []
        for meta in list_algorithms_metadata():
            module = sys.modules.get(getattr(meta.func, "__module__", ""))
            origin = getattr(module, "__file__", None)
            if origin and Path(origin).resolve() == expected:
                result.append(_to_public(meta))
        if not result:
            raise ValueError("文件未成功注册算法")
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"加载失败：{exc}") from exc
