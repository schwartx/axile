"""Exercise live imports and preserve the retired ledger as a test-only oracle."""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_analysis_runs_without_retired_wbt_packages():
    """Import the HTTP path and replay real observations with retired imports blocked."""
    root = Path(__file__).resolve().parents[3]
    script = dedent(
        """
        import importlib.abc
        import math
        import sys
        from datetime import datetime

        retired = {"wbt", "polars", "polars_runtime_32", "plotly"}

        class MissingWbtStack(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition(".")[0] in retired:
                    raise ModuleNotFoundError(fullname, name=fullname)

        sys.meta_path.insert(0, MissingWbtStack())

        from axile.server.api.routes.account_performance import router
        from axile.server.db.models.performance import PerformanceSettings
        from axile.server import performance
        from axile.server.performance_analysis import ENGINE_VERSION

        assert not hasattr(performance, "build_wbt_input")
        assert not hasattr(performance, "calculate_performance")
        assert not hasattr(performance, "Observation")
        assert not hasattr(performance, "observation")
        assert importlib.util.find_spec("axile.server.performance_replay") is None
        assert "tests.unit.server._performance_oracle" not in sys.modules
        assert "tests.unit.server._performance_costs_oracle" not in sys.modules
        assert ENGINE_VERSION == "equity-ledger-1"
        from tests.unit.server._performance_oracle import Observation, build_wbt_input, calculate_performance

        items = [
            Observation(1, "first", datetime(2026, 1, 1), 100.0, {"A": 1.0}, {"A": 100.0}),
            Observation(2, "second", datetime(2026, 1, 2), 110.0, {"A": 1.0}, {"A": 110.0}),
        ]
        settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0)
        result = calculate_performance(items, settings, "all")
        assert result.record_count == 2
        assert len(result.points) == 3
        assert math.isclose(result.points[-1].account_return, 0.1, abs_tol=1e-12)
        assert math.isclose(result.points[-1].portfolio_return, 0.1, abs_tol=1e-12)
        assert len(build_wbt_input(items).frame) == 2
        assert router.routes
        assert retired.isdisjoint(sys.modules)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
