"""``axile.server.core.log_config`` 日志格式与上下文助手测试."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from axile.server.core import log_config
from axile.server.core.log_config import (
    _context_label,
    _format_record,
    _json_format_record,
)


def _record(
    *,
    name: str | None = "axile.executor.algorithms.utils.order_tracker",
    extra: dict[str, object] | None = None,
    exception: object | None = None,
) -> dict[str, object]:
    """构造同时满足控制台与 JSON formatter 的最小 Loguru record."""

    class _Level:
        name = "INFO"

    return {
        "time": datetime.fromisoformat("2026-08-25T13:50:53.146+08:00"),
        "level": _Level(),
        "name": name,
        "module": "order_tracker",
        "message": "定时查询完成: pending=1",
        "extra": extra or {},
        "file": SimpleNamespace(path="/workspace/order_tracker.py"),
        "function": "_query_pending_orders",
        "line": 571,
        "process": SimpleNamespace(id=1234, name="MainProcess"),
        "thread": SimpleNamespace(id=5678, name="ctp-symbol-algo_0"),
        "exception": exception,
    }


def test_context_label_renders_human_readable_context() -> None:
    """账户名称、ID、渠道、symbol 与短 execution ID 应紧凑呈现."""
    label = _context_label(
        {
            "account_id": 1,
            "account_name": "测试网",
            "channel": "ctp",
            "symbol": "CF2701",
            "execution_id": "0e7a80e92bc94d38",
        }
    )
    assert label == "[测试网#1 ctp CF2701 0e7a80e9] "


def test_console_format_uses_component_without_source_location() -> None:
    """控制台只显示业务组件，不显示模块、函数或行号."""
    record = _record(extra={"account_id": 1})
    template = _format_record(record)  # type: ignore[arg-type]

    assert record["extra"]["_component"] == "订单"  # type: ignore[index]
    assert record["extra"]["_ctx"] == "[账户#1] "  # type: ignore[index]
    assert "{extra[_component]}" in template
    assert "{name}" not in template
    assert "{function}" not in template
    assert "{line}" not in template


def test_json_format_serializes_exception_traceback() -> None:
    """异常 JSON 应包含类型、消息与 traceback."""
    try:
        raise ValueError("测试异常")
    except ValueError as exc:
        exception = SimpleNamespace(type=type(exc), value=exc, traceback=exc.__traceback__)

    record = _record(exception=exception)
    _json_format_record(record)  # type: ignore[arg-type]
    payload = json.loads(record["extra"]["_json"])  # type: ignore[index]

    assert payload["exception"]["type"] == "ValueError"
    assert payload["exception"]["message"] == "测试异常"
    assert "ValueError: 测试异常" in payload["exception"]["traceback"]


def test_setup_logging_writes_versioned_jsonl_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """服务启动应停止创建文本日志，并配置 UTF-8 JSONL sink."""

    class _Logger:
        added: list[tuple[object, dict[str, object]]]

        def __init__(self) -> None:
            self.added = []

        def configure(self, **kwargs: object) -> None:
            _ = kwargs

        def info(self, message: object) -> None:
            _ = message

        def add(self, sink: object, **kwargs: object) -> None:
            self.added.append((sink, kwargs))

    fake_logger = _Logger()
    monkeypatch.setattr(log_config, "logger", fake_logger)
    monkeypatch.setattr(log_config, "setup_loguru_logging_intercept", lambda: None)
    monkeypatch.setattr(log_config.settings, "app_log_dir", tmp_path)
    monkeypatch.setattr(log_config.settings, "axile_log_rotation", "1 day")
    monkeypatch.setattr(log_config.settings, "environment", "local")

    log_config.setup_logging()

    assert len(fake_logger.added) == 1
    sink, options = fake_logger.added[0]
    assert sink == tmp_path / "axile.jsonl"
    assert options["format"] is log_config._json_format_record
    assert options["rotation"] == "1 day"
    assert options["encoding"] == "utf-8"
    assert not (tmp_path / "axile.log").exists()
