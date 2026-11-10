"""CI 范围不能将共享配置或未知路径误判为局部改动。"""

import runpy
from pathlib import Path

import pytest

select_scope = runpy.run_path(str(Path(__file__).parents[2] / "scripts/ci_scope.py"))["select_scope"]


@pytest.mark.parametrize(
    "paths,backend,ui",
    [
        ([], "full", "true"),
        (["ui/src/App.tsx", "ui/package.json"], "none", "true"),
        (
            ["axile/executor/ctp/ctp_execute.py", "tests/unit/executor/test_channel_market_data_submission.py"],
            "executor",
            "false",
        ),
        (["axile/executor/execution_session.py", "ui/src/App.tsx"], "full", "true"),
        (["axile/executor/ctp/ctp_execute.py", "tests/fixtures/algorithm_fakes.py"], "full", "true"),
        (["pyproject.toml"], "full", "true"),
        ([".github/workflows/code-quality.yml"], "full", "true"),
        (["axile/server/orders_live.py"], "full", "true"),
    ],
)
def test_scope(paths, backend, ui):
    assert select_scope(paths) == {"backend": backend, "ui": ui}
