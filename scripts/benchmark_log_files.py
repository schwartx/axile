"""Benchmark disk log reading and rendering in fresh processes, with fixed synthetic data."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CASES = ("read_raw", "read_gzip", "timeline", "tree", "evidence")
SEED = 20261027


def canonical(value: object) -> bytes:
    """Normalize object key order while preserving arrays, values and text exactly."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def digest(path: Path) -> str:
    """Hash actual bytes, also warming the filesystem cache before measurements."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(directory: Path) -> dict:
    """Create three runs, two independent fragments each, and raw/gzip duplicate evidence."""
    manifest = directory / "dataset.json"
    if manifest.exists():
        data = json.loads(manifest.read_text())
        assert (
            data["sha256"]
            == hashlib.sha256(canonical({key: value for key, value in data.items() if key != "sha256"})).hexdigest()
        )
        for name, expected in data["files"].items():
            assert digest(directory / name) == expected["sha256"], f"Dataset changed: {name}"
        return data
    for name in ("raw", "gzip", "evidence"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    count = 0
    for run in range(3):
        base = (datetime(2026, 10, 27, tzinfo=UTC) + timedelta(microseconds=run * 41)).isoformat()
        definitions = [
            ["H", 1, {"run_id": f"fixture-{run}", "time_base": base, "time_unit": "us"}],
            ["T", 1, "INFO", "订单 {order} 数量 {quantity}", ["order", "quantity", "payload"], {"kind": "event"}],
            ["T", 2, "INFO", "{name}开始", ["name"], {"kind": "span_start"}],
            ["T", 3, "INFO", "{name}结束", ["name", "outcome"], {"kind": "span_end"}],
            ["T", 4, "ERROR", "订单 {order} 失败", ["order", "exception"], {"kind": "event"}],
            ["T", 5, "INFO", "日志统计 {stats}", ["stats"], {"log_stats": True}],
        ]
        for context in range(1, 5):
            definitions.append(
                [
                    "C",
                    context,
                    {
                        "account_id": 7 if context < 4 else 8,
                        "execution_id": f"bench-E{context}",
                        "account_name": "合成账户",
                        "channel": "ctp",
                        "symbol": ["SHFE.rb2610", "CZCE.CF701"][context % 2],
                    },
                ]
            )
            for depth in range(3):
                identity = (context - 1) * 3 + depth + 1
                definitions.append(["S", identity, identity - 1 if depth else None, f"步骤{depth}", context, 0])
        events: list[list] = []

        def event(template: int, context: int, span: int | None, args: list) -> None:
            sequence = len(events) + 1
            events.append(["E", sequence, sequence * 137, template, context, span, args])

        for identity in range(1, 13):
            event(2, (identity - 1) // 3 + 1, identity, [f"步骤{(identity - 1) % 3}"])
        for index in range(2000):
            context = rng.randrange(1, 5)
            span = context * 3 if index % 7 else None
            if index % 100 == 0:
                event(
                    4,
                    context,
                    span,
                    [
                        index,
                        {"type": "ValueError", "message": "合成错误", "traceback": "读取失败\nValueError: 合成错误\n"},
                    ],
                )
            else:
                event(
                    1,
                    context,
                    span,
                    [index, round(rng.random() * 100, 4), {"flags": [True, None, False], "note": '中文\n引号"'}],
                )
            if index == 999:
                event(5, 0, None, [{"dropped_total": run + 1, "written_total": index}])
        for identity in reversed(range(1, 13)):
            event(
                3, (identity - 1) // 3 + 1, identity, [f"步骤{(identity - 1) % 3}", "error" if identity == 6 else "ok"]
            )
        event(5, 0, None, [{"dropped_total": (run + 1) * 3, "written_total": len(events)}])
        count += len(events)
        midpoint = len(events) // 2
        for fragment, selected in enumerate((events[:midpoint], events[midpoint:])):
            name = f"axile-run{run}-{fragment}.jsonl"
            content = b"".join(canonical(row) + b"\n" for row in definitions + selected)
            compressed = gzip.compress(content, mtime=0)
            (directory / "raw" / name).write_bytes(content)
            (directory / "gzip" / (name + ".gz")).write_bytes(compressed)
            (directory / "evidence" / name).write_bytes(content)
            (directory / "evidence" / (name + ".gz")).write_bytes(compressed)
    legacy = [
        {
            "timestamp": f"2026-10-27T08:00:00.{index:06d}+08:00",
            "context": {"account_id": 7, "execution_id": "bench-E1"},
            "message": f"历史字典记录{index}",
            "level": "WARNING",
        }
        for index in range(20)
    ]
    content = b"".join(canonical(row) + b"\n" for row in legacy)
    (directory / "raw" / "axile-legacy.jsonl").write_bytes(content)
    (directory / "gzip" / "axile-legacy.jsonl.gz").write_bytes(gzip.compress(content, mtime=0))
    (directory / "evidence" / "axile-legacy.jsonl").write_bytes(content)
    files = {
        str(path.relative_to(directory)): {"sha256": digest(path), "bytes": path.stat().st_size}
        for name in ("raw", "gzip", "evidence")
        for path in sorted((directory / name).iterdir())
    }
    data = {"seed": SEED, "runs": 3, "fragments": 6, "events": count + len(legacy), "files": files}
    data["sha256"] = hashlib.sha256(canonical(data)).hexdigest()
    manifest.write_text(json.dumps(data, indent=2) + "\n")
    return data


def worker(case: str, directory: Path) -> dict:
    """Time cold production imports separately, then measure the same public entrypoints."""
    sys.path.insert(0, str(ROOT))
    time.tzset()
    started = time.perf_counter()
    from axile.common import log_files

    if case == "evidence":
        from axile.server.evidence.logs import collect_logs
        from axile.server.evidence.models import ExportBudget
    imported = time.perf_counter()
    assert Path(log_files.__file__).resolve().is_relative_to(ROOT)
    paths = sorted((directory / ("gzip" if case == "read_gzip" else "raw")).iterdir())
    snapshot = SimpleNamespace(
        account={"id": 7},
        iter_contexts=lambda: [SimpleNamespace(execution_id=f"bench-E{index}") for index in range(1, 4)],
    )
    expected_count = json.loads((directory / "dataset.json").read_text())["events"]
    entrypoint = "read_logs"
    started_work = time.perf_counter()
    if case == "evidence":
        rows, report = collect_logs(directory / "evidence", snapshot, ExportBudget())
        result = {"rows": rows, "report": report}
        count = len(rows)
        entrypoint = "collect_logs"
    elif case in ("timeline", "tree") and callable(getattr(log_files, "format_logs", None)):
        result = log_files.format_logs(paths, case)
        count = expected_count
        entrypoint = "format_logs"
    else:
        rows = log_files.read_logs(paths)
        count = len(rows)
        if case == "timeline":
            result = "\n".join(log_files.render_record(row) for row in rows)
        elif case == "tree":
            result = log_files.render_tree(rows)
        else:
            result = rows
    elapsed = time.perf_counter() - started_work
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    if case == "evidence":
        result["report"].pop("captured_at")
    return {
        "cold_import_seconds": imported - started,
        "work_seconds": elapsed,
        "peak_rss_bytes": rss,
        "events": count,
        "entrypoint": entrypoint,
        "output_sha256": hashlib.sha256(canonical(result)).hexdigest(),
    }


def source_hashes() -> dict[str, str]:
    """Record Python, Rust and native bytes and reject source drift during a run."""
    files = [
        ROOT / name
        for name in (
            "axile/common/log_files.py",
            "axile/server/evidence/logs.py",
            "axile/server/evidence/archive.py",
            "axile/server/evidence/models.py",
            "axile/executor/algorithms/utils/clock.py",
        )
    ]
    files += sorted((ROOT / "rust/storage/src").glob("log*.rs"))
    files += [path for path in (ROOT / "axile/server/_native").glob("_core.*") if path.suffix in (".so", ".pyd")]
    return {str(path.relative_to(ROOT)): digest(path) for path in files}


def main() -> None:
    """Run five independent processes per case and save comparable, immutable results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--directory", type=Path, default=ROOT / "dist/log-reader-benchmark-20261027")
    parser.add_argument(
        "--cpu", default=platform.processor(), help="CPU model when sandboxed host inspection is restricted"
    )
    parser.add_argument("--worker", choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.directory)))
        return
    output = args.directory / f"{args.label}.json"
    if output.exists():
        parser.error(f"Preserve previous results: {output}")
    data = prepare(args.directory)
    hashes = source_hashes()
    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONHASHSEED="0", TZ="UTC")
    results = {}
    started = time.perf_counter()
    for case in CASES:
        samples = []
        for _ in range(5):
            sample = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--label",
                    args.label,
                    "--directory",
                    str(args.directory),
                    "--worker",
                    case,
                ],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            samples.append(json.loads(sample.stdout))
        assert len({sample["output_sha256"] for sample in samples}) == 1, f"Unstable output: {case}"
        if case != "evidence":
            assert all(sample["events"] == data["events"] for sample in samples)
        results[case] = {
            "samples": samples,
            "median_seconds": statistics.median(sample["work_seconds"] for sample in samples),
            "median_cold_import_seconds": statistics.median(sample["cold_import_seconds"] for sample in samples),
            "median_peak_rss_bytes": statistics.median(sample["peak_rss_bytes"] for sample in samples),
            "max_peak_rss_bytes": max(sample["peak_rss_bytes"] for sample in samples),
            "output_sha256": samples[0]["output_sha256"],
        }
        (args.directory / f"{args.label}-{case}.json").write_text(json.dumps(results[case], indent=2) + "\n")
        print(case, json.dumps({key: value for key, value in results[case].items() if key != "samples"}), flush=True)
    assert results["read_raw"]["output_sha256"] == results["read_gzip"]["output_sha256"]
    assert hashes == source_hashes(), "Production sources or native artifact changed during measurement"
    report = {
        "label": args.label,
        "recorded_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "cpu": args.cpu,
        "cpu_count": os.cpu_count(),
        "dataset_sha256": data["sha256"],
        "dataset_events": data["events"],
        "source_hashes": hashes,
        "script_sha256": digest(Path(__file__).resolve()),
        "scope": "warm filesystem cache; fresh process/imports; work includes file IO, decompression/decoding and requested rendering/collection; SHA256 normalization outside timed work; RSS includes Python/imports through work completion; TZ=UTC",
        "elapsed_seconds": time.perf_counter() - started,
        "results": results,
    }
    baseline_path = args.directory / "python.json"
    if args.label != "python" and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
        assert baseline["dataset_sha256"] == data["sha256"], "Baseline used different data"
        report["comparison"] = {
            case: {
                "equal_output": results[case]["output_sha256"] == baseline["results"][case]["output_sha256"],
                "speedup": baseline["results"][case]["median_seconds"] / results[case]["median_seconds"],
            }
            for case in CASES
        }
    with output.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    if "comparison" in report:
        assert all(case["equal_output"] for case in report["comparison"].values()), (
            "Output differs from Python baseline"
        )


if __name__ == "__main__":
    main()
