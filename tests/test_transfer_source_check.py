"""来源端检测只读取身份，不导出、导入或清理。"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server import data_transfer as transfer
from axile.server.api.routes.transfer import router


@pytest.mark.parametrize(
    "case,status,message",
    [
        ("ok", 200, None),
        ("self", 409, "来源端不能是本机"),
        ("missing", 409, "未提供有效的数据传送身份"),
        ("invalid", 409, "未提供有效的数据传送身份"),
        ("html", 409, "未提供有效的数据传送身份"),
        ("404", 409, "HTTP 404"),
        ("redirect", 409, "HTTP 302"),
        ("timeout", 409, "检测超时"),
        ("offline", 409, "无法连接来源端"),
    ],
)
def test_source_check_is_bounded_read_only_and_rejects_wrong_peers(monkeypatch, case, status, message):
    calls = []
    monkeypatch.setattr(transfer, "settings", lambda: {"source_id": "local"})

    def reject_data_operation(*args, **kwargs):
        pytest.fail("检测不得读写传送数据")

    monkeypatch.setattr(transfer, "_operation", reject_data_operation)

    def remote(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://source.test:1419/base/api/v1/transfer/identity"
        if case == "timeout":
            raise httpx.ReadTimeout("private detail")
        if case == "offline":
            raise httpx.ConnectError("private detail")
        if case == "404":
            return httpx.Response(404)
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://other.test"})
        if case == "html":
            return httpx.Response(200, text="<html>different service</html>")
        body = {"source_id": "local" if case == "self" else "remote"}
        if case == "missing":
            body = {"source_id": ""}
        if case == "invalid":
            body = []
        return httpx.Response(200, json=body)

    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"timeout": 5, "follow_redirects": False}
        return original(transport=httpx.MockTransport(remote), **kwargs)

    monkeypatch.setattr(transfer.httpx, "AsyncClient", client)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as api:
        response = api.post("/api/v1/transfer/check-source", json={"source_url": "http://source.test:1419/base/"})
    assert response.status_code == status
    if message:
        assert message in response.json()["detail"]
        assert "private detail" not in response.text
    else:
        assert response.json() == {"source_id": "remote"}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "host:1419",
        "http://user:password@host",
        "http://host/?query=x",
        "http://host:abc",
        "http://host:65536",
    ],
)
def test_invalid_source_does_not_access_network(monkeypatch, url):
    def reject_client(**kwargs):
        pytest.fail("非法地址不得访问网络")

    monkeypatch.setattr(transfer.httpx, "AsyncClient", reject_client)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as api:
        assert api.post("/api/v1/transfer/check-source", json={"source_url": url}).status_code == 422
