"""编辑服务契约与真实 ty 进程的端到端检查."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from axile.server.api.routes import editor


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(editor.router)
    with TestClient(app) as value:
        yield value


def request(socket, identifier, method, params):
    socket.send_json({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
    while True:
        message = socket.receive_json()
        if message.get("id") == identifier:
            assert "error" not in message, message
            return message["result"]


def initialize(socket):
    session = socket.receive_json()
    capabilities = request(
        socket,
        1,
        "initialize",
        {
            "capabilities": {
                "textDocument": {
                    "completion": {"completionItem": {"snippetSupport": True}},
                    "diagnostic": {},
                    "hover": {"contentFormat": ["plaintext"]},
                    "semanticTokens": {
                        "requests": {"full": True},
                        "tokenTypes": ["class", "function", "variable", "parameter"],
                        "tokenModifiers": [],
                        "formats": ["relative"],
                    },
                    "inlayHint": {},
                    "foldingRange": {},
                    "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}},
                }
            },
        },
    )
    socket.send_json({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    return session, capabilities


def open_document(socket, uri, code):
    socket.send_json(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": code},
            },
        }
    )


def test_formatting_and_syntax_errors(client):
    response = client.post("/editor/format", json={"code": 'x={"中文":1}\n'})
    assert response.status_code == 200
    assert response.json()["code"] == 'x = {"中文": 1}\n'
    assert client.post("/editor/format", json={"code": "def broken("}).status_code == 422


def test_contract_rejects_uncallable_signatures(client):
    for code in ("def notify(*, context):\n    pass\n", "async def notify(*, context):\n    pass\n"):
        report = client.post("/editor/contract", json={"kind": "account_notification", "code": code}).json()
        assert report["diagnostics"][0]["severity"] == 1
    code = "from __future__ import annotations\ndef calculate_portfolio(context):\n    return {}\n"
    report = client.post("/editor/contract", json={"kind": "portfolio", "code": code}).json()
    assert report["fixes"][0]["edits"][1]["range"]["start"]["line"] == 1


def test_source_access_is_read_only_and_scoped(client, tmp_path):
    source = Path(editor.__file__).resolve()
    assert client.get("/editor/source", params={"uri": source.as_uri()}).status_code == 200
    assert client.get("/editor/source", params={"uri": "file:///etc/passwd"}).status_code == 403
    assert client.get("/editor/source", params={"uri": (tmp_path / "secret.py").as_uri()}).status_code == 403


def test_origin_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/editor/lsp", headers={"origin": "https://unrelated.example"}):
            pass


def test_foreign_document_rejected():
    with pytest.raises(ValueError, match="当前编辑会话"):
        editor.validate_message(
            {"method": "textDocument/didOpen", "params": {"textDocument": {"uri": "file:///tmp/other.py"}}},
            "file:///tmp/mine.py",
        )


def test_external_source_supports_readonly_definition_navigation(client):
    source = Path(editor.__file__).resolve().parents[2] / "context.py"
    source_uri = source.as_uri()
    text = source.read_text(encoding="utf-8")
    with client.websocket_connect("/editor/lsp") as socket:
        session, _ = initialize(socket)
        open_document(socket, source_uri, text)
        definition = request(
            socket,
            32,
            "textDocument/definition",
            {"textDocument": {"uri": source_uri}, "position": {"line": 6, "character": 80}},
        )
        assert "unified_account_assets.py" in str(definition), definition
        with pytest.raises(ValueError, match="当前编辑会话"):
            editor.validate_message(
                {"method": "textDocument/didChange", "params": {"textDocument": {"uri": source_uri}}},
                session["uri"],
            )
        with pytest.raises(ValueError, match="当前编辑会话"):
            editor.validate_message(
                {"method": "textDocument/rename", "params": {"textDocument": {"uri": source_uri}}},
                session["uri"],
            )
        with pytest.raises(ValueError, match="内容与服务器文件不一致"):
            editor.validate_message(
                {"method": "textDocument/didOpen", "params": {"textDocument": {"uri": source_uri, "text": "changed"}}},
                session["uri"],
            )
