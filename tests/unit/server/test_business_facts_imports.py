"""Verify standalone fact imports register a complete schema without import cycles."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.business_facts_contract import business_evidence


@pytest.mark.parametrize(
    "module", ["axile.server.business_facts.repository", "axile.server.business_facts.head", "axile.server.db.models"]
)
def test_fact_entrypoints_register_schema_in_fresh_interpreter(module):
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f'import {module}; from axile.server.db.models import SQLModel; import sqlalchemy as sa; SQLModel.metadata.create_all(sa.create_engine("sqlite://"))',
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert business_evidence(result.returncode) == business_evidence(0), result.stderr
