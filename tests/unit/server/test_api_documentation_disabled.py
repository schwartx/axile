"""服务不再提供 API schema 与文档页面。"""

import pytest
from fastapi.testclient import TestClient

from axile.server import app


@pytest.mark.parametrize("embedded_frontend", [False, True])
def test_documentation_urls_return_404_with_or_without_frontend(tmp_path, monkeypatch, embedded_frontend):
    assert app._app.openapi_url is None
    assert app._app.docs_url is None
    assert app._app.redoc_url is None
    routes = [
        route
        for route in app._app.router.routes
        if getattr(route, "name", None) not in {"serve_index", "spa_fallback", "assets"}
    ]
    monkeypatch.setattr(app._app.router, "routes", routes)
    if embedded_frontend:
        (tmp_path / "index.html").write_text("embedded frontend")
        monkeypatch.setattr(app, "_STATIC_DIR", tmp_path)
        app._mount_embedded_frontend()
    client = TestClient(app._app)
    for path in ("/api/v1/openapi.json", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
        assert client.get(path).status_code == 404
    if embedded_frontend:
        assert client.get("/").text == "embedded frontend"
