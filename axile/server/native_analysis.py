"""以小请求调用只读 Rust 分析进程，保持历史与响应位于 Python 对象边界之外."""

from __future__ import annotations

import asyncio
import json
import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

_REQUEST_LIMIT = 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_DEFAULT_OUTPUT_LIMIT = 64 * 1024 * 1024
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_CONCURRENCY = 2
_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


class NativeAnalysisError(RuntimeError):
    """携带稳定错误码的原生分析失败，不在失败后重跑 Python 计算."""

    def __init__(self, message: str, *, code: str = "NATIVE_ANALYSIS_ERROR") -> None:
        """保存可映射到 HTTP 状态的稳定错误码."""
        super().__init__(message)
        self.code = code


class NativeAnalysisUnavailable(NativeAnalysisError):
    """显式选择原生分析时，运行程序或数据库不适用."""


class NativeAnalysisTimeout(NativeAnalysisError):
    """排队或原生计算超过任务期限，子进程已终止."""


class NativeAnalysisProtocolError(NativeAnalysisError):
    """子进程输出违反协议或资源预算."""


def _positive_setting(name: str, default: float, *, integer: bool = False) -> float | int:
    value = os.environ.get(name)
    try:
        parsed = int(value) if integer and value is not None else float(value) if value is not None else default
    except ValueError as exc:
        raise NativeAnalysisUnavailable(f"{name} 必须为正数", code="INVALID_NATIVE_CONFIG") from exc
    if parsed <= 0 or not float(parsed) < float("inf"):
        raise NativeAnalysisUnavailable(f"{name} 必须为有限正数", code="INVALID_NATIVE_CONFIG")
    return int(parsed) if integer else float(parsed)


