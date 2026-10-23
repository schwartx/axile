"""Negotiate low-cost compression without buffering streaming responses."""

from functools import partial

import anyio
import zstandard
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _quality(header: str, encoding: str) -> float:
    qualities = {}
    for entry in header.lower().split(","):
        name, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            key, _, value = parameter.strip().partition("=")
            if key == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        qualities[name] = quality if 0 <= quality <= 1 else 0.0
    return qualities.get(encoding, qualities.get("*", 0.0))


class CompressionMiddleware:
    """Prefer zstd level 1, with gzip fallback and bounded worker concurrency."""

    def __init__(self, app: ASGIApp, minimum_size: int = 4096) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=3)
        self._limiter: anyio.CapacityLimiter | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Select a coding and compress only complete JSON response bodies."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        accept = Headers(scope=scope).get("accept-encoding", "")
        zstd_quality, gzip_quality = _quality(accept, "zstd"), _quality(accept, "gzip")
        if zstd_quality <= 0 or gzip_quality > zstd_quality:
            # Pass only the selected coding: Starlette's gzip negotiation does
            # not interpret q=0, and must not compress an explicitly refused coding.
            selected = b"gzip" if gzip_quality > 0 else b"identity"
            headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"accept-encoding"]
            await self.gzip({**scope, "headers": [*headers, (b"accept-encoding", selected)]}, receive, send)
            return
        start: Message = {}
        passthrough = False

        async def compress_send(message: Message) -> None:
            nonlocal start, passthrough
            if message["type"] == "http.response.start":
                start = message
                headers = Headers(raw=message["headers"])
                media_type = headers.get("content-type", "").split(";")[0]
                passthrough = (
                    message["status"] in (204, 206, 304)
                    or "content-encoding" in headers
                    or not (media_type == "application/json" or media_type.endswith("+json"))
                )
                return
            if message["type"] == "http.response.body" and start:
                body = message.get("body", b"")
                # Never collect chunks: SSE and other streams retain their latency.
                if not passthrough and not message.get("more_body", False):
                    headers = MutableHeaders(raw=start["headers"])
                    headers.add_vary_header("Accept-Encoding")
                    if len(body) >= self.minimum_size:
                        if self._limiter is None:
                            self._limiter = anyio.CapacityLimiter(2)
                        compress = partial(_compress, body)
                        encoded = await anyio.to_thread.run_sync(compress, limiter=self._limiter)
                        if len(encoded) < len(body):
                            headers["Content-Encoding"] = "zstd"
                            headers["Content-Length"] = str(len(encoded))
                            message = {**message, "body": encoded}
                await send(start)
                start = {}
                passthrough = True
            elif start:
                await send(start)
                start = {}
            await send(message)

        await self.app(scope, receive, compress_send)


def _compress(body: bytes) -> bytes:
    return zstandard.ZstdCompressor(level=1).compress(body)
