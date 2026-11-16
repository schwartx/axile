"""保留编辑入口契约接口；交互语言服务在浏览器 WASM 中运行。"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from msgspec import Meta

from axile.server.api.inputs import json_body
from axile.server.editor_contract import EditorKind, check_editor_contract

router = APIRouter(prefix="/editor", tags=["editor"])


@dataclass(kw_only=True)
class ContractRequest:
    """当前代码及页面要求的入口契约。"""

    code: Annotated[str, Meta(max_length=2 * 1024 * 1024)]
    kind: EditorKind


@router.post("/contract", response_model=None)
async def editor_contract(
    payload: Annotated[ContractRequest, Depends(json_body(ContractRequest))],
) -> dict[str, list[dict[str, object]]]:
    """提供服务端契约检查，供兼容调用与跨实现回归核对。"""
    return check_editor_contract(payload.code, payload.kind)
