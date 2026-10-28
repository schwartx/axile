"""Build and stage the host-native PyO3 extension."""

from __future__ import annotations

import argparse
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Compile with the checked-in lockfile, then verify the extension."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="Build the debug profile instead of release")
    parser.add_argument("--no-build", action="store_true", help="Only stage the already built host extension")
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
    manifest = ROOT / "rust" / "storage" / "Cargo.toml"
    target_dir = args.target_dir or manifest.parent / "target"
    target_dir = target_dir.expanduser()
    if not target_dir.is_absolute():
        target_dir = ROOT / target_dir
    if not args.no_build:
        command = ["cargo", "build", "--locked", "--manifest-path", str(manifest), "--target-dir", str(target_dir)]
        if not args.debug:
            command.append("--release")
        command.extend(["--lib", "--features", "python"])
        subprocess.run(command, check=True, cwd=ROOT, timeout=args.timeout)
    library = {"win32": "axile_storage.dll", "darwin": "libaxile_storage.dylib"}.get(
        sys.platform, "libaxile_storage.so"
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
    print(f"Staged native extension: {extension}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
