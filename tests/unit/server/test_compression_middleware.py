"""Verify application compression without starting trading or database lifespans."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse, StreamingResponse

from axile.server.app import _app
from axile.server.compression import CompressionMiddleware


@pytest.mark.parametrize(
    ("path", "encoding", "compressed"),
    [
        ("/large", "gzip", True),
        ("/large", "zstd", True),
        ("/large", "gzip, zstd", True),
        ("/large", "gzip;q=0, zstd;q=0", False),
        ("/large", "zstd;q=0, gzip", True),
        ("/large", "identity", False),
        ("/small", "gzip", False),
        ("/medium", "gzip", False),
        ("/events", "gzip", False),
        ("/events", "zstd", False),
        ("/medium", "zstd", False),
    ],
)
def test_application_gzip_policy(path, encoding, compressed):
    """Exercise the configured middleware stack with isolated response routes."""
    app = FastAPI(middleware=list(_app.user_middleware))
    payload = {"data": "x" * 4096}
    event = "data: " + "x" * 4096 + "\n\n"

    @app.get("/large")
    async def large():
        return JSONResponse(payload)

    @app.get("/small")
    async def small():
        return JSONResponse({"ok": True})

    @app.get("/medium")
    async def medium():
        return JSONResponse({"data": "x" * 2048})

    @app.get("/events")
    async def events():
        return StreamingResponse(iter([event]), media_type="text/event-stream")

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, headers={"Accept-Encoding": encoding})

    response = asyncio.run(request())
    assert response.status_code == 200
    assert (response.headers.get("content-encoding") in ("gzip", "zstd")) is compressed
    if compressed:
        expected = "zstd" if "zstd" in encoding and "zstd;q=0" not in encoding else "gzip"
        assert response.headers["content-encoding"] == expected
    if compressed:
        assert "accept-encoding" in response.headers["vary"].lower()
        assert int(response.headers["content-length"]) < len(response.content)
        assert response.json() == payload
    elif path == "/events":
        assert response.text == event
    elif path == "/medium":
        assert response.json() == {"data": "x" * 2048}
    else:
        assert response.json() == (payload if path == "/large" else {"ok": True})


def test_application_gzip_uses_low_cost_settings():
    middleware = next(item for item in _app.user_middleware if item.cls is CompressionMiddleware)
    assert middleware.kwargs == {"minimum_size": 4096}


def test_zstd_preserves_streams_and_existing_encoding():
    """Do not buffer JSON streams or recompress already encoded responses."""
    from starlette.responses import Response

    async def request():
        app = FastAPI(middleware=list(_app.user_middleware))

        @app.get("/stream")
        async def stream():
            return StreamingResponse(iter([b"[", b"0," * 4096, b"0]"]), media_type="application/json")

        @app.get("/encoded")
        async def encoded():
            return Response(b"encoded" * 1024, media_type="application/json", headers={"Content-Encoding": "custom"})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stream_response = await client.get("/stream", headers={"Accept-Encoding": "zstd"})
            assert "content-encoding" not in stream_response.headers
            assert len(stream_response.json()) == 4097
            encoded_response = await client.get("/encoded", headers={"Accept-Encoding": "zstd"})
            assert encoded_response.headers["content-encoding"] == "custom"
            assert encoded_response.content == b"encoded" * 1024

    asyncio.run(request())
