"""为单文件 Python 编辑器提供隔离的语言服务与格式化."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import sysconfig
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import url2pathname

import anyio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from axile.server.editor_contract import EditorKind, check_editor_contract

router = APIRouter(prefix="/editor", tags=["editor"])
MAX_MESSAGE = 2 * 1024 * 1024
MAX_SESSIONS = 8
_sessions: set[WebSocket] = set()


@router.get("/source", response_model=None)
async def python_source(uri: str) -> dict[str, str]:
    """只读打开 Python 环境与 Axile 包内的源码，禁止访问任意文件."""
    path = source_path(uri)
    return {"code": await asyncio.to_thread(path.read_text, encoding="utf-8")}


def source_path(uri: str) -> Path:
    """校验语言服务和源码预览共用的只读文件范围."""
    parsed = urlsplit(uri)
    path = Path(url2pathname(parsed.path)).resolve()
    roots = [
        Path(__file__).resolve().parents[3],
        *[Path(sysconfig.get_path(name)).resolve() for name in ("stdlib", "purelib", "platlib")],
    ]
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or path.suffix not in {".py", ".pyi"}
        or not any(path.is_relative_to(root) for root in roots)
    ):
        raise HTTPException(403, "只能预览当前 Python 环境中的源码")
    if not path.is_file():
        raise HTTPException(404, "源码文件不存在")
    if path.stat().st_size > MAX_MESSAGE:
        raise HTTPException(413, "源码过大，无法预览")
    return path


class FormatRequest(BaseModel):
    """待格式化的完整文档，不执行代码."""

    code: str = Field(max_length=MAX_MESSAGE)


class ContractRequest(BaseModel):
    """当前代码及页面要求的入口契约。"""

    code: str = Field(max_length=MAX_MESSAGE)
    kind: EditorKind


@router.post("/contract", response_model=None)
async def editor_contract(payload: ContractRequest) -> dict[str, list[dict[str, object]]]:
    """为编辑器返回入口函数诊断与类型标注修复。"""
    return check_editor_contract(payload.code, payload.kind)


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """回收子进程，避免关闭页面后残留语言服务."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


