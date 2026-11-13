"""在隔离环境核验实际安装的发行包、原生迁移和内嵌页面。"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    """断言 wheel 内容和运行能力与构建源码一致。"""
    import axile
    from axile.server._native import _core

    repository = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = Path(axile.__file__).resolve().parent
    assert not package.is_relative_to(repository), f"必须从隔离环境导入 wheel: {package}"
    assert version("axile") == expected
    assert callable(_core.transfer_data)
    assert _core.PUBLIC_HEAD == "0037"
    assert _core.ANALYSIS_FACT_SCHEMA == 42
    assert _core.EXECUTION_REPLAY_SCHEMA == 1
    static = package / "server" / "_static"
    assert (static / "index.html").is_file()
    assert list(static.rglob("*.wasm")), "wheel 缺少回放 WASM"

    with tempfile.TemporaryDirectory(prefix="axile-release-verify-") as directory:
        runtime = Path(directory)
        database = str(runtime / "axile.db")
        config = runtime / "config.toml"
        config.write_text(
            f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{database}"\n'
            f'app_log_dir = "{runtime / "logs"}"\n'
            'environment = "local"\n',
            encoding="utf-8",
        )
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
        _core.close_database(database)
    print(f"Verified axile=={expected}: {package}; native schemas, migrations, UI and WASM")


if __name__ == "__main__":
    main()
