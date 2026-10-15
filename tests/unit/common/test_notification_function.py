"""自定义通知函数的运行边界。"""

import pytest

from axile.common import notification_function
from axile.common.notification_function import run_notification_function


@pytest.mark.parametrize("fails", [False, True])
def test_notification_uses_utf8_pipes(monkeypatch, fails) -> None:
    """父子进程固定使用 UTF-8，中文源码、日志与异常不受系统编码影响。"""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    original_run = notification_function.subprocess.run

    def run_with_legacy_default(*args, **kwargs):
        # 在 UTF-8 开发环境中模拟 Windows 的管道默认编码。
        kwargs.setdefault("encoding", "cp1252")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(notification_function.subprocess, "run", run_with_legacy_default)
    code = """def notify(context):
    assert context["name"] == "中文账户 🚀"
    print("通知日志 🚀")
    if context["fails"]:
        raise RuntimeError("通知失败 🚀")
"""
    result = run_notification_function(code, {"name": "中文账户 🚀", "fails": fails})
    assert result.ok is (not fails)
    if fails:
        assert result.error == "RuntimeError: 通知失败 🚀"
        assert result.error_line == 5


@pytest.mark.parametrize(
    "code",
    [
        "def notify(context):\n    import time\n    time.sleep(60)",
        "async def notify(context):\n    import asyncio\n    await asyncio.sleep(60)",
        "async def notify(context):\n    import time\n    time.sleep(60)",
    ],
)
def test_notification_timeout_covers_sync_async_and_blocking_async(monkeypatch, tmp_path, code) -> None:
    # 为冷启动导入留出余量，并证明超时发生在用户函数开始执行之后。
    monkeypatch.setattr(notification_function, "NOTIFICATION_FUNCTION_TIMEOUT_SECONDS", 4)
    started = tmp_path / "started"
    code = code.replace(
        "notify(context):\n",
        "notify(context):\n    from pathlib import Path\n    Path(context['started']).write_text('started')\n",
        1,
    )
    result = run_notification_function(code, {"started": str(started)})
    assert result.ok is False
    assert "超时" in result.error
    assert started.read_text() == "started"


def test_system_and_account_keys_are_isolated(monkeypatch):
    monkeypatch.setenv("AXILE_SYSTEM_FEISHU_KEY", "inherited-system")
    monkeypatch.setenv("AXILE_ACCOUNT_FEISHU_KEY", "inherited-account")
    code = "import os\ndef notify(context):\n    assert os.environ['AXILE_SYSTEM_FEISHU_KEY'] == context['system']\n    assert os.environ['AXILE_ACCOUNT_FEISHU_KEY'] == context['account']"
    assert run_notification_function(code, {"system": "system", "account": ""}, system_feishu_key="system").ok
    assert run_notification_function(code, {"system": "", "account": "account"}, feishu_key="account").ok


def test_notification_error_preserves_long_original_and_child_traceback():
    raw = "long synthetic error " * 150 + "\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因"
    result = run_notification_function(f"def notify(context):\n    raise RuntimeError({raw!r})\n", {})
    assert result.ok is False
    assert result.error == f"RuntimeError: {raw}"
    assert result.error_line == 2
    assert 'File "<notification>", line 2' in result.traceback
    assert raw in result.traceback
