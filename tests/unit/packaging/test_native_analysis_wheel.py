from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from hatchling.builders.wheel import WheelBuilder


@pytest.fixture(autouse=True)
def native_process(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "allow_cargo": False,
    }

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        state["calls"].append(command)
        if command[0] == "cargo":
            assert state["allow_cargo"], "staging and wheel construction must never invoke Cargo"
            return subprocess.CompletedProcess(command, 0, "", "")
        assert kwargs["timeout"] == 20
        if command[0] == sys.executable:
            if state.get("extension_error"):
                raise subprocess.CalledProcessError(1, command, stderr=state["extension_error"])
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unexpected native process: {command}")

    monkeypatch.setattr(subprocess, "run", run)
    return state


@pytest.fixture
def wheel_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    repository = Path(__file__).resolve().parents[3]
    production_config = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    artifacts = production_config["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    (project / "axile").mkdir(parents=True)
    (project / "axile" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(repository / "hatch_build.py", project / "hatch_build.py")
    (project / ".gitignore").write_text("axile/server/_native/\naxile/server/_static/\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "axile"\nversion = "0.0.1"\nrequires-python = ">=3.12"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["axile"]\n'
        f"artifacts = {json.dumps(artifacts)}\n"
        '[tool.hatch.build.targets.wheel.hooks.custom]\npath = "hatch_build.py"\n',
        encoding="utf-8",
    )
    extension = project / "axile/server/_native" / ("_core.pyd" if os.name == "nt" else "_core.abi3.so")
    extension.parent.mkdir(parents=True)
    extension.write_bytes(b"extension fixture")
    return project


def build_wheel(project: Path) -> Path:
    output = project / "dist"
    output.mkdir()
    filename = next(WheelBuilder(str(project)).build(directory=str(output), versions=["standard"]))
    return output / filename


def test_missing_extension_rejects_wheel(wheel_project: Path) -> None:
    for extension in (wheel_project / "axile/server/_native").iterdir():
        extension.unlink()
    with pytest.raises(RuntimeError, match="Required native extension missing"):
        build_wheel(wheel_project)


@pytest.mark.parametrize("error", ["wrong architecture", "missing required entrypoints: dashboard_inputs"])
def test_incompatible_extension_rejects_wheel(wheel_project: Path, native_process: dict[str, Any], error: str) -> None:
    native_process["extension_error"] = error
    with pytest.raises(RuntimeError, match=error):
        build_wheel(wheel_project)


def test_wheel_probe_requires_business_freeze_queue_and_migration_entrypoints(
    wheel_project: Path, native_process: dict[str, Any]
) -> None:
    import ast

    build_wheel(wheel_project)
    probe = next(command[3] for command in native_process["calls"] if command[0] == sys.executable)
    required = next(
        ast.literal_eval(node.value)
        for node in ast.parse(probe).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "required" for target in node.targets)
    )
    assert {
        "freeze_execution_input",
        "freeze_execution_after_assets",
        "queue_enqueue",
        "queue_status",
        "queue_recover",
        "queue_claim",
        "queue_fail",
        "queue_release",
        "migration_current",
        "migration_check",
        "migration_upgrade",
        "migration_downgrade",
        "execution_notification_context",
    } <= set(required)


@pytest.mark.parametrize(
    ("head", "fact_schema", "missing", "error"),
    [
        ("0034", 40, None, None),
        ("0033", 40, None, "public migration head 0034"),
        ("0034", 39, None, "analysis fact schema 40"),
        ("0034", 40, "migration_upgrade", "missing required entrypoints"),
        ("0034", 40, "execution_notification_context", "missing required entrypoints"),
    ],
)
def test_wheel_probe_checks_migration_head_separately_from_fact_schema(
    wheel_project: Path,
    native_process: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    head: str,
    fact_schema: int,
    missing: str | None,
    error: str | None,
) -> None:
    import ast

    build_wheel(wheel_project)
    probe = next(command[3] for command in native_process["calls"] if command[0] == sys.executable)
    required = next(
        ast.literal_eval(node.value)
        for node in ast.parse(probe).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "required" for target in node.targets)
    )
    module = SimpleNamespace(ANALYSIS_FACT_SCHEMA=fact_schema, PUBLIC_HEAD=head)
    for name in required:
        if name != missing:
            setattr(module, name, lambda: None)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda spec: module)
    monkeypatch.setattr(sys, "argv", ["probe", "extension"])

    if error:
        with pytest.raises(RuntimeError, match=error):
            exec(probe, {})
    else:
        exec(probe, {})