@router.post("/format", response_model=None)
async def format_python(payload: FormatRequest) -> dict[str, str]:
    """用固定 Ruff 配置格式化源码，保留语法错误原文供编辑器展示."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ruff",
        "format",
        "--isolated",
        "--stdin-filename",
        "portfolio.py",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(payload.code.encode()), 10)
        if process.returncode:
            raise HTTPException(422, error.decode(errors="replace")[:4000])
        return {"code": output.decode()}
    except TimeoutError as exc:
        raise HTTPException(504, "格式化超时，请重试") from exc
    finally:
        with anyio.CancelScope(shield=True):
            await stop_process(process)


async def read_message(reader: asyncio.StreamReader) -> str:
    """读取 stdio LSP 帧；长度按 UTF-8 字节计算并设上限."""
    header = await reader.readuntil(b"\r\n\r\n")
    fields = dict(line.split(b":", 1) for line in header.strip().split(b"\r\n"))
    length = int(next((value for key, value in fields.items() if key.lower() == b"content-length"), b"0"))
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("语言服务消息过大")
    return (await reader.readexactly(length)).decode()


def validate_message(message: dict, uri: str) -> None:
    """草稿可编辑；受限源码仅允许只读语言服务请求."""
    method = message.get("method", "")
    allowed = {
        "initialize",
        "initialized",
        "shutdown",
        "exit",
        "$/cancelRequest",
        "textDocument/didOpen",
        "textDocument/didChange",
        "textDocument/didClose",
        "textDocument/completion",
        "completionItem/resolve",
        "textDocument/hover",
        "textDocument/signatureHelp",
        "textDocument/diagnostic",
        "textDocument/definition",
        "textDocument/references",
        "textDocument/rename",
        "textDocument/prepareRename",
        "textDocument/codeAction",
        "codeAction/resolve",
        "textDocument/semanticTokens/full",
        "textDocument/inlayHint",
        "inlayHint/resolve",
        "textDocument/foldingRange",
        "textDocument/documentSymbol",
    }
    if not isinstance(method, str) or (method and method not in allowed):
        raise ValueError("不支持的编辑器操作")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("无效 LSP 参数")
    document = params.get("textDocument")
    if document is None:
        return
    document_uri = document.get("uri") if isinstance(document, dict) else None
    if document_uri == uri:
        return
    readonly_methods = {
        "textDocument/didOpen",
        "textDocument/didClose",
        "textDocument/completion",
        "textDocument/hover",
        "textDocument/signatureHelp",
        "textDocument/diagnostic",
        "textDocument/definition",
        "textDocument/references",
        "textDocument/semanticTokens/full",
        "textDocument/inlayHint",
        "textDocument/foldingRange",
        "textDocument/documentSymbol",
    }
    if not isinstance(document_uri, str) or method not in readonly_methods:
        raise ValueError("文档不属于当前编辑会话")
    try:
        path = source_path(document_uri)
    except HTTPException as exc:
        raise ValueError("文档不属于当前编辑会话") from exc
    if method == "textDocument/didOpen" and document.get("text") != path.read_text(encoding="utf-8"):
        raise ValueError("只读源码内容与服务器文件不一致")


async def relay_client(socket: WebSocket, writer: asyncio.StreamWriter, uri: str, root: str) -> None:
    """将 WebSocket JSON 转为 LSP 帧，固定工作区和 Python 环境."""
    while True:
        raw = await asyncio.wait_for(socket.receive_text(), 600)
        if len(raw.encode()) > MAX_MESSAGE:
            raise ValueError("文档过大")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("无效 LSP 消息")
        validate_message(message, uri)
        if message.get("method") == "initialize":
            params = message.setdefault("params", {})
            params.update(rootUri=root, workspaceFolders=[{"uri": root, "name": "portfolio"}])
            params["initializationOptions"] = {
                "diagnosticMode": "openFilesOnly",
                "configuration": {
                    "environment": {
                        "python": sys.prefix,
                        "extra-paths": [str(Path(__file__).resolve().parents[4])],
                    }
                },
            }
        body = json.dumps(message, ensure_ascii=False).encode()
        writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()


async def relay_server(socket: WebSocket, reader: asyncio.StreamReader) -> None:
    """转发语言服务响应；标准错误独立丢弃，不能混入协议流."""
    while True:
        await socket.send_text(await read_message(reader))


@router.websocket("/lsp")
async def python_lsp(socket: WebSocket) -> None:
    """每个页面连接拥有独立 ty 进程与临时工作区，断开即回收."""
    origin = socket.headers.get("origin")
    if origin and urlsplit(origin).netloc != socket.headers.get("host"):
        await socket.close(code=1008)
        return
    if len(_sessions) >= MAX_SESSIONS:
        await socket.close(code=1013)
        return
    _sessions.add(socket)
    try:
        await socket.accept()
        with TemporaryDirectory(prefix="axile-editor-") as directory:
            root = Path(directory).as_uri()
            uri = (Path(directory) / "portfolio.py").as_uri()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ty",
                "server",
                cwd=directory,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_MESSAGE,
            )
            tasks: list[asyncio.Task] = []
            try:
                assert process.stdin is not None and process.stdout is not None
                await socket.send_json({"type": "session", "uri": uri, "rootUri": root})
                tasks = [
                    asyncio.create_task(relay_client(socket, process.stdin, uri, root)),
                    asyncio.create_task(relay_server(socket, process.stdout)),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                # ASGI 在客户端离开时可能取消整个请求，回收必须完成后才移除工作区。
                with anyio.CancelScope(shield=True):
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await stop_process(process)
    except (WebSocketDisconnect, OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        _sessions.discard(socket)
        with contextlib.suppress(RuntimeError, OSError, WebSocketDisconnect):
            await socket.close(code=1011)
