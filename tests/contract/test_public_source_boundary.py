"""防止私有渠道词汇与资产示例渗回公共核心。"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

_FROZEN_CODEC_PATH = "tests/fixtures/rust_store/result_orders_trades.json"
_FROZEN_CODEC_SHA256 = "79b04e1053dacd992d34237d0283c96013cebac3b16551e711eae707594c47a0"
_FROZEN_CODEC_SOURCE = "Python order and trade codec oracle at Axile 509b3627ca5fc6ee5f5a72e91b5ebebed3cf9879"


def _is_frozen_codec_oracle(relative: str, content: bytes) -> bool:
    """只识别带确切来源和内容摘要的迁移前编解码基线。"""
    return (
        relative == _FROZEN_CODEC_PATH
        and hashlib.sha256(content).hexdigest() == _FROZEN_CODEC_SHA256
        and json.loads(content).get("source") == _FROZEN_CODEC_SOURCE
    )


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
        if relative == "rust/storage/Cargo.lock":
            # 通用摘要算法依赖只按 Cargo 的完整包名或依赖项识别，其余文本继续扫描。
            digest_dependency = re.escape("cryp" + "to-common")
            text = re.sub(rf'(?m)^(?:name = "{digest_dependency}"| "{digest_dependency}",)$', "", text)
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
        content = path.read_bytes()
        if _is_frozen_codec_oracle(relative, content):
            continue
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if match := pattern.search(line):
                violations.append(f"{relative}:{line_number}: {match.group(0)}")

    assert not violations, "channel-specific asset examples found:\n" + "\n".join(violations)


def test_digest_dependency_exception_only_matches_exact_lock_identifiers(tmp_path, monkeypatch) -> None:
    """依赖名豁免不覆盖相似包名、私有渠道、注释或普通源码。"""
    dependency = "cryp" + "to-common"
    private_channel = "bi" + "nance"
    lock = tmp_path / "rust/storage/Cargo.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f'[[package]]\nname = "{dependency}"\ndependencies = [\n "{dependency}",\n]\n')
    monkeypatch.setattr(f"{__name__}._tracked_paths", lambda: (tmp_path, {lock}))
    test_tracked_source_has_no_private_channel_terms()

    for text in (
        f'name = "{dependency}-channel"',
        f'description = "{dependency}"',
        f'# {private_channel}\nname = "{dependency}"',
        f'name = "{private_channel}"',
    ):
        lock.write_text(text)
        with pytest.raises(AssertionError, match="private channel terms found"):
            test_tracked_source_has_no_private_channel_terms()

    source = tmp_path / "example.py"
    source.write_text(f'name = "{dependency}"')
    monkeypatch.setattr(f"{__name__}._tracked_paths", lambda: (tmp_path, {source}))
    with pytest.raises(AssertionError, match="private channel terms found"):
        test_tracked_source_has_no_private_channel_terms()


def test_frozen_codec_exception_requires_exact_path_and_bytes(tmp_path, monkeypatch) -> None:
    """历史基线只豁免资产示例；改动内容、其他路径和私有渠道仍会失败。"""
    content = (Path(__file__).parents[2] / _FROZEN_CODEC_PATH).read_bytes()
    oracle = tmp_path / _FROZEN_CODEC_PATH
    oracle.parent.mkdir(parents=True)
    oracle.write_bytes(content)
    monkeypatch.setattr(f"{__name__}._tracked_paths", lambda: (tmp_path, {oracle}))
    test_tracked_source_has_no_channel_specific_asset_examples()
    test_tracked_source_has_no_private_channel_terms()

    oracle.write_bytes(content + b"\n")
    with pytest.raises(AssertionError, match="channel-specific asset examples found"):
        test_tracked_source_has_no_channel_specific_asset_examples()

    other = oracle.with_name("another_codec.json")
    other.write_bytes(content)
    monkeypatch.setattr(f"{__name__}._tracked_paths", lambda: (tmp_path, {other}))
    with pytest.raises(AssertionError, match="channel-specific asset examples found"):
        test_tracked_source_has_no_channel_specific_asset_examples()

    oracle.write_bytes(content + ("bi" + "nance").encode())
    monkeypatch.setattr(f"{__name__}._tracked_paths", lambda: (tmp_path, {oracle}))
    with pytest.raises(AssertionError, match="private channel terms found"):
        test_tracked_source_has_no_private_channel_terms()
