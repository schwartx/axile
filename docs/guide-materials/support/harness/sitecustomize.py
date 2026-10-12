"""仅供文档素材环境使用：在主进程和 spawn 子进程中启用离线替身。"""

import os

if os.environ.get("AXILE_DOCS_OFFLINE") == "1":
    from fixture_executor import install

    install()
