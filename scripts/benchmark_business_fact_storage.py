"""Measure actual persisted business writes against one frozen synthetic fixture.

Run each checkout in its own process, using the same fixture and SQLite settings.
The default lifecycle scope includes target snapshots, pre-order audit, post-order
audit and the terminal writer. No executor or channel connection is created.
Schema migration, fixture construction and semantic verification are unmeasured.
WAL occupancy is reported as occupancy, never as cumulative write amplification.

Examples
--------
python scripts/benchmark_business_fact_storage.py freeze fixture.jsonl --executions 1000
python scripts/benchmark_business_fact_storage.py run fixture.jsonl old.sqlite old.json \
    --checkout /path/to/baseline --layout legacy --expected-commit 59dac8b
python scripts/benchmark_business_fact_storage.py run fixture.jsonl new.sqlite new.json \
    --checkout /path/to/candidate --layout typed --expected-commit CANDIDATE_SHA
"""

from __future__ import annotations

# The driver and isolated arms share this source-only evidence oracle.
try:
    from business_facts_contract import business_evidence
except ModuleNotFoundError:
    from scripts.business_facts_contract import business_evidence

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import select
import socket
import sqlite3
import statistics
import struct
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


@dataclass(frozen=True)
class DatasetSpec:
    """Describe an entirely deterministic, credential-free success dataset."""

    records: int = 1000
    symbols: int = 12
    orders_per_symbol: int = 2
    trades_per_order: int = 2
    result_inputs: bool = False

    def __post_init__(self) -> None:
        """Reject empty or invalid dimensions before creating any files."""
        for name in ("records", "symbols", "orders_per_symbol", "trades_per_order"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")


def encoded(value: object) -> bytes:
    """Return reproducible UTF-8 JSON while preserving numeric representations."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def semantic_hash(value: object) -> str:
    """Hash object semantics without exposing evidence in a benchmark report."""
    body = json.dumps(
        business_evidence(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _symbol_result(index: int, number: int, when: str, spec: DatasetSpec) -> dict:
    symbol = f"S{number:03d}.SYNTHETIC"
    price = 100.0 + number * 10 + (index % 97) * 0.125
    orders, trades = [], []
    for order_number in range(spec.orders_per_symbol):
        order_id = f"order-{index}-{number}-{order_number}"
        quantity = float(spec.trades_per_order)
        orders.append(
            {
                "order_id": order_id,
                "symbol": symbol,
                "direction": "BUY",
                "order_type": "LIMIT",
                "status": "FILLED",
                "volume": quantity,
                "price": price,
                "filled_volume": quantity,
                "avg_price": price + 0.01,
                "create_time": when,
                "update_time": when,
                "extra": {"exchange": "SYNTHETIC", "reduce_only": False},
            }
        )
        for trade_number in range(spec.trades_per_order):
            trades.append(
                {
                    "trade_id": f"trade-{index}-{number}-{order_number}-{trade_number}",
                    "order_id": order_id,
                    "symbol": symbol,
                    "trade_time": when,
                    "trade_volume": 1.0,
                    "trade_price": price + 0.01,
                    "trade_value": price + 0.01,
                    "extra": {"commission": 0.0 if trade_number == 0 else 0.01, "commission_asset": "CNY"},
                }
            )
    return {
        "symbol": symbol,
        "algorithm": "synthetic",
        "status": "SUCCEEDED",
        "error": None,
        "success": True,
        "outcome": "completed",
        "target_volume": float(spec.orders_per_symbol * spec.trades_per_order),
        "final_volume": float(spec.orders_per_symbol * spec.trades_per_order),
        "first_tick": {
            "symbol": symbol,
            "last_price": price,
            "bid_price": price - 0.01,
            "ask_price": price + 0.01,
            "bid_volume": 100.0,
            "ask_volume": 100.0,
            "update_time": when,
            "book_valid": True,
            "extra": {},
        },
        "sizing": {
            "symbol": symbol,
            "sizing_mode": "weight",
            "status": "SIZED",
            "reason_code": "SYNTHETIC",
            "strategy_weight": 0.5 / spec.symbols,
            "account_weight": 0.5 / spec.symbols,
            "equity": 100_000.0 + index,
            "reference_price": price,
            "unit_notional": price,
            "unit_multiplier": 1.0,
            "target_quantity": float(spec.orders_per_symbol * spec.trades_per_order),
        },
        "orders": orders,
        "trades": trades,
        "memory": {"synthetic_note": "固定合成观测", "empty": "", "zero": 0},
    }


def synthetic_record(index: int, spec: DatasetSpec) -> dict:
    """Build one record with targets, prices, orders, fills and asset observations.

    Normal current worker results exclude ``inputs``; historical repeated input
    is a separate fixture option rather than an inflated live-write baseline.
    """
    if not 0 <= index < spec.records:
        raise ValueError("record index is outside the declared fixture")
    when = (datetime(2025, 1, 1, 9) + timedelta(minutes=index)).isoformat()
    results = {f"S{number:03d}.SYNTHETIC": _symbol_result(index, number, when, spec) for number in range(spec.symbols)}
    targets = {symbol: 0.5 / spec.symbols for symbol in results}
    raw_input = {
        "channel_type": "ctp",
        "curr_target": targets,
        "last_target": {symbol: 0.4 / spec.symbols for symbol in results},
        "strategy_target": dict(targets),
        "algorithm": {"method": "synthetic", "params": {"max_wait_seconds": 1}},
        "symbol_algorithms": {},
        "trade_rules": {symbol: {"quantity_step": 1.0, "min_quantity": 1.0} for symbol in results},
        "futures_close_policy": {"mode": "auto"},
        "forbidden_symbols": [],
        "risk_symbols": [],
        "execution_timeout": 180,
        "extra": {"trigger_source": "synthetic", "execution_kind": "rebalance"},
    }
    positions = [
        {
            "symbol": symbol,
            "volume": item["final_volume"],
            "available_volume": item["final_volume"],
            "market_value": item["first_tick"]["last_price"] * item["final_volume"],
            "direction": "LONG",
            "avg_price": item["first_tick"]["last_price"],
            "extra": {"position_attributes": {"margin": 100.0 + number, "unrealized_profit": 0.0}},
        }
        for number, (symbol, item) in enumerate(results.items())
    ]
    assets = {
        "available_cash": 50_000.0,
        "total_asset": 100_000.0 + index,
        "market_value": sum(position["market_value"] for position in positions),
        "currency": "CNY",
        "update_time": when,
        "source": "real",
        "positions": positions,
        "extra": {"synthetic": True},
    }
    result = {
        "status": "SUCCEEDED",
        "task_status": "SUCCEEDED",
        "outcome": "completed",
        "outcome_reason": None,
        "error": None,
        "success": True,
        "execution_kind": "rebalance",
        "channel_type": "ctp",
        "execution_time": 0.25,
        "account_assets": assets,
        "symbol_results": results,
        "memory": {"synthetic": True},
        "extra": {},
    }
    if spec.result_inputs:
        result["inputs"] = raw_input
    return {
        "id": index + 1,
        "account_id": 1,
        "execution_id": f"synthetic-{index + 1}",
        "created_at": when,
        "is_success": 1,
        "raw_input": raw_input,
        "raw_result": result,
        "before_account_assets": {**assets, "available_cash": 50_010.0, "positions": []},
    }


def freeze_fixture(path: Path, spec: DatasetSpec) -> dict:
    """Write an immutable-by-hash JSONL fixture and a small adjacent manifest."""
    if path.exists() or path.with_suffix(path.suffix + ".manifest.json").exists():
        raise FileExistsError("refusing to overwrite an existing frozen fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    raw_bytes = 0
    with path.open("xb") as stream:
        for index in range(spec.records):
            record = synthetic_record(index, spec)
            body = encoded(record) + b"\n"
            stream.write(body)
            digest.update(body)
            raw_bytes += len(encoded(record["raw_input"])) + len(encoded(record["raw_result"]))
    manifest = {
        "schema_version": 1,
        "dataset": asdict(spec),
        "sha256": digest.hexdigest(),
        "fixture_file_bytes": path.stat().st_size,
        "terminal_raw_documents_utf8_bytes": raw_bytes,
        "counts": {
            "executions": spec.records,
            "symbols": spec.records * spec.symbols,
            "orders": spec.records * spec.symbols * spec.orders_per_symbol,
            "trades": spec.records * spec.symbols * spec.orders_per_symbol * spec.trades_per_order,
            "after_positions": spec.records * spec.symbols,
        },
    }
    path.with_suffix(path.suffix + ".manifest.json").write_bytes(encoded(manifest) + b"\n")
    return manifest


def iter_fixture(path: Path) -> Iterator[dict]:
    """Read only an intact frozen fixture, then yield its records."""
    manifest = json.loads(path.with_suffix(path.suffix + ".manifest.json").read_text())
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
    if digest.hexdigest() != manifest["sha256"]:
        raise ValueError("frozen fixture hash mismatch")
    with path.open("rb") as stream:
        yield from (json.loads(line) for line in stream)


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def inspect_storage(connection: sqlite3.Connection, path: Path) -> dict:
    """Count all data/index pages, residual JSON, occupied files and SQL rows."""
    geometry = {
        name: connection.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("page_size", "page_count", "freelist_count")
    }
    objects = []
    try:
        for row in connection.execute(
            "SELECT name,sum(pgsize),sum(payload),sum(unused),count(*),"
            "sum(pagetype='overflow') FROM dbstat GROUP BY name ORDER BY name"
        ):
            objects.append(
                dict(
                    zip(
                        ("name", "allocated_bytes", "payload_bytes", "unused_bytes", "pages", "overflow_pages"),
                        row,
                        strict=True,
                    )
                )
            )
        dbstat_status = "available"
    except sqlite3.OperationalError:
        dbstat_status = "unavailable"
    tables = {}
    for (table,) in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
        columns = connection.execute(f"PRAGMA table_xinfo({_identifier(table)})").fetchall()
        json_columns = [column[1] for column in columns if column[2].upper() == "JSON"]
        json_bytes = {}
        for column in json_columns:
            query = f"SELECT coalesce(sum(length(CAST({_identifier(column)} AS BLOB))),0) FROM {_identifier(table)}"
            json_bytes[column] = connection.execute(query).fetchone()[0]
        tables[table] = {
            "rows": connection.execute(f"SELECT count(*) FROM {_identifier(table)}").fetchone()[0],
            "json_utf8_bytes_by_column": json_bytes,
            "json_utf8_bytes": sum(json_bytes.values()),
        }
    schema_types = dict(connection.execute("SELECT name,type FROM sqlite_schema"))
    return {
        **geometry,
        "allocated_page_bytes": geometry["page_size"] * geometry["page_count"],
        "live_page_bytes": geometry["page_size"] * (geometry["page_count"] - geometry["freelist_count"]),
        "file_bytes": {
            suffix or "database": Path(str(path) + suffix).stat().st_size if Path(str(path) + suffix).exists() else 0
            for suffix in ("", "-wal", "-shm")
        },
        "dbstat_status": dbstat_status,
        "objects": [{**item, "type": schema_types.get(item["name"], "internal")} for item in objects],
        "tables": tables,
        "all_json_utf8_bytes": sum(item["json_utf8_bytes"] for item in tables.values()),
    }


def checkpoint_storage(connection: sqlite3.Connection, path: Path) -> dict:
    """Require a complete checkpoint before reporting durable main-file size."""
    busy, log_frames, checkpointed_frames = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise RuntimeError("checkpoint was blocked; storage comparison is incomplete")
    return {
        "checkpoint": {"busy": busy, "log_frames": log_frames, "checkpointed_frames": checkpointed_frames},
        **inspect_storage(connection, path),
    }


def analysis_fact_hashes(connection: sqlite3.Connection) -> dict:
    """Fingerprint persisted source/observation values outside measured writes."""
    result = {}
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND "
        "(name LIKE 'analysis_source_%' OR name LIKE 'replay_observation%') ORDER BY name"
    ):
        digest = hashlib.sha256()
        columns = connection.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
        keys = [column[1] for column in sorted(columns, key=lambda column: column[5]) if column[5]]
        order = ",".join(_identifier(key) for key in keys)
        query = f"SELECT * FROM {_identifier(table)}" + (f" ORDER BY {order}" if order else "")
        count = 0
        for row in connection.execute(query):
            digest.update(encoded(row))
            digest.update(b"\n")
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def latency_summary(samples: list[float]) -> dict:
    """Describe complete writer calls, including their actual commit latency."""
    if not samples:
        return {"calls": 0, "total_seconds": 0.0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(samples)
    return {
        "calls": len(samples),
        "total_seconds": sum(samples),
        "p50_ms": statistics.median(samples) * 1000,
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000,
        "max_ms": max(samples) * 1000,
    }


@contextmanager
def no_network():
    """Fail any accidental socket connection before a system call can occur."""

    def reject(*_args, **_kwargs):
        raise AssertionError("storage benchmark forbids outbound network access")

    with patch.object(socket.socket, "connect", reject), patch.object(socket.socket, "connect_ex", reject):
        yield


def prepare_database(path: Path, checkout: Path) -> None:
    """Install the checkout's full Alembic schema, indexes and triggers."""
    from axile.server._native import _core

    if callable(getattr(_core, "migration_upgrade", None)):
        from axile.server.db.models import Account, Portfolio
        from axile.server.native_store import native_operation_sync

        initialize_benchmark_database(path)
        portfolio = Portfolio(
            name="synthetic",
            market="期货",
            custom_calc_py_code="def calc(): return {}",
            created_at="2025-01-01T09:00:00",
            updated_at="2025-01-01T09:00:00",
        )
        native_operation_sync(
            "portfolio_operation", {"op": "create", "portfolio": portfolio.model_dump(mode="json")}, database=str(path)
        )
        account = Account(
            name="synthetic",
            market="期货",
            trade_channel="ctp",
            brokerage="synthetic",
            portfolio_id=1,
            backtest_weight_type="cs",
            backtest_fee_rate=0.0002,
            created_at="2025-01-01T09:00:00",
            updated_at="2025-01-01T09:00:00",
        )
        native_operation_sync(
            "account_create",
            {"account": account.model_dump(mode="json"), "now": "2025-01-01T09:00:00"},
            database=str(path),
        )
        return

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.orm import Session

    # Only historical SQLAlchemy checkouts reach this branch.
    from axile.server.db.models import Account, Portfolio, PortfolioAccount

    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        config = Config(str(checkout / "alembic.ini"))
        config.set_main_option("script_location", str(checkout / "axile/server/alembic"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with Session(engine) as session:
            session.add(
                Portfolio(
                    id=1,
                    name="synthetic",
                    market="期货",
                    custom_calc_py_code="def calc(): return {}",
                    created_at="2025-01-01T09:00:00",
                    updated_at="2025-01-01T09:00:00",
                )
            )
            session.flush()
            session.add(
                Account(
                    id=1,
                    name="synthetic",
                    market="期货",
                    trade_channel="ctp",
                    brokerage="synthetic",
                    portfolio_id=1,
                    backtest_weight_type="cs",
                    backtest_fee_rate=0.0002,
                    created_at="2025-01-01T09:00:00",
                    updated_at="2025-01-01T09:00:00",
                )
            )
            session.flush()
            session.add(PortfolioAccount(account_id=1, portfolio_id=1, created_at="2025-01-01T09:00:00"))
            session.commit()
    finally:
        engine.dispose()


class SyntheticExecutor:
    """Supply audit sequence numbers without loading any channel executor."""

    def __init__(self):
        self.seq = 0

    def next_audit_seq(self) -> int:
        """Allocate the next event occurrence for one synthetic execution."""
        self.seq += 1
        return self.seq


def write_lifecycle_audit(item: dict, *, before: bool) -> None:
    """Run real worker audit writers with frozen, connection-free observations."""
    from axile.common.trade_channel import TradeChannel
    from axile.executor.models.execution_result import ExecutionStatus
    from axile.server.execution.worker_backend import worker_audit

    account = SimpleNamespace(id=1, trade_channel=TradeChannel.CTP)
    executor = SyntheticExecutor()
    raw_input = item["raw_input"]
    standard_input = SimpleNamespace(**raw_input)
    standard_input.futures_close_policy = SimpleNamespace(model_dump=lambda: raw_input["futures_close_policy"])
    environment = {
        "captured_at": item["created_at"],
        "channel": "ctp",
        "algorithm": "synthetic",
        "executor_class": "SyntheticExecutor",
        "packages": {"synthetic": "1"},
    }
    with patch.object(worker_audit, "execution_environment", return_value=environment):
        if before:
            worker_audit._append_trade_pre_execute_audit(
                account=account,
                execution_id=item["execution_id"],
                algorithm_name="synthetic",
                trigger_source="synthetic",
                audit_input=raw_input,
                standard_input=standard_input,
                executor=executor,
                before_account_assets=item["before_account_assets"],
            )
        else:
            executor.seq = 3
            parameters = {
                "account": account,
                "execution_id": item["execution_id"],
                "algorithm_name": "synthetic",
                "output": SimpleNamespace(status=ExecutionStatus.SUCCEEDED, success=True),
                "result": item["raw_result"],
                "executor": executor,
                "include_trade_rule_snapshots": True,
                "audit_input": raw_input,
                "before_account_assets": item["before_account_assets"],
            }
            supported = inspect.signature(worker_audit._append_success_audit).parameters
            worker_audit._append_success_audit(**{key: value for key, value in parameters.items() if key in supported})


async def _write_target(item: dict, sessions) -> None:
    from axile.server.target_weight_snapshots import append_target_weight_snapshot

    options = {
        "portfolio_id": 1,
        "account_id": 1,
        "raw_weights": item["raw_input"]["strategy_target"],
        "normalized_weights": item["raw_input"]["curr_target"],
        "source": "execution",
        "execution_id": item["execution_id"],
    }
    if sessions is None:
        await append_target_weight_snapshot(**options)
        return
    async with sessions() as session:
        await append_target_weight_snapshot(session, **options)


async def _verify_records(sessions, items: list[dict], layout: str) -> str:
    if sessions is None:
        from axile.server._native import _core
        from axile.server.core.db import database_path

        with sqlite3.connect(database_path()) as connection:
            rows = {
                row[1]: {"id": row[0], "account_id": 1, "created_at": row[2]}
                for row in connection.execute("SELECT id,execution_id,created_at FROM executerecord")
            }
        digest = hashlib.sha256()
        for item in items:
            record = rows.get(item["execution_id"])
            if record is None or record["created_at"] != item["created_at"]:
                raise AssertionError("terminal record identity/timestamp was not preserved")
            detail = json.loads(_core.execution_record_detail(_core.Store(database_path()), 1, record["id"]))
            evidence = {"input": detail["input"], "result": detail["result"]}
            if semantic_hash(evidence) != semantic_hash({"input": item["raw_input"], "result": item["raw_result"]}):
                raise AssertionError("cold-path native evidence differs from the fixture")
            digest.update(semantic_hash(evidence).encode())
        return digest.hexdigest()

    import sqlalchemy as sa

    from axile.server.db.models import ExecuteRecord

    digest = hashlib.sha256()
    async with sessions() as session:
        for item in items:
            record = await session.scalar(
                sa.select(ExecuteRecord).where(ExecuteRecord.execution_id == item["execution_id"])
            )
            if record is None or record.created_at != item["created_at"]:
                raise AssertionError("terminal record identity/timestamp was not preserved")
            if layout == "legacy":
                evidence = {"input": record.raw_input, "result": record.raw_result}
            else:
                from axile.server.business_facts.repository import read_execution_detail

                detail = await read_execution_detail(session, record)
                evidence = {"input": detail["input"], "result": detail["result"]}
            expected = {"input": item["raw_input"], "result": item["raw_result"]}
            if semantic_hash(evidence) != semantic_hash(expected):
                raise AssertionError(f"cold-path evidence differs for record {record.id}")
            digest.update(semantic_hash(evidence).encode())
    return digest.hexdigest()


def artifact_business_evidence(artifact: object, content: dict) -> dict:
    """Compare hydrated business evidence independently of reference protocol v3."""
    body = {key: value for key, value in content.items() if key != "business_facts_ref"}
    # The legacy audit writer injects this empty parameter object although the
    # frozen input contains no sizing context. It contributes no business fact.
    kind = getattr(artifact, "artifact_type")
    if str(getattr(kind, "value", kind)).lower() == "target_snapshot" and body.get("sizing_context") == {}:
        body.pop("sizing_context")
    return {
        "execution_id": getattr(artifact, "execution_id"),
        "type": getattr(artifact, "artifact_type"),
        "content": body,
        "created_at": getattr(artifact, "created_at"),
    }


def event_business_evidence(event: object, details: object) -> dict:
    """Keep exact hydrated event fields while dropping the reference marker."""
    evidence = event.model_dump(mode="json")
    if isinstance(details, dict) and isinstance(details.get("decision"), dict):
        details = {
            **details,
            "decision": {key: value for key, value in details["decision"].items() if key != "business_facts_ref"},
        }
    return {**evidence, "details": details}


async def _lifecycle_hashes(sessions, layout: str) -> dict:
    """Compare hydrated audit evidence independently from physical references."""
    if sessions is None:
        from axile.server.core.db import database_path
        from axile.server.native_store import native_operation

        with sqlite3.connect(database_path()) as connection:
            executions = [row[0] for row in connection.execute("SELECT execution_id FROM executerecord ORDER BY id")]
        artifact_digest, event_digest = hashlib.sha256(), hashlib.sha256()
        for execution_id in executions:
            for operation, digest in (
                ("list_execution_artifacts", artifact_digest),
                ("list_execution_events", event_digest),
            ):
                page = await native_operation(operation, {"execution_id": execution_id, "limit": 500})
                if page["count"] != len(page["data"]):
                    raise AssertionError("Lifecycle oracle requires all audit rows, not a partial page")
                for row in page["data"]:
                    if operation == "list_execution_artifacts":
                        evidence = artifact_business_evidence(SimpleNamespace(**row), row["content"])
                    else:
                        evidence = event_business_evidence(
                            SimpleNamespace(model_dump=lambda **_kwargs: row), row["details"]
                        )
                    digest.update(semantic_hash(evidence).encode())
        return {"artifacts": artifact_digest.hexdigest(), "events": event_digest.hexdigest()}

    import sqlalchemy as sa

    from axile.server.db.models import ExecutionArtifact, ExecutionEvent

    artifact_digest = hashlib.sha256()
    event_digest = hashlib.sha256()
    async with sessions() as session:
        artifacts = (await session.scalars(sa.select(ExecutionArtifact).order_by(ExecutionArtifact.id))).all()
        for artifact in artifacts:
            content = artifact.content
            if layout == "typed":
                from axile.server.business_facts.artifacts import resolve_artifact_content

                content = await resolve_artifact_content(session, artifact, account_id=1)
            artifact_digest.update(semantic_hash(artifact_business_evidence(artifact, content)).encode())
        events = (await session.scalars(sa.select(ExecutionEvent).order_by(ExecutionEvent.id))).all()
        for event in events:
            details = event.details
            if layout == "typed":
                from axile.server.business_facts.artifacts import resolve_event_details

                details = await resolve_event_details(session, event, account_id=1)
            event_digest.update(semantic_hash(event_business_evidence(event, details)).encode())
    return {"artifacts": artifact_digest.hexdigest(), "events": event_digest.hexdigest()}


def source_manifest(checkout: Path) -> dict[str, str]:
    """Freeze Python, Rust, SQL, Cargo inputs and packaged native artifacts."""
    paths = {
        path
        for directory in (checkout / "axile", checkout / "rust/storage/src")
        for path in directory.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and (path.suffix in (".py", ".rs", ".sql", ".json", ".toml", ".ini") or "_native" in path.parts)
    }
    paths.update(
        checkout / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            "alembic.ini",
            "hatch_build.py",
            "rust/storage/Cargo.toml",
            "rust/storage/Cargo.lock",
            "rust/storage/build.rs",
        )
        if (checkout / name).is_file()
    )
    return {str(path.relative_to(checkout)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def source_hash(checkout: Path) -> str:
    """Identify all implementation inputs and the artifacts actually packaged."""
    return semantic_hash(source_manifest(checkout))


def harness_manifest(script: Path) -> dict[str, str]:
    """Freeze the shared measurement code as well as the invoked script."""
    paths = {script.resolve(), Path(__file__).resolve(), script.with_name("business_facts_contract.py").resolve()}
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def initialize_benchmark_database(path: Path) -> None:
    """Initialize only the candidate's isolated copy, before measured requests."""
    from axile.server.core import db

    engine = getattr(db, "engine", None)
    configured = engine.url.database if engine is not None else db.database_path()
    if not configured or Path(configured).resolve() != path.resolve():
        raise RuntimeError("Benchmark config points outside its isolated database copy")
    if engine is None:
        db.initialize_database()


def native_artifact(root: Path) -> dict:
    """Record the loaded extension, including its embedded bundled SQLite ID."""
    from axile.server._native import _core

    loaded_path = Path(_core.__file__).absolute()
    if not loaded_path.is_relative_to(root.resolve() / "axile"):
        raise RuntimeError("Native extension was loaded outside the requested checkout")
    path = loaded_path.resolve()
    digest, versions, source_ids = hashlib.sha256(), set(), set()
    previous = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            body = previous + chunk
            versions.update(
                value.rstrip(b"\0").decode()
                for value in re.findall(rb"(?<![0-9.])3\.[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]+)?\x00", body)
            )
            source_ids.update(
                value.decode() for value in re.findall(rb"20[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9:]{8} [a-f0-9]{64}", body)
            )
            previous = body[-128:]
    return {
        "path": str(path),
        "import_path": str(loaded_path),
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "analysis_fact_schema": getattr(_core, "ANALYSIS_FACT_SCHEMA", None),
        "capabilities": sorted(name for name in dir(_core) if not name.startswith("_")),
        "bundled_sqlite_version_strings": sorted(versions),
        "bundled_sqlite_source_ids": sorted(source_ids),
        "sqlite_version_method": "embedded extension strings; not a Python sqlite3 version or runtime SQL probe",
    }


def sqlite_settings(connection) -> dict:
    """Read actual connection settings; several PRAGMAs are connection-local."""
    cursor = connection.cursor()
    try:
        result = {}
        for name in (
            "journal_mode",
            "synchronous",
            "busy_timeout",
            "foreign_keys",
            "page_size",
            "cache_size",
            "mmap_size",
            "temp_store",
            "wal_autocheckpoint",
        ):
            cursor.execute(f"PRAGMA {name}")
            result[name] = cursor.fetchone()[0]
        return result
    finally:
        cursor.close()


def wal_commit_count(path: Path) -> int:
    """Count all writers' durable commit frames while a reader prevents WAL reset."""
    wal = Path(str(path) + "-wal")
    if not wal.exists() or not wal.stat().st_size:
        return 0
    with wal.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32 or struct.unpack(">I", header[:4])[0] not in (0x377F0682, 0x377F0683):
            raise AssertionError("Invalid WAL header")
        page_size = struct.unpack(">I", header[8:12])[0]
        if page_size == 1:
            page_size = 65536
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise AssertionError("Invalid WAL page size")
        commits = 0
        while frame := stream.read(page_size + 24):
            if len(frame) != page_size + 24 or frame[8:16] != header[16:24]:
                raise AssertionError("Incomplete or reset WAL; commit count is unavailable")
            commits += struct.unpack(">I", frame[4:8])[0] != 0
    return commits


@contextmanager
def pinned_wal_reader(path: Path):
    """Pin from another PID so separately linked SQLite libraries share locks."""
    program = (
        "import select,sqlite3,sys; "
        "connection=sqlite3.connect(sys.argv[1]); "
        "connection.execute('BEGIN'); "
        "connection.execute('SELECT count(*) FROM sqlite_schema').fetchone(); "
        "print('ready',flush=True); "
        "select.select([sys.stdin],[],[],25); connection.close()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if not select.select([process.stdout], [], [], 5)[0] or process.stdout.readline().strip() != "ready":
            raise RuntimeError("Independent WAL reader did not become ready")
        yield
        if process.poll() is not None:
            raise RuntimeError("Independent WAL reader expired before counting completed")
    finally:
        try:
            process.communicate("stop", timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def source_identity(checkout: Path, expected_commit: str, *, allow_dirty_smoke: bool = False) -> dict:
    """Accept a clean Git checkpoint or root's manifested read-only archive."""
    archive_manifest = checkout / "BASELINE_SOURCE.json"
    if not (checkout / ".git").exists():
        if not archive_manifest.is_file():
            raise ValueError("source has neither Git metadata nor a baseline archive manifest")
        body = archive_manifest.read_bytes()
        manifest = json.loads(body)
        commit = manifest.get("commit", "")
        archive_hash = manifest.get("archive_sha256", "")
        if (
            not isinstance(commit, str)
            or len(commit) != 40
            or not isinstance(archive_hash, str)
            or len(archive_hash) != 64
            or any(character not in "0123456789abcdef" for character in commit + archive_hash)
            or manifest.get("kind") != "git archive source snapshot"
        ):
            raise ValueError("baseline source archive manifest is invalid")
        identity = {
            "kind": "manifested_archive",
            "commit": commit,
            "archive_sha256": archive_hash,
            "manifest_sha256": hashlib.sha256(body).hexdigest(),
        }
    else:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True, timeout=5).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=checkout, text=True, timeout=5
        )
        if status.strip() and not allow_dirty_smoke:
            raise ValueError("formal measurement requires a clean frozen checkout; dirty checkout is smoke-only")
        identity = {"kind": "git_worktree", "commit": commit, "clean": not status.strip()}
    if not commit.startswith(expected_commit):
        raise ValueError("checkout commit differs from the requested frozen checkpoint")
    return identity


def _run_writes(items: list[dict], sessions, path: Path, scope: str) -> tuple[dict, int]:
    """Measure the unchanged lifecycle transaction sequence for every fixture row."""
    from axile.executor.algorithms.utils.clock import get_default_clock

    phases = {name: [] for name in ("target", "pre_order_audit", "post_order_audit", "terminal", "complete")}
    wal_peak = 0
    for item in items:
        fixed_time = datetime.fromisoformat(item["created_at"]).timestamp()
        with patch.object(get_default_clock(), "time", return_value=fixed_time):
            _write_fixture_row(item, sessions, scope, phases)
        wal = Path(str(path) + "-wal")
        wal_peak = max(wal_peak, wal.stat().st_size if wal.exists() else 0)
    return phases, wal_peak


def _write_fixture_row(item: dict, sessions, scope: str, phases: dict) -> None:
    from axile.server.execution import records

    start = time.perf_counter()
    if scope == "lifecycle":
        for phase, action in (
            ("target", lambda: asyncio.run(_write_target(item, sessions))),
            ("pre_order_audit", lambda: write_lifecycle_audit(item, before=True)),
            ("post_order_audit", lambda: write_lifecycle_audit(item, before=False)),
        ):
            phase_start = time.perf_counter()
            action()
            phases[phase].append(time.perf_counter() - phase_start)
    terminal_start = time.perf_counter()
    options = {"session_factory": sessions} if sessions is not None else {}
    asyncio.run(
        records._persist_execute_record(
            account_id=1,
            execution_id=item["execution_id"],
            raw_input=item["raw_input"],
            raw_result=item["raw_result"],
            is_success=1,
            **options,
        )
    )
    phases["terminal"].append(time.perf_counter() - terminal_start)
    phases["complete"].append(time.perf_counter() - start)


def _verify_lifecycle_counts(connection: sqlite3.Connection, records: int) -> None:
    for table, count in (("execution_artifact", 7), ("execution_event", 4), ("target_weight_snapshot", 1)):
        actual = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if actual != records * count:
            raise AssertionError(f"lifecycle persistence incomplete: {table}")


@contextmanager
def writer_connections(items: list[dict], path: Path, layout: str):
    """Adapt only the benchmark harness to each checkout's actual write API."""
    from axile.server.core import db

    commits, settings = Counter(), []
    if not hasattr(db, "SessionLocal"):
        yield None, commits, settings
        return

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server import execution_audit

    # Only historical SQLAlchemy checkouts reach this branch.
    from axile.server.db.models import ExecuteRecord, ExecutionArtifact, ExecutionEvent, TargetWeightSnapshot

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    db.register_sqlite_concurrency_pragmas(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created = {item["execution_id"]: item["created_at"] for item in items}

    def fixed_created(_mapper, _connection, target):
        for field in ("created_at", "calculated_at", "ts_local_created", "ts_persisted"):
            if hasattr(target, field):
                setattr(target, field, created[target.execution_id])

    def committed(_connection):
        commits["all"] += 1

    timestamp_models = (ExecuteRecord, ExecutionArtifact, ExecutionEvent, TargetWeightSnapshot)
    for model in timestamp_models:
        sa.event.listen(model, "before_insert", fixed_created)
    sa.event.listen(
        engine.sync_engine, "connect", lambda connection, _record: settings.append(sqlite_settings(connection))
    )
    sa.event.listen(engine.sync_engine, "commit", committed)
    execution_audit.SessionLocal = sessions
    if layout == "typed":
        from axile.server.execution import input_persistence

        input_persistence.SessionLocal = sessions
    try:
        yield sessions, commits, settings
    finally:
        for model in timestamp_models:
            sa.event.remove(model, "before_insert", fixed_created)
        asyncio.run(engine.dispose())


def run_measurement(args) -> dict:
    """Measure one frozen checkout; callers schedule old/new runs exclusively."""
    checkout = args.checkout.resolve()
    sys.path.insert(0, str(checkout))
    identity = source_identity(checkout, args.expected_commit, allow_dirty_smoke=args.allow_dirty_smoke)
    frozen_source_hash = source_hash(checkout)
    frozen_harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    harness_files = harness_manifest(Path(__file__))
    oracle_path = Path(__file__).with_name("business_facts_contract.py")
    oracle_hash = hashlib.sha256(oracle_path.read_bytes()).hexdigest()
    if args.database.exists() or args.report.exists():
        raise FileExistsError("refusing to overwrite measurement database or report")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    config = args.database.with_suffix(".config.toml")
    if config.exists():
        raise FileExistsError("refusing to overwrite an isolated benchmark config")
    config.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{args.database.resolve()}"\napp_log_dir = "{args.database.parent.resolve()}/{args.database.stem}-logs"\nenvironment = "local"\n'
    )
    os.environ["AXILE_CONFIG_TOML"] = str(config.resolve())
    items = list(iter_fixture(args.fixture))
    manifest = json.loads(args.fixture.with_suffix(args.fixture.suffix + ".manifest.json").read_text())
    prepare_database(args.database, checkout)

    from axile.server.execution import records  # noqa: F401

    actual_module = Path(sys.modules["axile.server.execution.records"].__file__).resolve()
    if not actual_module.is_relative_to(checkout):
        raise AssertionError("editable installation imported a different checkout")
    artifact = native_artifact(checkout)
    with writer_connections(items, args.database, args.layout) as (sessions, commits, writer_settings):
        with sqlite3.connect(args.database) as monitor:
            monitor.execute("PRAGMA journal_mode=WAL")
            monitor.execute("PRAGMA synchronous=FULL")
            empty = checkpoint_storage(monitor, args.database)
            observed_settings = sqlite_settings(monitor)
            mode = getattr(args, "mode", "timing")
            actual_commits = None
            if mode == "counters":
                # 独立进程持有读快照，避免两个 SQLite 库误判本进程持有的锁。
                with pinned_wal_reader(args.database):
                    phases, wal_peak = _run_writes(items, sessions, args.database, args.scope)
                    actual_commits = wal_commit_count(args.database)
                    if actual_commits < len(items):
                        raise AssertionError("WAL commit observer missed committed terminal writes")
            else:
                phases, wal_peak = _run_writes(items, sessions, args.database, args.scope)
            before_checkpoint = inspect_storage(monitor, args.database)
            after_checkpoint = checkpoint_storage(monitor, args.database)
            analysis_hashes = analysis_fact_hashes(monitor)
            artifact_versions = {
                str(version): count
                for version, count in monitor.execute(
                    "SELECT schema_version,count(*) FROM execution_artifact GROUP BY schema_version ORDER BY schema_version"
                )
            }
            violations = monitor.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise AssertionError("persisted facts contain foreign-key violations")
            if args.scope == "lifecycle":
                _verify_lifecycle_counts(monitor, len(items))
            evidence_hash = asyncio.run(_verify_records(sessions, items, args.layout))
            lifecycle_hashes = (
                asyncio.run(_lifecycle_hashes(sessions, args.layout)) if args.scope == "lifecycle" else None
            )
    if source_hash(checkout) != frozen_source_hash:
        raise RuntimeError("writer sources changed during measurement; results were not accepted")
    if source_identity(checkout, args.expected_commit, allow_dirty_smoke=args.allow_dirty_smoke) != identity:
        raise RuntimeError("writer checkpoint or archive manifest changed during measurement")
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != frozen_harness_hash:
        raise RuntimeError("benchmark harness changed during measurement")
    if harness_manifest(Path(__file__)) != harness_files:
        raise RuntimeError("shared benchmark harness changed during measurement")
    if hashlib.sha256(oracle_path.read_bytes()).hexdigest() != oracle_hash:
        raise RuntimeError("business oracle changed during measurement")
    report = {
        "oracle_sha256": oracle_hash,
        "schema_version": 1,
        "measurement_kind": "smoke" if args.allow_dirty_smoke else "frozen",
        "measurement_mode": mode,
        "checkout": str(checkout),
        "source_commit": identity["commit"],
        "source_identity": identity,
        "source_tree_sha256": frozen_source_hash,
        "harness_sha256": frozen_harness_hash,
        "harness_files": harness_files,
        "records_module": str(actual_module),
        "layout": args.layout,
        "scope": args.scope,
        "fixture": manifest,
        "sqlite_version": sqlite3.sqlite_version,
        "native_artifact": artifact,
        "sqlite_settings": {
            "monitor_connection": observed_settings,
            "sqlalchemy_writer_connections": writer_settings,
            "native_connection_settings": None,
            "native_connection_settings_scope": "Not observable through Python; connection-local values are not inferred from monitor PRAGMAs",
        },
        "writer_latency": {name: latency_summary(samples) for name, samples in phases.items()}
        if mode == "timing"
        else None,
        "actual_commits": actual_commits,
        "sqlalchemy_commit_events": commits["all"],
        "commit_count_method": "counter runs only: target database WAL commit frames after TRUNCATE; independent-PID reader prevents checkpoint/reset; includes native writers",
        "empty": empty,
        "before_checkpoint": before_checkpoint,
        "after_checkpoint": after_checkpoint,
        "wal_peak_occupied_bytes": wal_peak,
        "net_allocated_page_bytes": after_checkpoint["allocated_page_bytes"] - empty["allocated_page_bytes"],
        "net_allocated_bytes_per_execution": (after_checkpoint["allocated_page_bytes"] - empty["allocated_page_bytes"])
        / len(items),
        "cold_evidence_sha256": evidence_hash,
        "analysis_fact_hashes": analysis_hashes,
        "lifecycle_semantic_hashes": lifecycle_hashes,
        "artifact_storage_schema_versions": artifact_versions,
        "boundaries": [
            "synthetic success-only records; channel-specific and failed/malformed history tested separately",
            "no native result publication or analysis query timing",
            "no migration timing or vacuum; full production migrations create the initial schema",
            "WAL values are occupied file bytes, not cumulative bytes written",
            "environment observation is frozen; actual storage writers and transaction boundaries are retained",
            "Counter runs pin a read snapshot to prevent WAL checkpoint/reset; latency from these instrumented runs is not reported",
            "Python SQLite version and connection-local PRAGMAs do not describe Rust's bundled SQLite connections",
        ],
    }
    args.report.write_bytes(encoded(report) + b"\n")
    return report


def main() -> None:
    """Create fixture or measure exactly one explicitly frozen checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("fixture", type=Path)
    freeze.add_argument("--executions", type=int, default=1000)
    freeze.add_argument("--symbols", type=int, default=12)
    freeze.add_argument("--orders-per-symbol", type=int, default=2)
    freeze.add_argument("--trades-per-order", type=int, default=2)
    freeze.add_argument("--result-inputs", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("fixture", type=Path)
    run.add_argument("database", type=Path)
    run.add_argument("report", type=Path)
    run.add_argument("--checkout", type=Path, required=True)
    run.add_argument("--expected-commit", required=True)
    run.add_argument("--layout", choices=("legacy", "typed"), required=True)
    run.add_argument("--scope", choices=("terminal", "lifecycle"), default="lifecycle")
    run.add_argument("--mode", choices=("timing", "counters"), default="timing")
    run.add_argument("--allow-dirty-smoke", action="store_true")
    args = parser.parse_args()
    with no_network():
        if args.command == "freeze":
            manifest = freeze_fixture(
                args.fixture,
                DatasetSpec(
                    args.executions, args.symbols, args.orders_per_symbol, args.trades_per_order, args.result_inputs
                ),
            )
            print(json.dumps(manifest, ensure_ascii=False))
        else:
            report = run_measurement(args)
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in (
                            "source_commit",
                            "layout",
                            "scope",
                            "actual_commits",
                            "net_allocated_page_bytes",
                            "cold_evidence_sha256",
                        )
                    }
                )
            )


if __name__ == "__main__":
    main()
