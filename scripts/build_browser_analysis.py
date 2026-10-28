"""Build the browser analysis engine with the checked-in Rust lockfile."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDGEN_VERSION = "0.2.128"


def main() -> None:
    """Compile and generate browser bindings with matching tools."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    bindgen = shutil.which("wasm-bindgen")
    if (
        not bindgen
        or subprocess.check_output([bindgen, "--version"], text=True).strip() != f"wasm-bindgen {BINDGEN_VERSION}"
    ):
        parser.error(f"Install wasm-bindgen-cli {BINDGEN_VERSION} and rustup target add wasm32-unknown-unknown")
    workspace = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    default_target = Path(tempfile.gettempdir()) / f"axile-browser-wasm-{workspace}"
    target = Path(os.environ.get("CARGO_TARGET_DIR", str(default_target))).resolve()
    env = {**os.environ, "RUSTC_WRAPPER": "", "CARGO_INCREMENTAL": "0"}
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--target",
            "wasm32-unknown-unknown",
            "--manifest-path",
            str(ROOT / "rust/browser/Cargo.toml"),
            "--target-dir",
            str(target),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=args.timeout,
    )
    output = ROOT / "ui/src/generated/analysis"
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            bindgen,
            str(target / "wasm32-unknown-unknown/release/axile_browser.wasm"),
            "--target",
            "web",
            "--out-dir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        timeout=30,
    )
    print(f"Built browser analysis: {output}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