def test_native_wheel_preserves_extension_and_excludes_retired_cli(wheel_project: Path) -> None:
    for name in ("axile-analysis", "axile-analysis.exe"):
        binary = wheel_project / "axile" / "server" / "_native" / name
        binary.write_bytes(b"retired CLI fixture")
        binary.chmod(0o755)
    wheel = build_wheel(wheel_project)
    assert "-cp312-abi3-" in wheel.name
    assert not wheel.name.endswith("-any.whl")
    assert "manylinux" not in wheel.name and "musllinux" not in wheel.name
    with zipfile.ZipFile(wheel) as archive:
        extension = "_core.pyd" if os.name == "nt" else "_core.abi3.so"
        assert archive.read(f"axile/server/_native/{extension}") == b"extension fixture"
        assert not any(Path(name).name in {"axile-analysis", "axile-analysis.exe"} for name in archive.namelist())
        assert "Root-Is-Purelib: false" in archive.read("axile-0.0.1.dist-info/WHEEL").decode()


def test_built_frontend_resources_are_preserved_with_production_artifact_rules(wheel_project: Path) -> None:
    static = wheel_project / "axile" / "server" / "_static"
    (static / "assets").mkdir(parents=True)
    resources = {
        "index.html": b'<script src="/assets/main.js"></script>',
        "assets/main.js": b'console.log("built UI")',
        "assets/main.css": b"body { color: black; }",
        "icons.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>',
    }
    for name, content in resources.items():
        (static / name).write_bytes(content)
    wheel = build_wheel(wheel_project)

    with zipfile.ZipFile(wheel) as archive:
        for name, content in resources.items():
            assert archive.read(f"axile/server/_static/{name}") == content


def load_build_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "build_native_analysis.py"
    spec = importlib.util.spec_from_file_location("build_native_analysis_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("build", [False, True])
def test_external_cargo_target_is_used_for_build_and_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_process: dict[str, Any], build: bool
) -> None:
    script = load_build_script()
    project = tmp_path / "project"
    external_target = tmp_path / "shared-target"
    profile = external_target / "debug"
    profile.mkdir(parents=True)
    for name in ("axile-analysis", "axile-analysis.exe"):
        (profile / name).write_bytes(b"retired CLI fixture")
    project.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[3] / "hatch_build.py", project / "hatch_build.py")
    library = {"win32": "axile_storage.dll", "darwin": "libaxile_storage.dylib"}.get(
        sys.platform, "libaxile_storage.so"
    )
    (profile / library).write_bytes(b"extension fixture")
    monkeypatch.setattr(script, "ROOT", project)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(external_target))
    arguments = ["build_native_analysis.py", "--debug"]
    if not build:
        arguments.append("--no-build")
    native_process["allow_cargo"] = build
    monkeypatch.setattr(sys, "argv", arguments)
    native = project / "axile" / "server" / "_native"

    script.main()

    extension = native / ("_core.pyd" if os.name == "nt" else "_core.abi3.so")
    assert extension.read_bytes() == b"extension fixture"
    assert {path.name for path in native.iterdir()} == {extension.name}
    assert not (project / "rust" / "storage" / "target").exists()
    cargo_calls = [command for command in native_process["calls"] if command[0] == "cargo"]
    if build:
        assert len(cargo_calls) == 1
        command = cargo_calls[0]
        assert command[command.index("--target-dir") + 1] == str(external_target)
        assert command[command.index("--manifest-path") + 1] == str(project / "rust" / "storage" / "Cargo.toml")
        assert "--locked" in command
        assert command[-3:] == ["--lib", "--features", "python"]
    else:
        assert not cargo_calls


def test_incompatible_staging_preserves_previous_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_process: dict[str, Any]
) -> None:
    script = load_build_script()
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[3] / "hatch_build.py", project / "hatch_build.py")
    native = project / "axile" / "server" / "_native"
    native.mkdir(parents=True)
    extension = native / ("_core.pyd" if os.name == "nt" else "_core.abi3.so")
    extension.write_bytes(b"previous valid extension")
    target = tmp_path / "target"
    (target / "debug").mkdir(parents=True)
    library = {"win32": "axile_storage.dll", "darwin": "libaxile_storage.dylib"}.get(
        sys.platform, "libaxile_storage.so"
    )
    (target / "debug" / library).write_bytes(b"incompatible extension")
    monkeypatch.setattr(script, "ROOT", project)
    monkeypatch.setattr(sys, "argv", ["build_native_analysis.py", "--debug", "--no-build", "--target-dir", str(target)])
    native_process["extension_error"] = "wrong architecture"

    with pytest.raises(RuntimeError, match="wrong architecture"):
        script.main()

    assert extension.read_bytes() == b"previous valid extension"
    assert list(native.iterdir()) == [extension]


def test_real_loader_rejects_invalid_extension(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    with pytest.raises(RuntimeError, match="Native extension is incompatible"):
        build_wheel(wheel_project)
