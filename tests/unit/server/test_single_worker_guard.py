"""单 worker 部署护栏测试。

覆盖 issue #27：多 worker 会让账户执行互斥锁、定时调度器与实盘进度镜像
（均为进程内内存态）失效，可能重复调度与重复下单，因此必须在启动阶段拒绝。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from axile.server.core.single_worker import (
    MultiWorkerNotSupportedError,
    detect_configured_workers,
    ensure_single_worker,
    validate_workers_option,
)


class _FakeUvicornConfig:
    """伪装成 uvicorn.Config 的最小对象（护栏按类名 + workers 属性识别）。"""

    def __init__(self, workers: int | None) -> None:
        self.workers = workers


# 让类名与 uvicorn.Config 一致，模拟真实回溯场景
_FakeUvicornConfig.__name__ = "Config"


def _call_with_config_on_stack(config: Any, func: Any) -> Any:
    """在局部变量 ``config`` 存在的栈帧下调用 func，模拟 uvicorn 子进程栈。"""
    return func()


def test_validate_workers_option_allows_none() -> None:
    """未显式指定 --workers 时放行。"""
    validate_workers_option(None)


def test_validate_workers_option_allows_single() -> None:
    """--workers 1 是受支持的取值。"""
    validate_workers_option(1)


@pytest.mark.parametrize("workers", [2, 4, 16])
def test_validate_workers_option_rejects_multi(workers: int) -> None:
    """--workers > 1 必须被拒绝，且提示里带上具体取值。"""
    with pytest.raises(MultiWorkerNotSupportedError, match=str(workers)):
        validate_workers_option(workers)


@pytest.mark.parametrize("workers", [0, -1])
def test_validate_workers_option_rejects_non_positive(workers: int) -> None:
    """非正数是非法输入，静默当成 1 会让运维误以为参数生效。"""
    with pytest.raises(MultiWorkerNotSupportedError):
        validate_workers_option(workers)


def test_detect_configured_workers_reads_uvicorn_config() -> None:
    """能从调用栈上的 uvicorn.Config 读到 workers。"""
    config = _FakeUvicornConfig(workers=4)  # noqa: F841 - 供护栏回溯栈帧读取
    assert detect_configured_workers() == 4


def test_detect_configured_workers_returns_none_without_config(monkeypatch: Any) -> None:
    """栈上无 Config 且无环境变量时返回 None（不可确定）。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert detect_configured_workers() is None


def test_detect_configured_workers_reads_web_concurrency(monkeypatch: Any) -> None:
    """gunicorn 约定的 WEB_CONCURRENCY 可作为退化来源。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    assert detect_configured_workers() == 8


def test_detect_configured_workers_ignores_malformed_env(monkeypatch: Any) -> None:
    """WEB_CONCURRENCY 非法时不应炸掉启动。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "not-a-number")
    assert detect_configured_workers() is None


def test_ensure_single_worker_rejects_multi_worker() -> None:
    """探测到多 worker 时必须拒绝启动。"""
    config = _FakeUvicornConfig(workers=3)  # noqa: F841 - 供护栏回溯栈帧读取
    with pytest.raises(MultiWorkerNotSupportedError, match="3"):
        ensure_single_worker()


def test_ensure_single_worker_allows_single_worker() -> None:
    """单 worker 启动和热重载的 Config.workers 均为 1，必须放行。"""
    config = _FakeUvicornConfig(workers=1)  # noqa: F841 - 供护栏回溯栈帧读取
    ensure_single_worker()


def test_ensure_single_worker_allows_unknown(monkeypatch: Any) -> None:
    """探测不到配置时按单 worker 放行，宁可漏判也不误杀正常启动。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    ensure_single_worker()


def test_ensure_single_worker_rejects_gunicorn_concurrency(monkeypatch: Any) -> None:
    """gunicorn -w N（经 WEB_CONCURRENCY）同样被拦下。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(MultiWorkerNotSupportedError, match="4"):
        ensure_single_worker()


def test_run_server_exits_on_multi_worker(monkeypatch: Any) -> None:
    """CLI --workers > 1 必须以非零码退出，且不进入 uvicorn 启动流程。"""
    import axile.server.main as server_main

    started: list[object] = []
    monkeypatch.setattr(
        server_main.uvicorn,
        "Server",
        lambda **kwargs: started.append(kwargs),
    )

    with pytest.raises(SystemExit) as exc_info:
        server_main.run_server(host="127.0.0.1", port=8000, workers=4)

    assert exc_info.value.code == 1
    assert started == [], "拒绝多 worker 后不应继续构造 uvicorn Server"


def test_run_server_accepts_single_worker(monkeypatch: Any) -> None:
    """--workers 1 不应被护栏拦下。"""
    import axile.server.main as server_main

    monkeypatch.setattr(server_main, "is_configured", lambda: False)
    monkeypatch.setattr(server_main, "setup_logging", lambda: None)
    monkeypatch.setattr(server_main.uvicorn, "Config", lambda *a, **k: SimpleNamespace(uds=None))

    ran: list[bool] = []
    monkeypatch.setattr(
        server_main.uvicorn,
        "Server",
        lambda **kwargs: SimpleNamespace(run=lambda: ran.append(True)),
    )

    server_main.run_server(host="127.0.0.1", port=8000, workers=1)

    assert ran == [True]
