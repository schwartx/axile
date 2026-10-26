"""定义账户控制流程使用的异常类型."""

from __future__ import annotations

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.diagnostics import AccountControlHit


class AccountControlConfigError(Exception):
    """配置错误；保留原生校验给出的错误码、配置字段路径与可读消息。"""

    def __init__(self, code: str, path: list[str], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    @property
    def details(self) -> dict[str, object]:
        """返回 SDK 与 HTTP 共用的配置错误载荷。"""
        return {"code": self.code, "path": self.path, "message": self.message}


class AccountControlBlockedError(RuntimeError):
    """
    表示账户控制拒绝本次对外交互的异常.

    Attributes
    ----------
    account_id : int | None
        被拒绝的账户 ID。
    execution_id : str | None
        当前执行会话 ID。
    channel : TradeChannel
        触发拒绝的交易渠道。
    operation : str
        被拒绝的操作键。
    symbol : str | None
        与本次调用关联的交易标的代码。
    reason_code : str | None
        可选的机器原因码，与用户可读异常消息分开保存。
    details : AccountControlHit | None
        额度触发瞬间的不可变快照；非额度拦截或旧调用可为空。
    """

    def __init__(
        self,
        message: str,
        *,
        account_id: int | None,
        execution_id: str | None,
        channel: TradeChannel,
        operation: str,
        symbol: str | None = None,
        details: AccountControlHit | None = None,
        reason_code: str | None = None,
        execution_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.account_id = account_id
        self.execution_id = execution_id
        self.channel = channel
        self.operation = operation
        self.symbol = symbol
        self.details = details
        self.reason_code = reason_code
        self.execution_error = execution_error or message
