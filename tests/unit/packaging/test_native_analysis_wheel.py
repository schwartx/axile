from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hatchling.builders.wheel import WheelBuilder


@pytest.fixture(autouse=True)
def native_process(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "capabilities": {"protocol": 1, "commands": ["publish_store"]},
        "calls": [],
        "allow_cargo": False,
    }

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        state["calls"].append(command)
        if command[0] == "cargo":
            assert state["allow_cargo"], "staging and wheel construction must never invoke Cargo"
            return subprocess.CompletedProcess(command, 0, "", "")
        assert kwargs["timeout"] == 20
        assert command[1:] in (["capabilities"], ["--version"])
        output = json.dumps(state["capabilities"]) if command[1] == "capabilities" else "axile-analysis 0.1.0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    return state


@pytest.fixture
def wheel_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "axile").mkdir(parents=True)
    (project / "axile" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(Path(__file__).resolve().parents[3] / "hatch_build.py", project / "hatch_build.py")
    (project / ".gitignore").write_text("axile/server/_native/\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "axile"\nversion = "0.0.1"\nrequires-python = ">=3.12"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["axile"]\n'
        'artifacts = ["axile/server/_native/axile-analysis", "axile/server/_native/axile-analysis.exe"]\n'
        '[tool.hatch.build.targets.wheel.hooks.custom]\npath = "hatch_build.py"\n',
        encoding="utf-8",
    )
    return project


def build_wheel(project: Path) -> Path:
    output = project / "dist"
    output.mkdir()
    filename = next(WheelBuilder(str(project)).build(directory=str(output), versions=["standard"]))
    return output / filename


def test_python_only_wheel_retains_portable_tag(wheel_project: Path) -> None:
    wheel = build_wheel(wheel_project)
    assert wheel.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert not any("/_native/" in name for name in archive.namelist())
        assert "Root-Is-Purelib: true" in archive.read("axile-0.0.1.dist-info/WHEEL").decode()


def test_native_wheel_is_platform_tagged_and_preserves_executable(wheel_project: Path) -> None:
    name = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
    binary = wheel_project / "axile" / "server" / "_native" / name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"native build fixture")
    binary.chmod(0o755)
    wheel = build_wheel(wheel_project)
    assert "-py3-none-" in wheel.name
    assert not wheel.name.endswith("-any.whl")
    assert "manylinux" not in wheel.name and "musllinux" not in wheel.name
    with zipfile.ZipFile(wheel) as archive:
        member = archive.getinfo(f"axile/server/_native/{name}")
        assert archive.read(member) == b"native build fixture"
        assert (member.external_attr >> 16) & 0o111
        assert "Root-Is-Purelib: false" in archive.read("axile-0.0.1.dist-info/WHEEL").decode()


def test_foreign_native_executable_is_rejected(wheel_project: Path) -> None:
    foreign = "axile-analysis" if os.name == "nt" else "axile-analysis.exe"
    binary = wheel_project / "axile" / "server" / "_native" / foreign
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"foreign build fixture")
    with pytest.raises(RuntimeError, match="different platform"):
        build_wheel(wheel_project)


@pytest.mark.parametrize(
    "capabilities",
    [
        {"protocol": 1, "commands": ["publish"]},
        {"protocol": 2, "commands": ["publish_store"]},
        {"protocol": True, "commands": ["publish_store"]},
        {"protocol": 1, "commands": "publish_store"},
        [],
    ],
)
def test_wheel_rejects_incompatible_native_capabilities(
    wheel_project: Path, native_process: dict[str, Any], capabilities: Any
) -> None:
    native_process["capabilities"] = capabilities
    name = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
    binary = wheel_project / "axile" / "server" / "_native" / name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"older native build fixture")
    with pytest.raises(RuntimeError, match="protocol 1 and command publish_store"):
        build_wheel(wheel_project)


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
    executable = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
    source = external_target / "debug" / executable
    source.parent.mkdir(parents=True)
    source.write_bytes(b"native build fixture")
    source.chmod(0o755)
    monkeypatch.setattr(script, "ROOT", project)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(external_target))
    arguments = ["build_native_analysis.py", "--debug"]
    if not build:
        arguments.append("--no-build")
    native_process["allow_cargo"] = build
    monkeypatch.setattr(sys, "argv", arguments)

    script.main()

    destination = project / "axile" / "server" / "_native" / executable
    assert destination.read_bytes() == source.read_bytes()
    assert not (project / "rust" / "analysis" / "target").exists()
    cargo_calls = [command for command in native_process["calls"] if command[0] == "cargo"]
    if build:
        assert len(cargo_calls) == 1
        command = cargo_calls[0]
        assert command[command.index("--target-dir") + 1] == str(external_target)
        assert "--locked" in command
    else:
        assert not cargo_calls


def test_incompatible_staging_preserves_previously_staged_binary(
    tmp_path: Path, native_process: dict[str, Any]
) -> None:
    script = load_build_script()
    source = tmp_path / "source"
    destination = tmp_path / "package" / "axile-analysis"
    source.write_bytes(b"incompatible new binary")
    destination.parent.mkdir()
    destination.write_bytes(b"previous valid binary")
    native_process["capabilities"] = {"protocol": 1, "commands": ["publish"]}

    with pytest.raises(RuntimeError, match="protocol 1 and command publish_store"):
        script.stage_binary(source, destination)

    assert destination.read_bytes() == b"previous valid binary"
