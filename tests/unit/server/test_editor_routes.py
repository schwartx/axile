"""编辑器服务端契约与浏览器 WASM 共用相同的冻结样例。"""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import editor


def test_contract_matches_shared_browser_fixtures():
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
