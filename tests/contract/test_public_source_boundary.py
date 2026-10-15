"""防止私有渠道词汇与资产示例渗回公共核心。"""

import re
import subprocess
from pathlib import Path


def _git_paths(root: Path, *args: str) -> set[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z", *args], cwd=root)
    return {root / item.decode() for item in output.split(b"\0") if item}


def _tracked_paths() -> tuple[Path, set[Path]]:
    """返回仓库根目录与当前可见的受版本管理文件。"""
    root = Path(__file__).parents[2]
    paths = _git_paths(root, "--cached", "--others", "--exclude-standard") - _git_paths(root, "--deleted")
    return root, paths


def test_tracked_source_has_no_private_channel_terms() -> None:
    """路径与文本均不得直接固化外部渠道名称."""
    root, paths = _tracked_paths()
    forbidden = ("bi" + "nance", "cryp" + "to")

    violations: list[str] = []
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        folded_path = relative.casefold()
        for term in forbidden:
            if term in folded_path:
                violations.append(f"{relative}: path")
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore").casefold()
        # 浏览器标准随机标识 API 不属于渠道声明；只豁免这一明确的成员访问。
        text = re.sub(rf"\b{re.escape(forbidden[1])}(?=\.randomuuid\b)", "", text)
        for term in forbidden:
            if term in text:
                violations.append(f"{relative}: content")

    assert not violations, "private channel terms found:\n" + "\n".join(violations)


def test_tracked_source_has_no_channel_specific_asset_examples() -> None:
    """源码、文档和测试中的示例资产必须属于公共市场。"""
    root, paths = _tracked_paths()
    asset_codes = (
        "B" + "TC",
        "E" + "TH",
        "S" + "OL",
        "B" + "NB",
        "X" + "RP",
        "D" + "OGE",
        "L" + "UNA",
        "A" + "DA",
        "A" + "VAX",
        "L" + "INK",
        "L" + "TC",
        "T" + "RX",
        "D" + "OT",
        "US" + "DT",
    )
    pattern = re.compile(rf"\b(?:{'|'.join(asset_codes)})(?:/?{'US' + 'DT'})?\b")
    violations: list[str] = []

    for path in sorted(paths):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if match := pattern.search(line):
                violations.append(f"{relative}:{line_number}: {match.group(0)}")

    assert not violations, "channel-specific asset examples found:\n" + "\n".join(violations)
