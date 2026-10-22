"""Tag optional native-executable wheels for the build host."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def verify_native_extension(extension: Path) -> None:
    """Load the staged extension in isolation and reject incompatible native APIs."""
    if not extension.is_file():
        raise RuntimeError(f"Required native extension missing: {extension}; run scripts/build_native_analysis.py")
    probe = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('_core', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
required = (
    'Store', 'journal', 'activity_symbols', 'execution_records',
    'execution_record_detail', 'execution_business_detail', 'asset_snapshots',
    'account_performance', 'snapshot', 'query_costs', 'summaries', 'dashboard_inputs',
    'performance_result', 'status', 'publication_status', 'chart',
    'execution_summary', 'execution_detail', 'order_submission_summary',
    'order_submissions', 'submission_monitor', 'publish_store',
)
missing = [name for name in required if not callable(getattr(module, name, None))]
if missing:
    raise RuntimeError(f'Native extension missing required entrypoints: {missing}')
"""
    try:
        subprocess.run(
            [sys.executable, "-I", "-c", probe, str(extension.resolve())],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Native extension is incompatible with this build host/API: {error.stderr}") from error


class NativeAnalysisBuildHook(BuildHookInterface):
    """Require the host-native PyO3 extension and preserve the optional publishing CLI."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Include validated native artifacts with the crate's CPython 3.12 stable ABI tag."""
        executable = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
        native_dir = Path(self.root) / "axile" / "server" / "_native"
        binary = native_dir / executable
        foreign_name = "axile-analysis" if os.name == "nt" else "axile-analysis.exe"
        if (native_dir / foreign_name).exists():
            raise RuntimeError("Staged analysis executable targets a different platform; clean _native and rebuild")
        extension = native_dir / ("_core.pyd" if os.name == "nt" else "_core.abi3.so")
        verify_native_extension(extension)
        build_data.setdefault("force_include", {})[str(extension)] = extension.relative_to(self.root).as_posix()
        if binary.is_file():
            verify_native_capabilities(binary)
        # A local build has not been audited for manylinux/musllinux compliance.
        platform = next(tag.platform for tag in sys_tags() if not tag.platform.startswith(("manylinux", "musllinux")))
        build_data["pure_python"] = False
        build_data["tag"] = f"cp312-abi3-{platform}"
