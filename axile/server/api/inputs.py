"""HTTP 编辑入口的载荷解析，不向 FastAPI 注册领域数据模型。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Request
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
        raise RequestValidationError(
            [{"type": "missing" if missing else "value_error", "loc": location, "msg": str(error), "input": None}]
        ) from error


def json_body(cls: type):
    """在配置提交入口解析 JSON；业务对象内部直接流转。"""

    async def dependency(request: Request):
        try:
            value = await request.json()
        except ValueError as error:
            raise RequestValidationError(
                [{"type": "json_invalid", "loc": ("body",), "msg": "无效的 JSON 请求", "input": None}]
            ) from error
        return _parse(cls, value, "body")

    return dependency


def query_params(cls: type):
    """将查询字符串转换为一次请求使用的参数对象。"""

    def dependency(request: Request):
        return _parse(cls, dict(request.query_params), "query")

    return dependency
