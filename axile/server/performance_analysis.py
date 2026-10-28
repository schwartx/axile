"""启动时核对交易事实接口；绩效计算由浏览器 WASM 执行。"""

from axile.server._native import _core

# 旧原生读接口仍引用此常量；浏览器使用独立的账本算法版本。
LOGIC_VERSION = "23"
ENGINE_VERSION = "equity-ledger-1"


def ensure_native_capabilities() -> None:
    """Reject obsolete extensions before any background services start."""
    required = (
        "close_database",
        "account_public",
        "connection_accounts",
        "execution_read",
        "supplement_has_execution_steps",
        "persist_execution_state",
        "freeze_execution_input",
        "freeze_execution_after_assets",
        "browser_analysis_state",
        "browser_analysis_facts",
        "analysis_context",
        "analysis_calendar_save",
    )
    if getattr(_core, "ANALYSIS_FACT_SCHEMA", None) != 40:
        raise RuntimeError("Native analysis extension requires direct business facts schema 0034; rebuild the wheel")
    if getattr(_core, "PUBLIC_HEAD", None) != "0038":
        raise RuntimeError("Native analysis extension requires migration head 0038; rebuild the wheel")
    missing = [name for name in required if not callable(getattr(_core, name, None))]
    if missing:
        raise RuntimeError(f"Native analysis extension must export {', '.join(missing)}; rebuild the wheel")
