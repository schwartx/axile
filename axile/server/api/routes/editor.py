"""保留编辑入口契约接口；交互语言服务在浏览器 WASM 中运行。"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from axile.server.editor_contract import EditorKind, check_editor_contract

router = APIRouter(prefix="/editor", tags=["editor"])


class ContractRequest(BaseModel):
    """当前代码及页面要求的入口契约。"""

    code: str = Field(max_length=2 * 1024 * 1024)
    kind: EditorKind


@router.post("/contract", response_model=None)
async def editor_contract(payload: ContractRequest) -> dict[str, list[dict[str, object]]]:
    """提供服务端契约检查，供兼容调用与跨实现回归核对。"""
    return check_editor_contract(payload.code, payload.kind)
