"""执行环境快照中的发行包归属与回退。"""

from __future__ import annotations

import hashlib
import json
from importlib import import_module, metadata
from pathlib import Path

import pytest

from axile.executor.execution_session import ExecutionSession
from axile.server.execution import environment


def test_builtin_executor_never_scans_installed_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "packages_distributions", lambda: pytest.fail("内置包无需扫描"))
    monkeypatch.setattr(environment, "package_version", lambda name: {"axile": "1.2.3"}[name])

    executor = object.__new__(ExecutionSession)
    snapshot = environment.execution_environment(executor, "ctp", "test")

    assert snapshot["packages"] == {"axile": "1.2.3"}
    assert snapshot["executor_module"] == ExecutionSession.__module__
    assert snapshot["implementation_sha256"]


@pytest.mark.parametrize("installation", ["wheel", "editable", "unverified"])
def test_registered_executor_package_requires_file_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installation: str
) -> None:
    module_name = f"test_executor_origin_{installation}"
    python_source = "class Executor:\n    pass\n"
    package = tmp_path / module_name
    package.mkdir()
    (package / "__init__.py").write_text(python_source)
    dist_name = f"test-executor-origin-{installation}"
    dist_info = tmp_path / f"test_executor_origin_{installation}-2.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Name: {dist_name}\nVersion: 2.0\n")
    (dist_info / "entry_points.txt").write_text(f"[axile.channels]\norigin-test = {module_name}:Executor\n")
    if installation == "wheel":
        (dist_info / "RECORD").write_text(f"{module_name}/__init__.py,,\n")
    elif installation == "editable":
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": tmp_path.as_uri(), "dir_info": {"editable": True}})
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    executor = import_module(module_name).Executor()
    (entry_point,) = metadata.entry_points(group="axile.channels", name="origin-test")
    monkeypatch.setattr(environment, "get_registered_channel_entry_point", lambda _channel: entry_point)
    monkeypatch.setattr(environment, "package_version", lambda name: {"axile": "1.0", "actual-provider": "3.0"}[name])
    if installation == "unverified":
        monkeypatch.setattr(environment, "packages_distributions", lambda: {module_name: ["actual-provider"]})
        expected = {"actual-provider": "3.0", "axile": "1.0"}
    else:
        monkeypatch.setattr(environment, "packages_distributions", lambda: pytest.fail("包文件归属已确认"))
        expected = {dist_name: "2.0", "axile": "1.0"}

    snapshot = environment.execution_environment(executor, "vendor", "test")

    assert snapshot["packages"] == expected
    assert snapshot["implementation_sha256"] == hashlib.sha256(python_source.encode()).hexdigest()
