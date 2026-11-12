"""用于健康检查、诊断与本机目录浏览的工具路由."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

router = APIRouter(prefix="/utils", tags=["utils"])


def _entry(path: Path) -> dict[str, Any]:
    """将本机路径转换为不含文件内容的公开目录项."""
    return {"name": path.name or str(path), "path": str(path)}


@router.get("/health-check/", response_model=None)
def health_check() -> bool:
    """返回一个简单的 API 进程存活信号."""
    return True


@router.get("/directories", response_model=None)
def list_directories(path: str | None = Query(default=None)) -> dict[str, Any]:
    """按用户指定层级列出本机目录，相对路径以服务工作目录为基准."""
    requested = Path.cwd() if path is None else Path(path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested

    try:
        current = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目录不存在") from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无法访问该目录") from exc

    if not current.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="路径不是目录")

    try:
        children = sorted(
            (child for child in current.iterdir() if child.is_dir()),
            key=lambda child: child.name.casefold(),
        )
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限读取该目录") from exc

    parent = None if current.parent == current else str(current.parent)
    return {"path": str(current), "parent": parent, "entries": [_entry(child) for child in children]}
