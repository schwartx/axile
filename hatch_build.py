"""Tag optional native-executable wheels for the build host."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import sys_tags


def verify_native_capabilities(binary: Path) -> None:
    """Ensure a staged worker supports the store publication contract."""
    output = subprocess.run(
        [str(binary), "capabilities"], check=True, capture_output=True, text=True, timeout=20
    ).stdout
    try:
        capabilities = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("Staged native analysis executable returned invalid capabilities") from error
    if (
        not isinstance(capabilities, dict)
        or type(capabilities.get("protocol")) is not int
        or capabilities["protocol"] != 1
        or not isinstance(capabilities.get("commands"), list)
        or "publish_store" not in capabilities["commands"]
    ):
        raise RuntimeError("Staged native analysis executable requires protocol 1 and command publish_store")


class NativeAnalysisBuildHook(BuildHookInterface):
    """Preserve pure wheels unless a host-native analysis executable is staged."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Include the executable with a platform tag and no Python ABI claim."""
        executable = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
        native_dir = Path(self.root) / "axile" / "server" / "_native"
        binary = native_dir / executable
        foreign_name = "axile-analysis" if os.name == "nt" else "axile-analysis.exe"
        if (native_dir / foreign_name).exists():
            raise RuntimeError("Staged analysis executable targets a different platform; clean _native and rebuild")
        if not binary.is_file():
            return
        verify_native_capabilities(binary)
        # A local build has not been audited for manylinux/musllinux compliance.
        platform = next(tag.platform for tag in sys_tags() if not tag.platform.startswith(("manylinux", "musllinux")))
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform}"
