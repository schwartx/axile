from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import pytest
from hatchling.builders.wheel import WheelBuilder


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
