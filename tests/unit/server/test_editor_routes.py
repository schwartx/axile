"""编辑服务契约与真实 ty 进程的端到端检查."""

import json
import sys
import sysconfig
import venv
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

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


def request(socket, identifier, method, params, *, response=False):
    socket.send_json({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
    while True:
        message = socket.receive_json()
        if message.get("id") == identifier:
            assert "error" not in message, message
            return message if response else message["result"]


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


def test_signal_quote_editor_checks_fetch_quotes_without_import_fix(client):
    def check(code):
        response = client.post("/editor/contract", json={"kind": "signal_quotes", "code": code})
        assert response.status_code == 200, response.text
        return response.json()

    assert "fetch_quotes(symbols)" in check("def calculate_portfolio(context): return {}")["diagnostics"][0]["message"]
    assert "必须是同步函数" in check("async def fetch_quotes(symbols): return {}")["diagnostics"][0]["message"]
    report = check("def fetch_quotes(symbols): return {}")
    assert [edit["newText"] for edit in report["fixes"][0]["edits"]] == [": list[str]"]
    assert check("def fetch_quotes(symbols: list[str]): return {}") == {"diagnostics": [], "fixes": []}


def test_signal_account_editor_checks_fetch_account(client):
    response = client.post(
        "/editor/contract", json={"kind": "signal_account", "code": "def fetch_account(context): return {}"}
    )
    assert response.status_code == 200
    edits = response.json()["fixes"][0]["edits"]
    assert edits[0]["newText"] == ": SignalAccountContext"
    assert edits[1]["newText"] == "from axile.channels.rebalance_signal import SignalAccountContext\n"
    invalid = client.post(
        "/editor/contract", json={"kind": "signal_account", "code": "async def fetch_account(context): return {}"}
    )
    assert "必须是同步函数" in invalid.json()["diagnostics"][0]["message"]


def test_source_path_is_not_an_http_endpoint(client):
    response = client.get("/editor/source", params={"uri": Path(editor.__file__).resolve().as_uri()})
    assert response.status_code == 404


def test_origin_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/editor/lsp", headers={"origin": "https://unrelated.example"}):
            pass


def test_foreign_document_rejected():
    with pytest.raises(ValueError, match="当前编辑会话"):
        editor.validate_message(
            {"method": "textDocument/didOpen", "params": {"textDocument": {"uri": "file:///tmp/other.py"}}},
            "file:///tmp/mine.py",
            editor.SourcePreviews(),
        )


def test_external_source_supports_readonly_definition_navigation(client):
    with client.websocket_connect("/editor/lsp") as socket:
        session, _ = initialize(socket)
        uri = session["uri"]
        open_document(socket, uri, "from axile.server.context import Context\nContext\n")
        inbound = request(
            socket,
            2,
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": 1, "character": 1}},
            response=True,
        )
        source_uri = inbound["result"][0]["uri"]
        text = inbound["axileSources"][source_uri]
        line, statement = next(
            (index, statement)
            for index, statement in enumerate(text.splitlines())
            if statement.startswith("from axile.executor.models.unified_account_assets import ")
        )
        open_document(socket, source_uri, text)
        definition = request(
            socket,
            3,
            "textDocument/definition",
            {
                "textDocument": {"uri": source_uri},
                "position": {"line": line, "character": statement.index("UnifiedAccountAssets")},
            },
            response=True,
        )
        next_uri = definition["result"][0]["uri"]
        assert next_uri.endswith("/unified_account_assets.py")
        assert "class UnifiedAccountAssets" in definition["axileSources"][next_uri]
        previews = editor.SourcePreviews(sent={source_uri: sha256(text.encode()).digest()})
        for method in ("textDocument/didChange", "textDocument/rename"):
            with pytest.raises(ValueError, match="当前编辑会话"):
                editor.validate_message(
                    {"method": method, "params": {"textDocument": {"uri": source_uri}}}, uri, previews
                )
        with pytest.raises(ValueError, match="内容与预览不一致"):
            editor.validate_message(
                {"method": "textDocument/didOpen", "params": {"textDocument": {"uri": source_uri, "text": "changed"}}},
                uri,
                previews,
            )


def test_editable_dependency_source_is_returned_from_its_project(client, tmp_path, monkeypatch):
    environment = tmp_path / "venv"
    venv.EnvBuilder().create(environment)
    packages = Path(sysconfig.get_path("purelib", vars={"base": str(environment), "platbase": str(environment)}))
    project = tmp_path / "project"
    project.mkdir()
    dependency = project / "editable_dependency.py"
    dependency.write_text("class EditableClient:\n    pass\n", encoding="utf-8")
    # Path-based editable installations register the project's import root with a .pth file.
    (packages / "editable_dependency.pth").write_text(f"{project}\n", encoding="utf-8")
    monkeypatch.setattr(editor, "sys", SimpleNamespace(executable=sys.executable, prefix=str(environment)))
    with client.websocket_connect("/editor/lsp") as socket:
        session, _ = initialize(socket)
        uri = session["uri"]
        open_document(socket, uri, "from editable_dependency import EditableClient\nEditableClient()\n")
        definition = request(
            socket,
            2,
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": 1, "character": 1}},
            response=True,
        )
        assert definition["result"][0]["uri"] == dependency.as_uri()
        assert "class EditableClient" in definition["axileSources"][dependency.as_uri()]


def test_contract_matches_fixtures():
    app = FastAPI()
    app.include_router(editor.router)
    fixtures = json.loads((Path(__file__).parents[2] / "fixtures/editor_contract.json").read_text())
    with TestClient(app) as client:
        for fixture in fixtures:
            response = client.post("/editor/contract", json={"code": fixture["code"], "kind": fixture["kind"]})
            assert response.status_code == 200
            assert response.json() == fixture["report"]
        assert client.post("/editor/contract", json={"kind": "unknown", "code": ""}).status_code == 422
        assert (
            client.post("/editor/contract", json={"kind": "portfolio", "code": "x" * (2 * 1024 * 1024 + 1)}).status_code
            == 422
        )


def test_installed_dependency_completion_definition_and_signature(client):
    with client.websocket_connect("/editor/lsp") as socket:
        session, _ = initialize(socket)
        uri = session["uri"]
        open_document(socket, uri, "import httpx\nhttpx.Cl\nhttpx.Client(invalid_parameter=True)\n")
        params = {"textDocument": {"uri": uri}}
        completion = request(
            socket,
            2,
            "textDocument/completion",
            {
                **params,
                "position": {"line": 1, "character": 8},
            },
        )
        items = completion["items"] if isinstance(completion, dict) else completion
        assert any(item["label"] == "Client" for item in items)
        signature = request(
            socket,
            3,
            "textDocument/signatureHelp",
            {
                **params,
                "position": {"line": 2, "character": 13},
            },
        )
        assert "timeout" in signature["signatures"][0]["label"]
        definition = request(
            socket,
            4,
            "textDocument/definition",
            {
                **params,
                "position": {"line": 2, "character": 8},
            },
            response=True,
        )
        target = definition["result"][0]["uri"]
        assert target.endswith("/httpx/_client.py")
        assert "class Client" in definition["axileSources"][target]
        report = request(socket, 5, "textDocument/diagnostic", params)
        assert any("invalid_parameter" in item["message"] for item in report["items"])
