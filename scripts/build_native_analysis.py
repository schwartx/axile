"""Build and stage the host-native SQLite analysis executable for a wheel."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    parser.add_argument("--no-build", action="store_true", help="Only stage an already built host executable")
    parser.add_argument("--timeout", type=int, default=180, help="Cargo build time limit in seconds (default: 180)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    manifest = ROOT / "rust" / "analysis" / "Cargo.toml"
    target_dir = manifest.parent / "target"
    if not args.no_build:
        command = ["cargo", "build", "--locked", "--manifest-path", str(manifest), "--target-dir", str(target_dir)]
        if not args.debug:
            command.append("--release")
        subprocess.run(command, check=True, cwd=ROOT, timeout=args.timeout)
    executable = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
    source = target_dir / ("debug" if args.debug else "release") / executable
    destination = ROOT / "axile" / "server" / "_native" / executable
    version = stage_binary(source, destination)
    print(f"Staged {version or executable}: {destination}")


if __name__ == "__main__":
    main()
