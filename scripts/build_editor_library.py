"""为离线编辑器收集公开 Python 源码与模板依赖，不导入或执行这些模块。"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

import zstandard

# 只打包明确支持的依赖；服务器额外安装的私有渠道不会被复制到浏览器。
PACKAGES = (
    "aiohttp",
    "aiohappyeyeballs",
    "aiosignal",
    "frozenlist",
    "multidict",
    "yarl",
    "propcache",
    "idna",
    "numpy",
    "pandas",
    "pydantic",
    "pydantic_core",
    "annotated_types",
    "typing_extensions",
    "typing_inspection",
)


def sources(root: Path, prefix: str) -> dict[str, str]:
    """收集类型分析所需文件，排除测试、缓存和运行配置。"""
    paths = root.rglob("*") if root.is_dir() else [root]
    return {
        f"/lib/{prefix}/{path.relative_to(root).as_posix()}" if root.is_dir() else f"/lib/{root.name}": path.read_text(
            encoding="utf-8"
        )
        for path in paths
        if path.is_file()
        and path.suffix in {".py", ".pyi"}
        and not {"tests", "test", "__pycache__", "_static", "_native"}.intersection(path.relative_to(root).parts)
    }


def main() -> None:
    """生成与当前源码和已锁定依赖匹配的确定性资源包。"""
    root = Path(__file__).resolve().parents[1]
    files = sources(root / "axile", "axile")
    notices = {"ty": "https://github.com/astral-sh/ruff\n" + (root / "ui/src/features/editor/wasm/LICENSE").read_text()}
    notices["fzstd"] = (root / "ui/node_modules/fzstd/LICENSE").read_text()
    distributions = importlib.metadata.packages_distributions()
    versions = {"axile": importlib.metadata.version("axile")}
    for package in PACKAGES:
        spec = importlib.util.find_spec(package)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"缺少离线编辑依赖：{package}")
        path = Path(spec.origin)
        files.update(sources(path.parent if spec.submodule_search_locations else path, package))
        for name in distributions.get(package, []):
            distribution = importlib.metadata.distribution(name)
            versions[name] = distribution.version
            for entry in distribution.files or []:
                if entry.name.lower().startswith(("license", "notice", "copying")):
                    license_file = Path(distribution.locate_file(entry))
                    if license_file.is_file():
                        notices[f"{name}/{entry}"] = license_file.read_text(encoding="utf-8", errors="replace")
    data = json.dumps(
        {"files": files, "notices": notices, "versions": versions}, ensure_ascii=False, sort_keys=True
    ).encode()
    output = Path(sys.argv[1]) / "library.json.bin"
    compressor = zstandard.ZstdCompressor(level=19, write_checksum=True)
    output.write_bytes(compressor.compress(data))
    print(f"Editor library: {len(files)} files, {len(data):,} bytes, {output.stat().st_size:,} compressed")
    wasm = output.parent / "ty_wasm_bg.wasm"
    wasm_output = output.parent / "ty_wasm_bg.wasm.bin"
    wasm_output.write_bytes(compressor.compress(wasm.read_bytes()))
    print(f"Editor WASM: {wasm.stat().st_size:,} bytes, {wasm_output.stat().st_size:,} compressed")


if __name__ == "__main__":
    main()
