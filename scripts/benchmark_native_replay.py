"""Measure isolated synthetic SQLite replay in fresh Python and Rust processes.

The fixture contains only normalized columns, not historical JSON. Both children
read that same committed database and produce all three public performance
ranges. Process peak RSS includes imports, loading, computation and serialization;
the API server and combined Python-plus-Rust deployment are deliberately absent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 20
RANGES = ("30", "90", "all")
TOLERANCE = 1e-12


def create_fixture(database: Path, count: int, symbol_count: int) -> None:
    """Write deterministic, complete quantity and price evidence without JSON."""
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE replay_observation (
                record_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
                execution_id TEXT, time TEXT NOT NULL, asset REAL,
                sizing_fallback INTEGER NOT NULL, target_present INTEGER NOT NULL,
                target_weight_present INTEGER NOT NULL, target_units_present INTEGER NOT NULL,
                target_quantities_present INTEGER NOT NULL
            );
            CREATE INDEX ix_replay_observation_account_time
                ON replay_observation(account_id,time,record_id);
            CREATE TABLE replay_observation_symbol (
                record_id INTEGER NOT NULL, symbol TEXT NOT NULL,
                target REAL, price REAL, target_weight REAL,
                target_units REAL, target_quantity REAL,
                PRIMARY KEY(record_id,symbol)
            );
            """
        )
        start = datetime(2025, 1, 1, 9)
        for index in range(count):
            asset = 100_000.0 + index * 0.5
            connection.execute(
                "INSERT INTO replay_observation VALUES (?,1,?,?,?,0,1,1,1,1)",
                (index + 1, f"synthetic-{index + 1}", (start + timedelta(hours=index)).isoformat(), asset),
            )
            rows = []
            for symbol_index in range(symbol_count):
                price = 100.0 + 10 * symbol_index + (index % 97) * 0.125
                quantity = 1.0 + ((index // 12 + symbol_index) % 3)
                rows.append(
                    (
                        index + 1,
                        f"S{symbol_index:02d}",
                        quantity * price / asset,
                        price,
                        0.5 / symbol_count,
                        quantity,
                        quantity,
                    )
                )
            connection.executemany("INSERT INTO replay_observation_symbol VALUES (?,?,?,?,?,?,?)", rows)


def python_child(database: Path) -> None:
    """Retain historical observations as the Python reference implementation does."""
    started = time.perf_counter()
    from axile.server.db.models.performance import PerformanceSettings
    from axile.server.performance import Observation, calculate_performance

    imported = time.perf_counter()
    request = json.load(sys.stdin)
    settings = PerformanceSettings.model_validate(request["settings"])
    observations = []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.executescript("PRAGMA cache_size=-4096; PRAGMA mmap_size=0; PRAGMA temp_store=FILE;")
        cursor = connection.execute(
            """
            SELECT o.record_id,o.execution_id,o.time,o.asset,
                s.symbol,s.target,s.price,s.target_weight,s.target_units,s.target_quantity
            FROM replay_observation o
            JOIN replay_observation_symbol s ON s.record_id=o.record_id
            WHERE o.account_id=1 ORDER BY o.time,o.record_id,s.symbol
            """
        )
        current = None
        for record_id, execution_id, when, asset, symbol, target, price, weight, units, quantity in cursor:
            if current is None or current.id != record_id:
                current = Observation(
                    id=record_id,
                    execution_id=execution_id,
                    time=datetime.fromisoformat(when),
                    asset=asset,
                    target={},
                    prices={},
                    target_weight={},
                    target_units={},
                    target_quantities={},
                )
                observations.append(current)
            current.target[symbol] = target
            current.prices[symbol] = price
            current.target_weight[symbol] = weight
            current.target_units[symbol] = units
            current.target_quantities[symbol] = quantity
    loaded = time.perf_counter()
    result = {key: calculate_performance(observations, settings, key) for key in RANGES}
    computed = time.perf_counter()
    encoded = json.dumps(
        {key: value.model_dump(mode="json") for key, value in result.items()},
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    finished = time.perf_counter()
    print(
        json.dumps(
            {
                "import_seconds": imported - started,
                "load_seconds": loaded - imported,
                "compute_seconds": computed - loaded,
                "serialize_write_seconds": finished - computed,
            }
        ),
        file=sys.stderr,
    )


def measure(arguments: list[str], request: bytes, directory: Path, name: str) -> tuple[dict, bytes]:
    """Collect per-child OS peak RSS with a hard timeout, outside the API process."""
    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONHASHSEED="0", OMP_NUM_THREADS="1")
    output = directory / f"{name}.json"
    errors = directory / f"{name}.stderr"
    started = time.perf_counter()
    with output.open("wb") as stdout, errors.open("wb") as stderr:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, env=environment)
        assert process.stdin is not None
        process.stdin.write(request)
        process.stdin.close()
        try:
            while True:
                pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                    break
                if time.perf_counter() - started > TIMEOUT_SECONDS:
                    raise TimeoutError(f"{name} exceeded {TIMEOUT_SECONDS} seconds")
                time.sleep(0.002)
        finally:
            if process.returncode is None:
                process.kill()
                _, status, _ = os.wait4(process.pid, 0)
                process.returncode = os.waitstatus_to_exitcode(status)
    elapsed = time.perf_counter() - started
    if process.returncode:
        raise RuntimeError(f"{name} exited {process.returncode}: {errors.read_text()[-2000:]}")
    payload = output.read_bytes()
    report = {
        "wall_seconds": elapsed,
        "peak_rss_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        "user_cpu_seconds": usage.ru_utime,
        "system_cpu_seconds": usage.ru_stime,
        "output_bytes": len(payload),
    }
    if name == "python":
        report["phases"] = json.loads(errors.read_text())
    return report, payload


def compare(actual: object, expected: object, path: str = "result") -> float:
    """Require identical fields, nulls and ordering; allow f64 rounding only."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            raise AssertionError(f"Different object keys at {path}")
        return max((compare(actual[key], value, f"{path}.{key}") for key, value in expected.items()), default=0.0)
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise AssertionError(f"Different array shape at {path}")
        return max(
            (compare(item, value, f"{path}[{index}]") for index, (item, value) in enumerate(zip(actual, expected))),
            default=0.0,
        )
    if isinstance(expected, float):
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isclose(actual, expected, rel_tol=TOLERANCE, abs_tol=TOLERANCE)
        ):
            raise AssertionError(f"Different floating-point value at {path}: {actual!r} != {expected!r}")
        return abs(actual - expected)
    if type(actual) is not type(expected) or actual != expected:
        raise AssertionError(f"Different value at {path}: {actual!r} != {expected!r}")
    return 0.0


def main() -> None:
    """Run one bounded synthetic comparison without touching a live database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "rust/analysis/target/release/axile-analysis")
    parser.add_argument("--observations", type=int, default=10_000, help="Synthetic observations, 1 to 10000")
    parser.add_argument("--symbols", type=int, default=10, help="Symbols per observation, 1 to 10")
    parser.add_argument("--output", type=Path, help="Also save the small measurement report as JSON")
    parser.add_argument("--python-child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.python_child is not None:
        python_child(args.python_child)
        return
    if not hasattr(os, "wait4"):
        parser.error("This OS must expose wait4 to measure per-process peak RSS")
    if not 1 <= args.observations <= 10_000 or not 1 <= args.symbols <= 10:
        parser.error("Use 1 to 10000 observations and 1 to 10 symbols")
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        parser.error("Build the Rust release binary first or pass --binary")
    request = json.dumps(
        {
            "command": "performance",
            "account_id": 1,
            "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002},
            "include_backtest": True,
        }
    ).encode()
    with tempfile.TemporaryDirectory(prefix="axile-native-replay-") as temporary:
        directory = Path(temporary)
        database = directory / "facts.sqlite"
        create_fixture(database, args.observations, args.symbols)
        python, expected = measure(
            [sys.executable, str(Path(__file__).resolve()), "--python-child", str(database)],
            request,
            directory,
            "python",
        )
        rust, actual = measure(
            [str(args.binary.resolve()), "query", "--database", str(database)], request, directory, "rust"
        )
        difference = compare(json.loads(actual), json.loads(expected))
        report = {
            "fixture": "synthetic_complete_normalized_sqlite",
            "observations": args.observations,
            "symbols_per_observation": args.symbols,
            "symbol_rows": args.observations * args.symbols,
            "database_bytes": database.stat().st_size,
            "ranges": RANGES,
            "python": python,
            "rust": rust,
            "parity": {"equal": True, "float_relative_absolute_tolerance": TOLERANCE, "max_abs_difference": difference},
            "memory_measurement": "per-child whole-process OS ru_maxrss; parent and API server excluded",
            "scope": "Fresh isolated children; startup, loading, three-range computation, JSON serialization included",
            "unmeasured": [
                "combined live API plus worker RSS",
                "API tail latency",
                "concurrent writes",
                "production history",
            ],
        }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
