"""测试入口只增加交付包路径，柜台网络始终关闭。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axile.executor.algorithms.utils.clock import get_default_clock, set_default_clock  # noqa: E402

from support import OfflineClock  # noqa: E402


@pytest.fixture(autouse=True)
def offline_clock(monkeypatch):
    old = get_default_clock()
    clock = OfflineClock()
    set_default_clock(clock)
    # 离线回归一旦误走网络就失败，不依赖开发者环境是否可连柜台。
    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *a, **k: pytest.fail("离线回归禁止访问网络"))
    yield clock
    set_default_clock(old)