def _binary_path() -> Path | None:
    override = os.environ.get("AXILE_ANALYSIS_BINARY")
    if override is not None and not override.strip():
        raise NativeAnalysisUnavailable("AXILE_ANALYSIS_BINARY 不能为空", code="NATIVE_BINARY_MISSING")
    filename = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
    project = Path(__file__).resolve().parents[2]
    candidates = (
        [Path(override).expanduser()]
        if override
        else [
            Path(__file__).parent / "_native" / filename,
            project / "rust" / "analysis" / "target" / "release" / filename,
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    if override is not None:
        raise NativeAnalysisUnavailable("AXILE_ANALYSIS_BINARY 未指向可执行文件", code="NATIVE_BINARY_MISSING")
    return None


def _database_path(session: AsyncSession) -> Path | None:
    bind = session.get_bind()
    url = bind.engine.url
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:" or url.query.get("mode") == "memory":
        return None
    if database.startswith("file:"):
        parsed = urlsplit(database)
        if parsed.netloc not in ("", "localhost") or parsed.path == ":memory:":
            return None
        database = unquote(parsed.path)
    path = Path(database).expanduser().resolve()
    return path if path.is_file() else None


def get_native_analysis(session: AsyncSession, *, required: bool = False) -> NativeAnalysis | None:
    """为实际会话选择原生分析，兼容模式仅在运行条件不具备时返回空值.

    Parameters
    ----------
    session : AsyncSession
        数据库路径来自会话绑定，不能由 API 查询参数或连接凭据代替。
    required : bool, default=False
        要求原生执行；与 ``AXILE_ANALYSIS_MODE=required`` 相同。

    Returns
    -------
    NativeAnalysis or None
        文件型 SQLite 的原生入口；auto 模式缺少运行程序或使用内存库时为空。

    Raises
    ------
    NativeAnalysisUnavailable
        显式配置不能满足，或会话仍有未提交的 ORM 写入。
    """
    mode = os.environ.get("AXILE_ANALYSIS_MODE", "auto").lower()
    if mode not in {"auto", "python", "required"}:
        raise NativeAnalysisUnavailable(
            "AXILE_ANALYSIS_MODE 必须为 auto、python 或 required", code="INVALID_NATIVE_CONFIG"
        )
    if mode == "python" and not required:
        return None
    explicit = required or mode == "required" or "AXILE_ANALYSIS_BINARY" in os.environ
    binary = _binary_path()
    if binary is None:
        if explicit:
            raise NativeAnalysisUnavailable("原生分析程序尚未构建或安装", code="NATIVE_BINARY_MISSING")
        return None
    database = _database_path(session)
    if database is None:
        if explicit:
            raise NativeAnalysisUnavailable(
                "原生分析只支持已存在的文件型 SQLite 数据库", code="NATIVE_DATABASE_UNSUPPORTED"
            )
        return None
    state = session.sync_session
    if state.new or state.dirty or state.deleted:
        raise NativeAnalysisUnavailable("原生分析只能读取已提交事实，请先完成写入事务", code="UNCOMMITTED_NATIVE_INPUT")
    return NativeAnalysis(
        binary=binary,
        database=database,
        timeout=float(_positive_setting("AXILE_ANALYSIS_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT)),
        max_concurrency=int(_positive_setting("AXILE_ANALYSIS_MAX_CONCURRENCY", _DEFAULT_CONCURRENCY, integer=True)),
        max_output_bytes=int(_positive_setting("AXILE_ANALYSIS_MAX_OUTPUT_BYTES", _DEFAULT_OUTPUT_LIMIT, integer=True)),
    )


def _gate(concurrency: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gates = _GATES.setdefault(loop, {})
    return gates.setdefault(concurrency, asyncio.Semaphore(concurrency))


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks = bytearray()
    while chunk := await stream.read(min(64 * 1024, limit + 1 - len(chunks))):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise NativeAnalysisProtocolError("原生分析输出超过字节预算", code="NATIVE_OUTPUT_LIMIT")
    return bytes(chunks)


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def _process_error(stderr: bytes, returncode: int) -> NativeAnalysisError:
    try:
        value = json.loads(stderr)
    except (ValueError, UnicodeDecodeError):
        value = None
    if isinstance(value, dict):
        return NativeAnalysisError(
            str(value.get("error", "原生分析失败")), code=str(value.get("code", "NATIVE_ANALYSIS_ERROR"))
        )
    detail = stderr.decode("utf-8", errors="replace").strip()
    return NativeAnalysisError(detail or f"原生分析进程退出，状态 {returncode}")


@dataclass(frozen=True, slots=True)
class NativeAnalysis:
    """每次调用持有一个受限只读子进程，最终响应保持编码后的字节形式."""

    binary: Path
    database: Path
    timeout: float = _DEFAULT_TIMEOUT
    max_concurrency: int = _DEFAULT_CONCURRENCY
    max_output_bytes: int = _DEFAULT_OUTPUT_LIMIT

    async def query(self, command: str, **request_fields: Any) -> bytes:
        """一次性交接查询参数，直接返回 Rust 编码的 JSON 字节.

        Notes
        -----
        已提交事实由 Rust 自行扫描；不能把历史数据作为请求字段传入。
        超时包含等待并发名额的时间，取消后先回收子进程再释放名额。
        """
        request = json.dumps({**request_fields, "command": command}, ensure_ascii=False, allow_nan=False).encode()
        if len(request) > _REQUEST_LIMIT:
            raise NativeAnalysisProtocolError("原生分析只接收小型控制请求", code="NATIVE_REQUEST_LIMIT")
        return await self._invoke(("query", "--database", str(self.database)), request)

    async def capabilities(self) -> dict[str, Any]:
        """读取小型能力声明；仅能力响应在 Python 中解码."""
        try:
            value = json.loads(await self._invoke(("capabilities",), b"", timeout=min(self.timeout, 5)))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NativeAnalysisProtocolError("原生分析能力声明不是 JSON", code="INVALID_NATIVE_CAPABILITIES") from exc
        if not isinstance(value, dict):
            raise NativeAnalysisProtocolError("原生分析能力声明必须是对象", code="INVALID_NATIVE_CAPABILITIES")
        return value

    async def _invoke(self, args: tuple[str, ...], request: bytes, *, timeout: float | None = None) -> bytes:
        try:
            async with asyncio.timeout(self.timeout if timeout is None else timeout):
                async with _gate(self.max_concurrency):
                    return await self._run_process(args, request)
        except TimeoutError as exc:
            raise NativeAnalysisTimeout("原生分析超时，子进程已终止", code="NATIVE_ANALYSIS_TIMEOUT") from exc

    async def _run_process(self, args: tuple[str, ...], request: bytes) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.binary),
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise NativeAnalysisUnavailable("无法启动原生分析程序", code="NATIVE_BINARY_START_FAILED") from exc
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(_read_limited(process.stdout, self.max_output_bytes)),
            asyncio.create_task(_read_limited(process.stderr, _STDERR_LIMIT)),
        ]
        try:
            try:
                process.stdin.write(request)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # 提前拒绝请求的进程仍通过 stderr 返回稳定错误码。
                pass
            finally:
                process.stdin.close()
            stdout, stderr = await asyncio.gather(*readers)
            returncode = await process.wait()
            if returncode:
                raise _process_error(stderr, returncode)
            if not stdout.strip():
                raise NativeAnalysisProtocolError("原生分析未返回 JSON", code="NATIVE_EMPTY_RESPONSE")
            return stdout
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.shield(_kill_and_reap(process))


async def query_json(session: AsyncSession, command: str, **request_fields: Any) -> bytes:
    """要求原生执行一个查询，供明确选择 Rust 的调用方使用."""
    native = get_native_analysis(session, required=True)
    assert native is not None
    return await native.query(command, **request_fields)
