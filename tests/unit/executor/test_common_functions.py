from __future__ import annotations

import runpy
from datetime import datetime
from types import SimpleNamespace

import pytest

from axile.executor import common_functions
from axile.executor.algorithms.utils import clock as clock_module


def test_common_functions_module_main_prints_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        clock_module, "_default_clock", SimpleNamespace(time=lambda: datetime(2024, 3, 18, 10, 0).timestamp())
    )

    assert common_functions.__file__ is not None
    runpy.run_path(common_functions.__file__, run_name="__main__")

    captured = capsys.readouterr().out
    assert "当前是否为交易时间: True" in captured
    assert "common_functions.py 模块测试完成" in captured
