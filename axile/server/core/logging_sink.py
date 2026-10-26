"""Loguru 到进程内 Rust 日志队列的薄入口及后台显示、归档。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from axile.common.log_files import render_record, template_fields
from axile.common.logging import LOG_CONTEXT_FIELDS, current_log_context


def _rotation_limits(value: str) -> dict[str, int]:
    duration = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(seconds?|minutes?|hours?|days?|weeks?)\s*", value, re.IGNORECASE)
    if duration:
        seconds = (
            float(duration[1])
            * {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}[duration[2].lower().rstrip("s")]
        )
        return {"rotation_seconds": max(1, int(seconds))}
    size = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)(i?)(?:B|bytes?)\s*", value, re.IGNORECASE)
    if size:
        power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}[size[2].upper()]
        return {"max_bytes": max(1, int(float(size[1]) * (1024 if size[3] else 1000) ** power))}
    raise ValueError("日志轮转需要使用时长或文件大小，例如 1 day、100 MB")


def compress_log(path: Path) -> None:
    """验证 gzip 可完整还原后发布归档，再移除原文件；失败保留原件。"""
    temporary = path.with_name(path.name + ".gz.tmp")
    target = path.with_name(path.name + ".gz")
    created = False
    try:
        with (
            path.open("rb") as source,
            os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output,
        ):
            created = True
            with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
            output.flush()
            os.fsync(output.fileno())
        with path.open("rb") as original, gzip.open(temporary, "rb") as restored:
            if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(restored, "sha256").digest():
                raise ValueError("日志压缩校验失败")
        os.link(temporary, target)
        path.unlink()
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def event_payload(record: dict[str, Any]) -> dict[str, Any]:
    """将 Python 对象在调用线程中快照为纯 JSON 值，后台不保留 Python 引用。"""
    from axile.server.core.log_config import _component_label, _exception_payload

    extra = record["extra"]
    context, spans = current_log_context()
    context.update({field: extra[field] for field in LOG_CONTEXT_FIELDS if extra.get(field) is not None})
    args = dict(extra.get("_log_args", {"message": record["message"]}))
    exception = _exception_payload(record["exception"])
    if exception is not None:
        args["exception"] = exception
    template = extra.get("_log_template", "{message}")
    template_fields(template, list(args))
    return {
        "level": record["level"].name,
        "template": template,
        "fields": list(args),
        "args": list(args.values()),
        "metadata": {
            "component": _component_label(record.get("name") or record.get("module"), extra),
            "kind": extra.get("_log_kind", "event"),
            "source": {
                "name": record.get("name"),
                "file": record["file"].path,
                "function": record["function"],
                "line": record["line"],
            },
            "process": {"id": record["process"].id, "name": record["process"].name},
            "thread": {"id": record["thread"].id, "name": record["thread"].name},
        },
        "context": context,
        "spans": spans,
    }


class NativeLogSink:
    """只提交内存事件；控制台和压缩分别在后台执行。"""

    def __init__(self, directory: Path, level: str, rotation: str) -> None:
        self.core: Any = None
        self.config = (directory, level, rotation)
        self.closed = False
        self.local_dropped = 0
        self.counter_lock = threading.Lock()
        self.error: str | None = None
        self.stdout = sys.stdout
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        try:
            from axile.server._native import _core

            self.core = _core
            self.core.logging_start(str(directory), minimum=level, **_rotation_limits(rotation))
            run_id = json.loads(self.core.logging_stats())["run_id"]
        except Exception as error:
            self.error = str(error)
            return
        for name, poll, consume, failed in (
            ("console", self.core.logging_console, self._write_console, self.core.logging_console_failed),
            (
                "archive",
                self.core.logging_archive,
                lambda path: compress_log(Path(path)),
                self.core.logging_archive_failed,
            ),
        ):
            try:
                thread = threading.Thread(
                    target=self._consume,
                    args=(partial(poll, run_id), consume, partial(failed, run_id)),
                    name=f"axile-log-{name}",
                    daemon=True,
                )
                thread.start()
                self.threads.append(thread)
            except Exception as error:
                self.error = str(error)
                failed(run_id)

    def __call__(self, message: Any) -> None:
        """日志失败只更新计数，不改变调用方的结果。"""
        try:
            payload = json.dumps(
                event_payload(message.record), ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str
            )
            self.core.logging_emit(payload)
        except Exception:
            self.invalid()

    def invalid(self) -> None:
        """记录无法编码或转发的单个事件，避免递归调用 Loguru。"""
        try:
            self.core.logging_invalid()
        except Exception:
            with self.counter_lock:
                self.local_dropped += 1

    def stats(self) -> dict[str, Any]:
        """读取当前进程的累计计数，不经日志队列。"""
        result = (
            json.loads(self.core.logging_stats()) if self.core is not None else {"dropped_total": 0, "stopped": True}
        )
        with self.counter_lock:
            result["dropped_total"] += self.local_dropped
        if self.error:
            result["initialization_error"] = self.error
        return result

    def _consume(
        self, poll: Callable[[], str | None], consume: Callable[[str], None], failed: Callable[[], None]
    ) -> None:
        """两个独立消费者共用排空和停止规则，固定读取各自的运行。"""
        while True:
            payload = poll()
            if payload is None:
                if self.stop_event.is_set():
                    return
                self.stop_event.wait(0.02)
                continue
            try:
                consume(payload)
            except Exception:
                failed()

    def _write_console(self, payload: str) -> None:
        record = json.loads(payload)
        text = render_record(record, color=bool(self.stdout.isatty())) + "\n"
        try:
            descriptor = self.stdout.fileno()
        except (AttributeError, OSError):
            self.stdout.write(text)
            self.stdout.flush()
            return
        data = memoryview(text.encode(self.stdout.encoding or "utf-8"))
        while data:
            written = os.write(descriptor, data)
            if written <= 0:
                raise OSError("console write made no progress")
            data = data[written:]

    def stop(self) -> None:
        """正常退出限时排空；慢终端或压缩不延长业务清理。"""
        if self.closed:
            return
        self.closed = True
        try:
            if self.core is not None:
                self.core.logging_close(200)
        except Exception as error:
            self.error = str(error)
        self.stop_event.set()
        for thread in self.threads:
            thread.join(0.1)
