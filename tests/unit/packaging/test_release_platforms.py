"""发行流程必须同时验证 Linux 和 Windows x86_64。"""

import runpy
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "runtime", [PurePosixPath('/tmp/release "check"'), PureWindowsPath(r"C:\Users\runner\Temp\发行验证")]
)
def test_release_verification_config_preserves_platform_paths(runtime):
    config = runpy.run_path(str(ROOT / "scripts/verify_release_install.py"))["runtime_config"](runtime)
    parsed = tomllib.loads(config)
    assert parsed["sqlalchemy_database_uri"] == f"sqlite+aiosqlite:///{(runtime / 'axile.db').as_posix()}"
    assert parsed["app_log_dir"] == str(runtime / "logs")


def test_release_requires_both_platforms_and_reuses_shared_ui():
    quality = yaml.safe_load((ROOT / ".github/workflows/code-quality.yml").read_text())
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    expected = {("ubuntu-latest", "linux-x86_64"), ("windows-latest", "windows-x86_64")}
    for job in (quality["jobs"]["native"], quality["jobs"]["smoke"], release["jobs"]["wheel"]):
        assert {(item["os"], item["platform"]) for item in job["strategy"]["matrix"]["include"]} == expected
        assert job["runs-on"] == "${{ matrix.os }}"
    assert quality["jobs"]["editor-ui"]["runs-on"] == "ubuntu-latest"
    assert "needs" not in quality["jobs"]["lint"]
    wheel_steps = release["jobs"]["wheel"]["steps"]
    reuse = next(step for step in wheel_steps if step.get("name", "").startswith("Reuse artifacts"))
    assert '--commit "$GITHUB_SHA"' in reuse["run"]
    assert "--name axile-web-ui" in reuse["run"]
    assert reuse["env"]["NATIVE_ARTIFACT"] == "axile-native-core-${{ matrix.platform }}"
    assert any(
        step.get("if") == "runner.os == 'Windows'" and "delvewheel repair" in step["run"] for step in wheel_steps
    )
    publish = release["jobs"]["release"]
    assert publish["needs"] == "wheel"
    check = next(step["run"] for step in publish["steps"] if step.get("name", "").startswith("Require both"))
    assert "len(wheels) == 2" in check and "-win_amd64.whl" in check and "manylinux" in check
    assert publish["steps"][-1]["if"] == "github.event_name == 'push'"
