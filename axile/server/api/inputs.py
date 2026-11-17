"""HTTP 编辑入口的载荷解析，不向 FastAPI 注册领域数据模型。"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import Body, Request
from fastapi.exceptions import RequestValidationError

from axile.common.serialization import parse_input


def _parse(cls: type, value: Any, source: str):
    try:
        return parse_input(cls, value, strict=source != "query")
    except (ValueError, TypeError) as error:
        match = re.search(r"at `\$\.([^`]+)`", str(error))
        missing = re.search(r"missing required field `([^`]+)`", str(error))
        path = match.group(1) if match else missing.group(1) if missing else ""
        location = (source, *(int(part) if part.isdecimal() else part for part in re.findall(r"[^.\[\]]+", path)))
        error_type = (
            "missing" if missing else "extra_forbidden" if str(error).startswith("extra_forbidden:") else "value_error"
        )
        raise RequestValidationError(
            [{"type": error_type, "loc": location, "msg": str(error), "input": None}]
        ) from error


def json_body(cls: type):
    """通过 FastAPI/Pydantic 绑定请求体，再检查配置字段与业务约束。"""

    async def dependency(value: Annotated[dict[str, Any], Body()]):
        return _parse(cls, value, "body")

    return dependency


def query_params(cls: type):
    """将查询字符串转换为一次请求使用的参数对象。"""

    def dependency(request: Request):
        return _parse(cls, dict(request.query_params), "query")

    return dependency
