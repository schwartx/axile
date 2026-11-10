"""选择 PR 验证范围；主分支和无法归类的改动保留全量验证。"""

import subprocess
import sys


def select_scope(paths: list[str]) -> dict[str, str]:
    """只缩小明确归属的改动，其余保留全量。"""
    if paths and all(path.startswith("ui/") for path in paths):
        return {"backend": "none", "ui": "true"}
    if paths and all(path.startswith(("axile/executor/", "tests/unit/executor/")) for path in paths):
        return {"backend": "executor", "ui": "false"}
    return {"backend": "full", "ui": "true"}


if __name__ == "__main__":
    scope = select_scope([])
    if len(sys.argv) == 3:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", "-z", f"{sys.argv[1]}...{sys.argv[2]}"]
        )
        scope = select_scope(changed.decode().rstrip("\0").split("\0"))
    for key, value in scope.items():
        print(f"{key}={value}")
