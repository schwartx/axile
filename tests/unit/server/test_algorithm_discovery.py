"""单层 Rust 预览、保存门禁与 Python 实际加载的衔接。"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.executor.algorithms.core import base
from axile.server import app as server_app
from axile.server.api.routes import algorithms


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(algorithms.router, prefix="/api/v1")
    return TestClient(app)


def _script(path: Path, name: str = "TEST-DIRECTORY-ALGORITHM") -> None:
    path.write_text(
        f"from axile import register_algorithm\n@register_algorithm({name!r})\ndef run(executor, data): pass\n",
        encoding="utf-8",
    )


def test_preview_does_not_execute_or_import_scripts(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed.txt"
    (tmp_path / "algo.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n"
        "raise RuntimeError('must not execute')\n@register_algorithm('PREV-VWAP-OPEN')\ndef opening(e, i): pass\n",
        encoding="utf-8",
    )
    payload = _client().get("/api/v1/algorithms/discover", params={"path": str(tmp_path)}).json()
    assert payload["discovery"] == {
        "files": 1,
        "algorithms": [{"name": "PREV-VWAP-OPEN", "function": "opening", "file": "algo.py"}],
        "issues": [],
        "can_use": True,
    }
    assert not sentinel.exists()


def test_preview_does_not_recurse_and_ignores_non_python_files(tmp_path: Path) -> None:
    nested = tmp_path / "src"
    nested.mkdir()
    _script(nested / "algo.py")
    _script(tmp_path / "ignored.txt")
    (tmp_path / "helper.py").write_text("# @register_algorithm('FAKE')\n", encoding="utf-8")
    payload = _client().get("/api/v1/algorithms/discover", params={"path": str(tmp_path)}).json()
    assert payload["entries"] == [{"name": "src", "path": str(nested)}]
    assert payload["discovery"] == {"files": 1, "algorithms": [], "issues": [], "can_use": False}
    child = _client().get("/api/v1/algorithms/discover", params={"path": str(nested)}).json()
    assert child["discovery"]["can_use"] is True


@pytest.mark.parametrize("source", [b"\xff", b"@register_algorithm(\n"])
def test_incomplete_scan_cannot_be_selected(tmp_path: Path, source: bytes) -> None:
    _script(tmp_path / "valid.py")
    (tmp_path / "bad.py").write_bytes(source)
    payload = _client().get("/api/v1/algorithms/discover", params={"path": str(tmp_path)}).json()
    assert payload["discovery"]["algorithms"][0]["name"] == "TEST-DIRECTORY-ALGORITHM"
    assert payload["discovery"]["can_use"] is False
    assert payload["discovery"]["issues"][0]["file"] == "bad.py"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 符号链接可能要求额外权限")
def test_file_symlinks_are_not_scanned(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _script(outside / "algo.py")
    (tmp_path / "link.py").symlink_to(outside / "algo.py")
    payload = _client().get("/api/v1/algorithms/discover", params={"path": str(tmp_path)}).json()
    assert payload["discovery"]["files"] == 0
    assert payload["discovery"]["can_use"] is False


def test_preview_reports_invalid_directory(tmp_path: Path) -> None:
    client = _client()
    assert client.get("/api/v1/algorithms/discover", params={"path": str(tmp_path / "missing")}).status_code == 404
    file = tmp_path / "file.txt"
    file.touch()
    assert client.get("/api/v1/algorithms/discover", params={"path": str(file)}).status_code == 400


@pytest.mark.parametrize("package", [False, True])
def test_startup_loads_declaration_file_with_empty_package_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: bool
) -> None:
    base.list_algorithms_metadata()
    monkeypatch.setattr(base, "_algorithm_registry", base._algorithm_registry.copy())
    directory = tmp_path / ("test_discovered_package" if package else "plain scripts")
    directory.mkdir()
    if package:
        (directory / "__init__.py").touch()
    _script(directory / "algo.py")
    (directory / "unused.py").write_text("raise RuntimeError('helper must not be imported')", encoding="utf-8")
    monkeypatch.setattr(server_app, "_algorithm_directories", [directory])
    try:
        assert server_app._load_configured_algorithm_directories() == [str(directory)]
        metadata = {meta.name: meta for meta in base.list_algorithms_metadata()}
        assert metadata["TEST-DIRECTORY-ALGORITHM"].func.__name__ == "run"
    finally:
        for name, module in list(sys.modules.items()):
            if str(getattr(module, "__file__", "")).startswith(str(directory)) or name == directory.name:
                sys.modules.pop(name, None)


def test_declared_name_is_not_a_successful_runtime_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "shadow.py").write_text(
        "def register_algorithm(name): return lambda function: function\n"
        "@register_algorithm('TEST-NOT-REGISTERED')\ndef run(e, i): pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server_app, "_algorithm_directories", [tmp_path])
    with pytest.raises(ValueError, match="未由.*成功注册"):
        server_app._load_configured_algorithm_directories()


def test_explicit_load_resolves_imported_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base.list_algorithms_metadata()
    monkeypatch.setattr(base, "_algorithm_registry", base._algorithm_registry.copy())
    (tmp_path / "names.py").write_text("NAME = 'CROSS-FILE-NAME'\n", encoding="utf-8")
    (tmp_path / "algo.py").write_text(
        "from axile import register_algorithm\nfrom .names import NAME\n"
        "@register_algorithm(NAME)\ndef run(e, i): pass\n",
        encoding="utf-8",
    )
    client = _client()
    discovery = client.get("/api/v1/algorithms/discover", params={"path": str(tmp_path)}).json()["discovery"]
    assert discovery["can_use"]
    assert discovery["algorithms"][0]["name"] == ""
    assert "CROSS-FILE-NAME" not in base._algorithm_registry
    response = client.post("/api/v1/algorithms/load", json={"directory": str(tmp_path), "file": "algo.py"})
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()] == ["CROSS-FILE-NAME"]
    assert (
        client.post("/api/v1/algorithms/load", json={"directory": str(tmp_path), "file": "names.py"}).status_code == 400
    )
