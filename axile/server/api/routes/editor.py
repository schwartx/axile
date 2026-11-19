"""为单文件 Python 编辑器提供隔离的语言服务与格式化."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from urllib.parse import urlsplit
from urllib.request import url2pathname

import anyio
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from msgspec import Meta

from axile.server.api.inputs import json_body
from axile.server.editor_contract import EditorKind, check_editor_contract

router = APIRouter(prefix="/editor", tags=["editor"])
MAX_MESSAGE = 2 * 1024 * 1024
MAX_SESSIONS = 8
_sessions: set[WebSocket] = set()


NAVIGATION_METHODS = {"textDocument/definition", "textDocument/references"}
MAX_PREVIEW_FILES = 128


@dataclass
class SourcePreviews:
    """记录本次会话中 ty 返回的导航请求和已发送的只读源码。"""

    pending: set[int | str] = field(default_factory=set)
    sent: dict[str, bytes] = field(default_factory=dict)


def read_preview(uri: str) -> str | None:
    """仅用于读取 ty 返回的本机 Python 文件。"""
    try:
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc:
            return None
        local = url2pathname(parsed.path)
        path = Path(local)
        if local.replace("\\", "/").startswith("//") or not path.is_absolute():
            return None
        path = path.resolve(strict=True)
        if path.suffix not in {".py", ".pyi"} or not path.is_file():
            return None
        with path.open(encoding="utf-8") as source:
            code = source.read(MAX_MESSAGE + 1)
        return code if len(code) <= MAX_MESSAGE else None
    except (OSError, ValueError):
        return None


def attach_previews(message: dict, uri: str, previews: SourcePreviews) -> None:
    """随导航结果发送源码，客户端之后只可打开已收到的文档。"""
    locations = message.get("result")
    if not locations:
        return
    sources: dict[str, str] = {}
    budget = MAX_MESSAGE - len(json.dumps(message, ensure_ascii=False).encode()) - 32
    for location in locations if isinstance(locations, list) else [locations]:
        if not isinstance(location, dict):
            continue
        target = location.get("targetUri") or location.get("uri")
        if not isinstance(target, str) or target == uri or target in previews.sent:
            continue
        if len(previews.sent) >= MAX_PREVIEW_FILES or budget <= 0:
            break
        code = read_preview(target)
        if code is None:
            continue
        size = len(json.dumps({target: code}, ensure_ascii=False).encode())
        if size <= budget:
            budget -= size
            sources[target] = code
            previews.sent[target] = sha256(code.encode()).digest()
    if sources:
        message["axileSources"] = sources


@dataclass(kw_only=True)
class FormatRequest:
    """待格式化的完整文档，不执行代码."""

    code: Annotated[str, Meta(max_length=MAX_MESSAGE)]


@dataclass(kw_only=True)
class ContractRequest:
    """当前代码及页面要求的入口契约。"""

    code: Annotated[str, Meta(max_length=MAX_MESSAGE)]
    kind: EditorKind


@router.post("/contract", response_model=None)
async def editor_contract(
    payload: Annotated[ContractRequest, Depends(json_body(ContractRequest))],
) -> dict[str, list[dict[str, object]]]:
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
async def format_python(payload: Annotated[FormatRequest, Depends(json_body(FormatRequest))]) -> dict[str, str]:
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


def validate_message(message: dict, uri: str, previews: SourcePreviews) -> None:
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
    if not isinstance(document_uri, str) or method not in readonly_methods or document_uri not in previews.sent:
        raise ValueError("文档不属于当前编辑会话")
    if method == "textDocument/didOpen":
        text = document.get("text")
        if not isinstance(text, str) or sha256(text.encode()).digest() != previews.sent[document_uri]:
            raise ValueError("只读源码内容与预览不一致")


async def relay_client(
    socket: WebSocket, writer: asyncio.StreamWriter, uri: str, root: str, previews: SourcePreviews
) -> None:
    """将 WebSocket JSON 转为 LSP 帧，固定工作区和 Python 环境."""
    while True:
        raw = await asyncio.wait_for(socket.receive_text(), 600)
        if len(raw.encode()) > MAX_MESSAGE:
            raise ValueError("文档过大")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("无效 LSP 消息")
        validate_message(message, uri, previews)
        method = message.get("method")
        if not method:
            # 客户端不提供工作区配置，也无需响应 ty 发起的请求。
            continue
        request_id = message.get("id")
        if method in NAVIGATION_METHODS and isinstance(request_id, (int, str)) and len(previews.pending) < 128:
            previews.pending.add(request_id)
        if method == "initialize":
            params = message.setdefault("params", {})
            capabilities = params.get("capabilities", {})
            if not isinstance(capabilities, dict):
                raise ValueError("无效 LSP capabilities")
            message["params"] = params = {
                "capabilities": {key: value for key, value in capabilities.items() if key != "workspace"},
                "processId": None,
                "rootUri": root,
                "workspaceFolders": [{"uri": root, "name": "portfolio"}],
            }
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


async def relay_server(socket: WebSocket, reader: asyncio.StreamReader, uri: str, previews: SourcePreviews) -> None:
    """按原 LSP 结构转发响应，只为定义和引用结果附加源码。"""
    while True:
        raw = await read_message(reader)
        message = json.loads(raw)
        response_id = message.get("id")
        if "method" not in message and isinstance(response_id, (int, str)) and response_id in previews.pending:
            previews.pending.discard(response_id)
            await asyncio.to_thread(attach_previews, message, uri, previews)
            raw = json.dumps(message, ensure_ascii=False)
        await socket.send_text(raw)


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
                previews = SourcePreviews()
                tasks = [
                    asyncio.create_task(relay_client(socket, process.stdin, uri, root, previews)),
                    asyncio.create_task(relay_server(socket, process.stdout, uri, previews)),
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
