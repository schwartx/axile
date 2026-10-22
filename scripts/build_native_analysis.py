"""Build and stage the host-native SQLite publishing CLI and PyO3 extension."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_capabilities(source: Path) -> None:
    """Reject executables that cannot publish the current native store protocol."""
    output = subprocess.run(
        [str(source), "capabilities"], check=True, capture_output=True, text=True, timeout=20
    ).stdout
    try:
        capabilities = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("Native analysis executable returned invalid capabilities") from error
    if (
        not isinstance(capabilities, dict)
        or type(capabilities.get("protocol")) is not int
        or capabilities["protocol"] != 1
        or not isinstance(capabilities.get("commands"), list)
        or "publish_store" not in capabilities["commands"]
    ):
        raise RuntimeError("Native analysis executable requires protocol 1 and command publish_store")


def stage_binary(source: Path, destination: Path) -> str:
    """Verify the executable and publish a complete copy into the package.

    Parameters
    ----------
    source
        Executable produced by Cargo for the current host.
    destination
        Package path discovered by the Python analysis adapter.

    Returns
    -------
    str
        Executable version reported by the smoke check.
    """
    if not source.is_file():
        raise FileNotFoundError(f"Native executable not found: {source}")
    verify_capabilities(source)
    version = subprocess.run(
        [str(source), "--version"], check=True, capture_output=True, text=True, timeout=20
    ).stdout.strip()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return version


def main() -> None:
    """Compile with the checked-in lockfile, then stage and verify the binary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="Build the debug profile instead of release")
    parser.add_argument("--no-build", action="store_true", help="Only stage the already built host executable and extension")
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=os.environ.get("CARGO_TARGET_DIR"),
        help="Cargo output directory (defaults to CARGO_TARGET_DIR or the crate target directory)",
    )
    parser.add_argument("--timeout", type=int, default=180, help="Cargo build time limit in seconds (default: 180)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    manifest = ROOT / "rust" / "analysis" / "Cargo.toml"
    target_dir = args.target_dir or manifest.parent / "target"
    target_dir = target_dir.expanduser()
    if not target_dir.is_absolute():
        target_dir = ROOT / target_dir
    if not args.no_build:
        command = ["cargo", "build", "--locked", "--manifest-path", str(manifest), "--target-dir", str(target_dir)]
        if not args.debug:
            command.append("--release")
        subprocess.run(command, check=True, cwd=ROOT, timeout=args.timeout)
        extension_command = command + ["--lib", "--features", "python"]
        subprocess.run(extension_command, check=True, cwd=ROOT, timeout=args.timeout)
    library = {"win32": "axile_analysis.dll", "darwin": "libaxile_analysis.dylib"}.get(
        sys.platform, "libaxile_analysis.so"
    )
    extension_source = target_dir / ("debug" if args.debug else "release") / library
    extension = ROOT / "axile" / "server" / "_native" / ("_core.pyd" if os.name == "nt" else "_core.abi3.so")
    extension.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = extension.parent / f".stage-{os.getpid()}"
    temporary_dir.mkdir()
    try:
        staged_extension = temporary_dir / extension.name
        shutil.copy2(extension_source, staged_extension)
        runpy.run_path(str(ROOT / "hatch_build.py"))["verify_native_extension"](staged_extension)
        staged_extension.replace(extension)
    finally:
        shutil.rmtree(temporary_dir)
    executable = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
    source = target_dir / ("debug" if args.debug else "release") / executable
    destination = ROOT / "axile" / "server" / "_native" / executable
    version = stage_binary(source, destination)
    print(f"Staged {version or executable}: {destination}")


if __name__ == "__main__":
    main()
