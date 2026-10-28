"""Validate native artifacts and tag wheels for the build host."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import sys_tags


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
if getattr(module, 'ANALYSIS_FACT_SCHEMA', None) != 40:
    raise RuntimeError('Native extension requires analysis fact schema 40')
if getattr(module, 'PUBLIC_HEAD', None) != '0036':
    raise RuntimeError('Native extension requires public migration head 0036')
required = (
    'Store', 'close_database', 'journal', 'activity_symbols', 'execution_records',
    'execution_record_detail', 'asset_snapshots',
    'account_performance', 'snapshot', 'query_costs', 'account_dashboard_context',
    'performance_result', 'status', 'chart',
    'execution_summary', 'execution_detail',
    'order_submissions', 'submission_monitor_json', 'publish_store',
    'persist_execution_state', 'freeze_execution_input', 'freeze_execution_after_assets',
    'queue_enqueue', 'queue_status', 'queue_recover', 'queue_claim', 'queue_fail', 'queue_release',
    'migration_current', 'migration_check', 'migration_upgrade', 'migration_downgrade',
    'execution_notification_context', 'normalize_execution_result',
    'list_execution_events_json', 'list_execution_artifacts_json',
    'account_public', 'connection_accounts', 'execution_read', 'supplement_has_execution_steps',
    'account_runtime_sync_json', 'notifications_get_system_json',
    'AccountControlOverride', 'EffectiveAccountControlPolicy',
    'control_registry', 'resolve_control_policy',
    'logging_start', 'logging_emit', 'logging_stats', 'logging_close',
    'logging_console', 'logging_archive', 'logging_span', 'logging_scope',
    'logging_invalid', 'logging_console_failed', 'logging_archive_failed',
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
    """Require the host-native PyO3 extension."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Include validated native artifacts with the crate's CPython 3.12 stable ABI tag."""
        native_dir = Path(self.root) / "axile" / "server" / "_native"
        extension = native_dir / ("_core.pyd" if os.name == "nt" else "_core.abi3.so")
        verify_native_extension(extension)
        build_data.setdefault("force_include", {})[str(extension)] = extension.relative_to(self.root).as_posix()
        # A local build has not been audited for manylinux/musllinux compliance.
        platform = next(tag.platform for tag in sys_tags() if not tag.platform.startswith(("manylinux", "musllinux")))
        build_data["pure_python"] = False
        build_data["tag"] = f"cp312-abi3-{platform}"
