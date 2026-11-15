"""核验固定新格式游标序列及其原始块读取放大；只读，不重放业务。"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from axile.server.execution_replay.reader import index, metadata


def main() -> None:
    """汇总浏览器实测与预先固定的目标集合；不包含基准工具自身的索引读取。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--account", required=True, type=int)
    parser.add_argument("--recording", required=True)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--browser-results", required=True, type=Path)
    parser.add_argument("--business-kind", choices=["trade", "weak_trade", "command", "position"])
    args = parser.parse_args()
    source = json.loads(args.targets.read_text())
    measured = json.loads(args.browser_results.read_text())
    targets = source["targets"]
    if len(targets) > 1024 or not measured["done"] or measured["error"] or len(measured["results"]) != len(targets):
        raise ValueError("benchmark_incomplete")
    with index(args.root, args.account, lambda: None) as (db, _, _):
        manifest = metadata(db, {"operation": "manifest", "execution_id": source["execution"]})
        recording = next(row for row in manifest["recordings"] if row["id"] == args.recording)
        stream = next(row["id"] for row in recording["streams"] if row["topic"] == source["quote_topic"])
    unique_blocks: dict[int, int] = {}
    for target in targets:
        with index(args.root, args.account, lambda: None) as (db, _, _):
            view = {
                "operation": "plan",
                "execution_id": source["execution"],
                "index_generation": manifest["index_generation"],
                "recording": recording["id"],
                "through_seq": recording["seq"],
                "commit_id": recording["commit_id"],
                "stream": stream,
                "time_us": target["time_us"],
                "capture_seq": target["capture_seq"],
            }
            plan = metadata(db, view)
            if plan["selected"]["seq"] != target["quote"]["seq"] or plan["evidence"][0]["seq"] != target["capture_seq"]:
                raise ValueError("benchmark_cursor_mismatch")
            unique_blocks.update((block["id"], block["length"]) for block in plan["blocks"])
            if args.business_kind:
                business_view = {**view, "cutoff_seq": plan["cutoff_seq"]}
                page = metadata(db, {**business_view, "operation": "objects", "kind": args.business_kind})
                objects = [subject["id"] for subject in page["subjects"]]
                if objects:
                    streams = metadata(db, {**business_view, "operation": "object-streams", "objects": objects})
                    for business_stream in streams["streams"]:
                        after = 0
                        while True:
                            blocks = metadata(
                                db,
                                {
                                    **business_view,
                                    "operation": "object-blocks",
                                    "objects": objects,
                                    "stream": business_stream,
                                    "after_block": after,
                                },
                            )
                            unique_blocks.update((block["id"], block["length"]) for block in blocks["blocks"])
                            if blocks["next"] is None:
                                break
                            after = blocks["next"]
    rows = measured["results"]
    read_bytes = sum(row["read"] for row in rows)
    unique_bytes = sum(unique_blocks.values())
    print(
        json.dumps(
            {
                "events": source["events"],
                "business_kind": args.business_kind,
                "targets": len(targets),
                "unique_required_blocks": len(unique_blocks),
                "D_bytes": unique_bytes,
                "R_total_bytes": read_bytes,
                "amplification": read_bytes / unique_bytes if unique_bytes else None,
                "p50_ms": statistics.median(row["ms"] for row in rows),
                "max_ms": max(row["ms"] for row in rows),
                "wasm_peak_bytes": max(row["wasm"] for row in rows),
                "cache_peak_bytes": max(row["cache"] for row in rows),
                "main_heap_start": measured["heapStart"],
                "main_heap_end": measured["heapEnd"],
                "progress": [
                    {"targets": stop, "read_bytes": sum(row["read"] for row in rows[:stop])}
                    for stop in (60, 120, 180, len(rows))
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
