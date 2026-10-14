"""在独立进程中运行用户自定义通知函数。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Awaitable
from dataclasses import dataclass

from axile.common.function_contract import accepts_context, has_generator_yield

NOTIFICATION_FUNCTION_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class NotificationFunctionResult:
    """单次通知函数运行结果。"""

    ok: bool
    error: str | None = None
    error_line: int | None = None
    traceback: str | None = None


def validate_notification_function(code: str) -> None:
    """校验通知源码定义了 ``notify(context)``。"""
    if not code.strip():
        raise ValueError("通知函数不能为空")
    try:
        tree = ast.parse(code, filename="<notification>")
    except SyntaxError as exc:
        raise ValueError(f"第 {exc.lineno} 行语法错误: {exc.msg}") from exc
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "notify"
    ]
    if not functions:
        raise ValueError("脚本必须定义 notify(context) 函数")
    if has_generator_yield(functions[-1]):
        raise ValueError("notify 不支持生成器或异步生成器函数")
    parameters = functions[-1].args
    if (
        len(parameters.posonlyargs) + len(parameters.args) != 1
        or parameters.vararg
        or parameters.kwarg
        or parameters.kwonlyargs
    ):
        raise ValueError("notify 必须且只能接收一个 context 参数")


def run_notification_function(
    code: str,
    context: dict[str, object],
    *,
    feishu_key: str | None = None,
    system_feishu_key: str | None = None,
) -> NotificationFunctionResult:
    """运行通知函数并强制限制墙钟时间。"""
    try:
        validate_notification_function(code)
        process = subprocess.run(
            [sys.executable, "-m", "axile.common.notification_function", "--child"],
            input=json.dumps({"code": code, "context": context}, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=NOTIFICATION_FUNCTION_TIMEOUT_SECONDS,
            check=False,
            env={
                **os.environ,
                # 管道协议固定为 UTF-8，避免 Windows 默认代码页及继承的编码设置影响中文通知。
                "PYTHONIOENCODING": "utf-8",
                "AXILE_ACCOUNT_FEISHU_KEY": feishu_key or "",
                "AXILE_SYSTEM_FEISHU_KEY": system_feishu_key or "",
            },
        )
        response = json.loads(process.stdout)
        return NotificationFunctionResult(
            ok=bool(response.get("ok")),
            error=str(response.get("error")) if response.get("error") else None,
            error_line=response.get("error_line") if isinstance(response.get("error_line"), int) else None,
            traceback=response.get("traceback") if isinstance(response.get("traceback"), str) else None,
        )
    except subprocess.TimeoutExpired as exc:
        return NotificationFunctionResult(
            False,
            f"通知函数执行超时（{NOTIFICATION_FUNCTION_TIMEOUT_SECONDS} 秒）\n{exc}",
            traceback="".join(traceback.format_exception(exc)),
        )
    except (SyntaxError, ValueError) as exc:
        return NotificationFunctionResult(
            False, str(exc), getattr(exc, "lineno", None), "".join(traceback.format_exception(exc))
        )
    except (OSError, json.JSONDecodeError) as exc:
        return NotificationFunctionResult(
            False, f"通知函数进程失败: {exc}", traceback="".join(traceback.format_exception(exc))
        )


async def _await_notification(result: Awaitable[object]) -> object:
    """在独立事件循环中等待任意 awaitable，而不只接受协程对象。"""
    return await result


def _invoke_notification(function: object, context: dict[str, object]) -> None:
    """等待通知入口完成；生成器不属于通知执行契约。

    Notes
    -----
    同步入口在事件循环外调用，保留其自行使用 asyncio.run 的能力。
    异步入口必须自行等待后台任务；事件循环退出时会取消未完成任务。
    """
    if not callable(function) or not accepts_context(function, allow_async=True):
        raise TypeError("notify 必须且只能接收一个位置参数 context")
    if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise TypeError("notify 不支持生成器或异步生成器函数")
    result = function(context)
    if inspect.isawaitable(result):
        result = asyncio.run(_await_notification(result))
    if inspect.isgenerator(result) or inspect.isasyncgen(result):
        raise TypeError("notify 不支持返回生成器或异步生成器")


def _run_child() -> None:
    """从标准输入读取事件，在子进程内执行用户函数。"""
    import contextlib

    try:
        request = json.load(sys.stdin)
        namespace: dict[str, object] = {}
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(request["code"], "<notification>", "exec"), namespace)  # noqa: S102
            function = namespace["notify"]
            _invoke_notification(function, request["context"])
        payload: dict[str, object] = {"ok": True}
    except BaseException as exc:  # noqa: BLE001 - 子进程边界将用户错误返回给父进程
        line = next(
            (
                frame.lineno
                for frame in reversed(traceback.extract_tb(exc.__traceback__))
                if frame.filename == "<notification>"
            ),
            None,
        )
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "error_line": line,
            "traceback": "".join(traceback.format_exception(exc)),
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--child":
    _run_child()
