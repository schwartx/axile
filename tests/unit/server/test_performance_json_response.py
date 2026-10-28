"""事实 JSON 原文透传，大整数不经过 Python 重编码。"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import account_analysis as routes


@pytest.mark.parametrize(
    "endpoint,operation", [("facts-state", "browser_analysis_state"), ("facts", "browser_analysis_facts")]
)
def test_facts_response_preserves_native_bytes(monkeypatch, endpoint, operation):
    content = b'{ "large_id":9223372036854775807, "missing":null }\n'
    read = AsyncMock(return_value=content)
    monkeypatch.setattr(routes, "native_operation", read)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = (
            client.get("/performance/1/facts-state")
            if endpoint == "facts-state"
            else client.post(
                "/performance/1/facts",
                json={"source_id": "source", "until": "1", "cursor": {"seq": "0", "table": 22, "after": None}},
            )
        )
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["cache-control"] == "no-store"
    assert read.await_args.args[0] == operation
