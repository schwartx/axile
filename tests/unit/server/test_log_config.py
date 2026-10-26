"""真实原生 sink 的格式、故障隔离与生命周期测试。"""

import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest
from loguru import logger

from axile.common.log_files import context_text, read_logs, render_record, render_tree
from axile.common.logging import current_log_context, log_span
from axile.server._native import _core
from axile.server.core import log_config
from axile.server.core.logging_sink import NativeLogSink
from axile.server.native_store import native_operation_sync


@pytest.fixture(autouse=True)
def isolated_logging(tmp_path, monkeypatch):
    log_config.shutdown_logging()
    monkeypatch.setattr(log_config.settings, "app_log_dir", tmp_path)
    monkeypatch.setattr(log_config.settings, "environment", "local")
    monkeypatch.setattr(log_config.settings, "axile_log_rotation", "1 day")
    monkeypatch.setattr(log_config, "setup_loguru_logging_intercept", lambda: None)
    yield
    log_config.shutdown_logging()
    logger.remove()


def test_context_label_preserves_current_business_labels():
    assert (
        context_text(
            {
                "account_id": 1,
                "account_name": "测试网",
                "channel": "ctp",
                "symbol": "CF2701",
                "execution_id": "0e7a80e92bc94d38",
            }
        )
        == "[测试网#1 ctp CF2701 0e7a80e9] "
    )


def test_setup_idempotent_and_real_native_roundtrip(tmp_path):
    log_config.setup_logging()
    identity = log_config.logging_stats()["run_id"]
    log_config.setup_logging()
    assert log_config.logging_stats()["run_id"] == identity
    logger.bind(account_id=7, execution_id="E1", component="执行").info("消息 {}", '中文\n"引号"')
    log_config.shutdown_logging()
    records = [row for row in read_logs(tmp_path.glob("*.jsonl")) if not row["metadata"].get("log_stats")]
    assert len(records) == 1 and records[0]["message"] == '消息 中文\n"引号"'
    assert records[0]["context"] == {"account_id": 7, "execution_id": "E1"}
    assert "[执行] [账户#7 E1]" in render_record(records[0])
    assert "\n  | " in render_record(records[0])
    stats = log_config.logging_stats()
    assert stats["written_total"] == stats["accepted_total"] == 1
    assert stats["dropped_total"] == 0


def test_exception_roundtrip_and_native_failure_keep_business_exception(tmp_path):
    log_config.setup_logging()
    with log_span("执行检查", account_id=7, execution_id="E1"):
        with pytest.raises(_core.NativeError) as caught:
            native_operation_sync(
                "account_get", {"account_id": 7}, database=str(tmp_path / "missing" / "database.sqlite")
            )
        assert caught.value.args[0] == "DATABASE_OPEN"
        try:
            raise ValueError("测试异常")
        except ValueError:
            logger.exception("读取失败")
    log_config.shutdown_logging()
    rows = read_logs(tmp_path.glob("*.jsonl"))
    native = next(row for row in rows if row["metadata"].get("source", {}).get("name") == "axile.native")
    assert native["context"]["execution_id"] == "E1" and len(native["spans"]) == 2
    assert dict(zip(native["fields"], native["args"], strict=True))["outcome"] == "error"
    exception = next(row for row in rows if row["exception"])
    assert exception["exception"]["type"] == "ValueError"
    assert "ValueError: 测试异常" in render_record(exception)
    assert "执行检查" in render_tree(rows) and "account_get" in render_tree(rows)
    assert dict(zip(native["fields"], native["args"], strict=True))["error"] in render_tree(rows)


def test_encoding_failure_counted_without_raising():
    log_config.setup_logging()
    logger.bind(account_id=float("nan")).info("不可编码上下文")
    stats = log_config.logging_stats()
    assert stats["dropped_total"] == stats["dropped_by_reason"]["invalid"] == 1


def test_unavailable_directory_does_not_raise_from_logging(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("occupied")
    sink = NativeLogSink(blocked, "INFO", "1 day")
    assert sink.error
    sink(SimpleNamespace(record={}))
    assert sink.stats()["dropped_total"] >= 1
    sink.stop()


def test_standard_logger_format_failure_isolated_and_counted():
    log_config.setup_logging()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "%d", (object(),), None)
    log_config.InterceptHandler().emit(record)
    assert log_config.logging_stats()["dropped_by_reason"]["invalid"] == 1


def test_closed_writer_counts_new_events():
    log_config.setup_logging()
    log_config.shutdown_logging()
    logger.info("关闭后消息")
    assert json.loads(_core.logging_stats())["dropped_by_reason"]["unavailable"] == 1


@pytest.mark.parametrize("rotation", ["100 MB", "1 KiB", "0.25 day"])
def test_rotation_configuration_keeps_logging_available(tmp_path, monkeypatch, rotation):
    monkeypatch.setattr(log_config.settings, "axile_log_rotation", rotation)
    log_config.setup_logging()
    logger.info("轮转配置有效")
    log_config.shutdown_logging()
    assert "initialization_error" not in log_config.logging_stats()
    rows = read_logs(list(tmp_path.glob("*.jsonl")) + list(tmp_path.glob("*.jsonl.gz")))
    assert any(row["message"] == "轮转配置有效" for row in rows)


def test_failed_console_thread_does_not_stop_business_or_file_logging(tmp_path, monkeypatch):
    original = threading.Thread.start

    def start(thread):
        if thread.name == "axile-log-console":
            raise RuntimeError("can't start new thread")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    log_config.setup_logging()
    logger.info("文件日志继续工作")
    log_config.shutdown_logging()
    assert log_config.logging_stats()["console_dropped_total"] >= 1
    assert any(row["message"] == "文件日志继续工作" for row in read_logs(tmp_path.glob("*.jsonl")))


def test_same_configuration_retries_after_directory_recovers(tmp_path, monkeypatch):
    directory = tmp_path / "recover"
    directory.write_text("blocked")
    monkeypatch.setattr(log_config.settings, "app_log_dir", directory)
    log_config.setup_logging()
    assert log_config.logging_stats()["initialization_error"]
    directory.unlink()
    log_config.setup_logging()
    logger.info("日志已恢复")
    log_config.shutdown_logging()
    assert "initialization_error" not in log_config.logging_stats()
    assert any(row["message"] == "日志已恢复" for row in read_logs(directory.glob("*.jsonl")))


@pytest.mark.parametrize("failure,outcome", [(ValueError("failed"), "error"), (asyncio.CancelledError(), "cancelled")])
def test_span_restores_context_and_original_failure(tmp_path, failure, outcome):
    log_config.setup_logging()
    with pytest.raises(type(failure)) as caught:
        with log_span("中断过程", account_id=7):
            assert current_log_context()[0] == {"account_id": 7}
            raise failure
    assert caught.value is failure
    assert current_log_context() == ({}, [])
    log_config.shutdown_logging()
    rows = read_logs(tmp_path.glob("*.jsonl"))
    end = next(row for row in rows if row["metadata"].get("kind") == "span_end")
    assert dict(zip(end["fields"], end["args"], strict=True))["outcome"] == outcome
