"""离线资源仅包含允许的源码文件，排除配置、测试和缓存。"""

import runpy
from pathlib import Path


def test_library_excludes_runtime_data_and_preserves_module_paths(tmp_path):
    source = runpy.run_path(str(Path(__file__).parents[3] / "scripts/build_editor_library.py"))["sources"]
    for name in ("__init__.py", "types.pyi", "config.toml", "tests/secret.py", "__pycache__/cached.py"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("# 中文\n")
    assert source(tmp_path, "example") == {
        "/lib/example/__init__.py": "# 中文\n",
        "/lib/example/types.pyi": "# 中文\n",
    }
