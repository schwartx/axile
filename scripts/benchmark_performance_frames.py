"""Shared process sampling for cost, symbol and journal read benchmarks."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path


def emit(name, **values):
    """Emit bounded phase markers for the external sampler."""
    print(json.dumps(dict(phase=name, when=time.monotonic(), **values)), flush=True)


def observe(args, root, directory, *, worker_script, phase_names):
    """Sample total process RSS while retaining private diagnostic output."""
    import psutil

    directory.mkdir(mode=0o700)
    shutil.copy2(args.database, directory / "working.db")
    (directory / "working.db").chmod(0o600)
    config = directory / "config.toml"
    config.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{directory}/working.db"\napp_log_dir = "{directory}/logs"\nenvironment = "local"\n'
    )
    config.chmod(0o600)
    environment = dict(
        os.environ,
        AXILE_CONFIG_TOML=str(config),
        PYTHONPATH=str(root),
        PYTHONHASHSEED="0",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    samples = []
    with (directory / "process.log").open("w") as stream:
        started = time.monotonic()
        process = subprocess.Popen(
            [
                sys.executable,
                str(worker_script),
                str(root),
                str(args.database),
                str(directory),
                "--worker",
            ],
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        handle = psutil.Process(process.pid)
        while process.poll() is None:
            if time.monotonic() - started > 60:
                process.kill()
                process.wait()
                raise TimeoutError(str(directory))
            try:
                samples.append((time.monotonic(), handle.memory_info().rss))
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.005)
        if process.returncode:
            raise RuntimeError("Inspect private log: " + str(directory / "process.log"))
    phases = {}
    for line in (directory / "process.log").read_text().splitlines():
        if line.startswith('{"phase":'):
            row = json.loads(line)
            phases[row["phase"]] = row
    for name in phase_names:
        start, finish = phases[name + "_start"]["when"], phases[name + "_end"]["when"]
        phases[name + "_end"].update(
            seconds=finish - start, peak_bytes=max((rss for when, rss in samples if start <= when <= finish), default=0)
        )
    return phases


def require_legacy_fixture(source: Path) -> None:
    """Reject typed databases before a historical JSON fixture creates any output."""
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(executerecord)")}
    if not {"raw_input", "raw_result"} <= columns:
        raise ValueError("Legacy synthetic fixtures require an Axile <=0036 baseline checkout and database")
