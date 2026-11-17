"""在隔离环境核验实际安装的发行包、原生迁移和内嵌页面。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from importlib.metadata import version
from pathlib import Path, PurePath


def runtime_config(runtime: PurePath) -> str:
    """使用 TOML 兼容的转义保留 Windows 路径。"""
    database_uri = f"sqlite+aiosqlite:///{(runtime / 'axile.db').as_posix()}"
    return (
        f"sqlalchemy_database_uri = {json.dumps(database_uri)}\n"
        f"app_log_dir = {json.dumps(str(runtime / 'logs'))}\n"
        'environment = "local"\n'
    )


def main() -> None:
    """断言 wheel 内容和运行能力与构建源码一致。"""
    import zstandard

    import axile
    from axile.server._native import _core

    repository = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = Path(axile.__file__).resolve().parent
    assert not package.is_relative_to(repository), f"必须从隔离环境导入 wheel: {package}"
    assert version("axile") == expected
    assert callable(_core.transfer_data)
    assert _core.TRANSFER_PACKAGE_VERSION == 1
    assert _core.PUBLIC_HEAD == "0037"
    assert _core.ANALYSIS_FACT_SCHEMA == 42
    assert _core.EXECUTION_REPLAY_SCHEMA == 2
    assert callable(_core.execution_replay_rebuild)
    assert callable(_core.ReplayIndex)
    static = package / "server" / "_static"
    assert (static / "index.html").is_file()
    assert list(static.rglob("*.wasm")), "wheel 缺少回放 WASM"
    assert (static / "editor-sw.js").is_file(), "wheel 缺少离线编辑器入口"
    assert list(static.rglob("ty_wasm_bg.wasm-*.bin")), "wheel 缺少编辑器 WASM"
    assert list(static.rglob("library.json-*.bin")), "wheel 缺少离线类型和源码"
    assert list(static.rglob("python.worker-*.js")), "wheel 缺少本地语言服务 Worker"
    decoder = zstandard.ZstdDecompressor()
    editor_wasm = next(static.rglob("ty_wasm_bg.wasm-*.bin"))
    assert decoder.decompress(editor_wasm.read_bytes()).startswith(b"\x00asm"), "编辑器 WASM 不是有效 zstd 资源"
    library = json.loads(decoder.decompress(next(static.rglob("library.json-*.bin")).read_bytes()))
    assert "/lib/axile/server/context.py" in library["files"]
    assert library["versions"]["axile"] == expected

    with tempfile.TemporaryDirectory(prefix="axile-release-verify-") as directory:
        runtime = Path(directory)
        database = str(runtime / "axile.db")
        config = runtime / "config.toml"
        config.write_text(runtime_config(runtime), encoding="utf-8")
        os.environ["AXILE_CONFIG_TOML"] = str(config)
        _core.migration_upgrade(database, "{}")
        assert _core.migration_current(database, "{}")["public"] == "0037"
        _core.migration_downgrade(database, '{"public":"0033"}')
        _core.migration_upgrade(database, "{}")
        assert _core.migration_current(database, "{}")["public"] == "0037"

        from fastapi.testclient import TestClient

        from axile.server.app import _app

        with TestClient(_app, raise_server_exceptions=True) as client:
            response = client.get("/")
            assert response.status_code == 200
            resources = re.findall(r'(?:src|href)="(/[^"#]+)"', response.text)
            assert resources, "页面未引用内嵌资源"
            for resource in resources:
                assert client.get(resource).status_code == 200, resource
            worker = client.get("/editor-sw.js")
            assert worker.status_code == 200
            assert "javascript" in worker.headers["content-type"]
        _core.close_database(database)
    print(f"Verified axile=={expected}: {package}; native schemas, migrations, UI and WASM")


if __name__ == "__main__":
    main()
