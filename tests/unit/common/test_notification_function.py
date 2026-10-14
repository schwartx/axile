"""自定义通知函数的运行边界。"""

import pytest

from axile.common import notification_function
from axile.common.function_contract import accepts_context
from axile.common.notification_config import validate_notification_config
from axile.common.notification_function import run_notification_function


def test_notification_function_runs_with_context() -> None:
    """同步函数可以读取事件快照，且不依赖飞书 webhook。"""
    result = run_notification_function(
        'def notify(context):\n    assert context["execution"]["id"] == "run-1"',
        {"execution": {"id": "run-1"}},
    )
    assert result.ok is True


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


def test_notification_function_accepts_public_context_annotation() -> None:
    """编辑器提供的类型标注在隔离通知进程中也可导入。"""
    code = (
        "from axile.common.notification_context import AccountNotificationContext\n"
        "def notify(context: AccountNotificationContext) -> None:\n"
        '    assert context["execution"]["is_test"]\n'
    )
    result = run_notification_function(code, {"execution": {"is_test": True}})
    assert result.ok is True


def test_notification_function_reports_source_line() -> None:
    """用户函数异常返回源码行号，不影响父进程。"""
    result = run_notification_function('def notify(context):\n    raise RuntimeError("boom")', {})
    assert result.ok is False
    assert result.error_line == 2
    assert "boom" in str(result.error)


def test_notification_function_rejects_keyword_only_entry() -> None:
    """编辑器与运行时都要求同步且可按位置传入上下文。"""
    for code in ("def notify(*, context):\n    pass", "async def notify(*, context):\n    pass"):
        result = run_notification_function(code, {})
        assert result.ok is False


def test_notification_config_requires_function_only_in_function_mode() -> None:
    """默认模式允许空源码；函数模式要求 notify 入口。"""
    validate_notification_config("default", None)
    try:
        validate_notification_config("function", "value = 1")
    except ValueError as exc:
        assert "notify(context)" in str(exc)
    else:
        raise AssertionError("缺少 notify 函数应被拒绝")


@pytest.mark.parametrize(
    "entry",
    ["async def notify(context):", "def notify(context):\n    return work(context)\n\nasync def work(context):"],
)
def test_notification_waits_for_awaitable_completion(entry, tmp_path) -> None:
    code = (
        "import asyncio\nfrom pathlib import Path\n"
        + entry
        + "\n    await asyncio.sleep(0)\n    Path(context['path']).write_text('completed')"
    )
    output = tmp_path / "completed.txt"
    result = run_notification_function(code, {"path": str(output)})
    assert result.ok is True
    assert output.read_text() == "completed"


def test_notification_waits_for_custom_awaitable(tmp_path) -> None:
    code = """import asyncio
from pathlib import Path
class Completion:
    def __init__(self, context):
        self.context = context
    def __await__(self):
        async def work():
            await asyncio.sleep(0)
            Path(self.context['path']).write_text('completed')
        return work().__await__()
def notify(context):
    return Completion(context)
"""
    output = tmp_path / "completed.txt"
    assert run_notification_function(code, {"path": str(output)}).ok
    assert output.read_text() == "completed"


def test_async_notification_reports_source_line() -> None:
    code = "async def notify(context):\n    import asyncio\n    await asyncio.sleep(0)\n    raise RuntimeError('async boom')"
    result = run_notification_function(code, {})
    assert result.ok is False
    assert result.error_line == 4
    assert "async boom" in result.error


@pytest.mark.parametrize(
    "code",
    [
        "def notify(context):\n    yield 1",
        "def notify(context):\n    yield from []",
        "async def notify(context):\n    yield 1",
    ],
)
def test_notification_rejects_generator_entry(code) -> None:
    with pytest.raises(ValueError, match="生成器"):
        notification_function.validate_notification_function(code)
    assert run_notification_function(code, {}).ok is False


@pytest.mark.parametrize(
    "code",
    [
        "def notify(context):\n    return (x for x in [])",
        "async def stream():\n    yield 1\ndef notify(context):\n    return stream()",
        "async def notify(context):\n    return (x for x in [])",
        "def notify(context):\n    pass\ndef stream(context):\n    yield 1\nnotify = stream",
    ],
)
def test_notification_rejects_runtime_generators(code) -> None:
    result = run_notification_function(code, {})
    assert result.ok is False
    assert "生成器" in result.error


def test_notification_allows_nested_generators() -> None:
    code = """def notify(context):
    def items():
        yield 1
    async def stream():
        yield 2
    class Helper:
        def items(self):
            yield 3
    helper = lambda: (yield 4)
    assert list(items()) == [1]
    assert list(Helper().items()) == [3]
    assert list(helper()) == [4]
"""
    assert run_notification_function(code, {}).ok is True


@pytest.mark.parametrize(
    "code",
    [
        "def notify(context):\n    import time\n    time.sleep(60)",
        "async def notify(context):\n    import asyncio\n    await asyncio.sleep(60)",
        "async def notify(context):\n    import time\n    time.sleep(60)",
    ],
)
def test_notification_timeout_covers_sync_async_and_blocking_async(monkeypatch, code) -> None:
    monkeypatch.setattr(notification_function, "NOTIFICATION_FUNCTION_TIMEOUT_SECONDS", 2)
    result = run_notification_function(code, {})
    assert result.ok is False
    assert "超时" in result.error


def test_context_contract_preserves_sync_default() -> None:
    async def entry(context):
        pass

    assert accepts_context(entry) is False
    assert accepts_context(entry, allow_async=True) is True


def test_sync_notification_can_manage_its_own_event_loop() -> None:
    code = """import asyncio
async def work():
    await asyncio.sleep(0)
    return 42
def notify(context):
    assert asyncio.run(work()) == 42
"""
    assert run_notification_function(code, {}).ok is True


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
