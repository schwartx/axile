"""动态算法模块发现与服务启动接线测试。"""

from pathlib import Path
from typing import cast

import pytest

from axile.server import app as server_app
from axile.server import main as server_main


def test_build_arg_parser_sets_default_algorithm_dir() -> None:
    """应使用 ``user_algorithms`` 作为默认 CLI 算法目录。"""
    parser = server_main.build_arg_parser()

    args = cast(object, parser.parse_args([]))

    assert getattr(args, "algorithm_dir") == "user_algorithms"
    assert getattr(args, "reload") is False
    assert getattr(args, "reload_dir") == []


def test_validate_host_rejects_wildcard_and_public_addresses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """通配地址（如 0.0.0.0）与任何非回环地址都应被拒绝。"""
    for host in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.1", "not-an-ip"):
        with pytest.raises(SystemExit) as exc_info:
            server_main._validate_host(host)  # pyright: ignore[reportPrivateUsage]
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "0.0.0.0" in captured.out


def test_run_server_rejects_wildcard_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_server 在收到 0.0.0.0 时应直接退出，避免对外暴露。"""
    algorithm_dir = tmp_path / "user_algorithms"
    algorithm_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        server_main.run_server(
            host="0.0.0.0",
            port=8000,
            algorithm_dir=algorithm_dir,
            workers=None,
        )

    captured = capsys.readouterr()

    assert exc_info.value.code == 1
    assert "0.0.0.0" in captured.out


def test_run_server_uses_import_string_when_reload_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """热重载模式应使用 import string，并把算法目录传给 reload worker。"""
    algorithm_dir = tmp_path / "algos"
    algorithm_dir.mkdir()
    extra_reload_dir = tmp_path / "extra"
    extra_reload_dir.mkdir()
    configs: list[dict[str, object]] = []

    def fake_config(*args: object, **kwargs: object) -> object:
        configs.append({"args": args, "kwargs": kwargs})
        return type("_FakeConfig", (), {"uds": None})()

    class _FakeServer:
        def __init__(self, config: object) -> None:
            return None

        def run(self) -> None:
            return None

    monkeypatch.setattr(server_main.uvicorn, "Config", fake_config)
    monkeypatch.setattr(server_main.uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(server_main, "setup_logging", lambda: None)
    monkeypatch.setattr(server_main, "initialize_database", lambda *_args, **_kwargs: None)

    # workers 固定为 1：多 worker 会让实盘互斥与调度失效，已在 run_server 入口
    # 被护栏拒绝（见 tests/unit/server/test_single_worker_guard.py）。本用例只
    # 关心 reload 模式下的 import string 与 reload_dirs 行为。
    server_main.run_server(
        host="127.0.0.1",
        port=8000,
        algorithm_dir=algorithm_dir,
        workers=1,
        reload=True,
        reload_dirs=[extra_reload_dir],
    )

    assert configs
    kwargs = cast(dict[str, object], configs[0]["kwargs"])
    assert configs[0]["args"] == ("axile.server.app:_app",)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8000
    assert kwargs["workers"] is None
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == [Path("axile"), algorithm_dir, extra_reload_dir]
    assert server_main.os.environ[server_app.ALGORITHM_DIRECTORIES_ENV] == str(algorithm_dir)


def test_load_configured_algorithm_modules_reraises_nested_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模块加载中的嵌套依赖错误应继续向上抛出。"""
    monkeypatch.setattr(server_app.settings, "algorithm_modules", ["user_algorithms"])

    def fake_loader(_modules: list[str]) -> None:
        raise ModuleNotFoundError(name="missing_dependency")

    monkeypatch.setattr(server_app, "load_algorithm_modules", fake_loader)

    with pytest.raises(ModuleNotFoundError) as exc_info:
        _ = server_app.load_configured_algorithm_modules()
    assert exc_info.value.name == "missing_dependency"
