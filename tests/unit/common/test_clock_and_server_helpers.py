"""服务端辅助模块测试。"""

from __future__ import annotations

import pytest

from axile.server.core import std_log_config


def test_setup_std_logging_returns_and_applies_log_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """应构造预期的日志配置并传给 logging.config.dictConfig。"""
    captured: dict[str, object] = {}

    monkeypatch.setattr(std_log_config.logging.config, "dictConfig", lambda config: captured.update(config))

    result = std_log_config.setup_std_logging()

    assert result["version"] == 1
    assert result["loggers"]["uvicorn.error"]["level"] == "INFO"
    assert captured == result
