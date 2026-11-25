"""代码工作台的入口函数契约与类型标注修复。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

from axile.common.function_contract import has_generator_yield

type EditorKind = Literal["portfolio", "account_notification", "system_notification", "signal_quotes", "signal_account"]


@dataclass(frozen=True)
class EntryContract:
    """入口名称及其公开参数类型。"""

    name: str
    module: str | None
    type_name: str
    parameter_name: str = "context"


CONTRACTS: dict[EditorKind, EntryContract] = {
    "portfolio": EntryContract("calculate_portfolio", "axile.server.context", "Context"),
    "account_notification": EntryContract("notify", "axile.common.notification_context", "AccountNotificationContext"),
    "system_notification": EntryContract("notify", "axile.common.notification_context", "SystemNotificationContext"),
    "signal_quotes": EntryContract("fetch_quotes", None, "list[str]", "symbols"),
    "signal_account": EntryContract("fetch_account", "axile.channels.rebalance_signal", "SignalAccountContext"),
}


def _position(line: int, column: int) -> dict[str, int]:
    return {"line": line, "character": column}


def _range(line: int, start: int, end: int) -> dict[str, dict[str, int]]:
    return {"start": _position(line, start), "end": _position(line, end)}


def _diagnostic(message: str, line: int, start: int, end: int, severity: int = 1) -> dict[str, object]:
    return {"range": _range(line, start, end), "severity": severity, "message": message, "source": "入口契约"}


def _import_position(tree: ast.Module) -> int:
    """在模块 docstring 和 future imports 之后插入导入。"""
    index = 0
    body = tree.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        index = body[0].end_lineno or body[0].lineno
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            index = node.end_lineno or node.lineno
    return index


def _utf16_column(code: str, line: int, byte_column: int) -> int:
    """把 Python AST 的 UTF-8 字节列转换为 LSP 的 UTF-16 列。"""
    source_line = code.splitlines()[line]
    prefix = source_line.encode("utf-8")[:byte_column].decode("utf-8")
    return len(prefix.encode("utf-16-le")) // 2


def _annotation_fix(tree: ast.Module, code: str, argument: ast.arg, contract: EntryContract) -> dict[str, object]:
    """生成只修改参数标注及必要导入的源码编辑。"""
    line = argument.end_lineno - 1 if argument.end_lineno else argument.lineno - 1
    column = _utf16_column(code, line, argument.end_col_offset or argument.col_offset + len(argument.arg.encode()))
    edits: list[dict[str, object]] = [{"range": _range(line, column, column), "newText": f": {contract.type_name}"}]
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == contract.module
        and any(alias.name == contract.type_name and alias.asname is None for alias in node.names)
        for node in tree.body
    )
    if contract.module is not None and not imported:
        insert_line = _import_position(tree)
        edits.append(
            {
                "range": _range(insert_line, 0, 0),
                "newText": f"from {contract.module} import {contract.type_name}\n",
            }
        )
    return {"title": f"为 {argument.arg} 添加 {contract.type_name} 类型标注", "edits": edits}


def check_editor_contract(code: str, kind: EditorKind) -> dict[str, list[dict[str, object]]]:
    """检查入口定义，并给未标注的上下文参数提供修复。"""
    contract = CONTRACTS[kind]
    diagnostics: list[dict[str, object]] = []
    fixes: list[dict[str, object]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"diagnostics": diagnostics, "fixes": fixes}  # 语法错误由 ty 报告
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == contract.name
    ]
    if not functions:
        diagnostics.append(_diagnostic(f"必须定义 {contract.name}({contract.parameter_name}) 函数", 0, 0, 0))
        return {"diagnostics": diagnostics, "fixes": fixes}
    function = functions[-1]
    line = function.lineno - 1
    if kind in {"portfolio", "signal_quotes", "signal_account"} and isinstance(function, ast.AsyncFunctionDef):
        diagnostics.append(
            _diagnostic(
                f"{contract.name} 必须是同步函数", line, function.col_offset, function.col_offset + len("async def")
            )
        )
    if kind != "portfolio" and has_generator_yield(function):
        diagnostics.append(
            _diagnostic(
                f"{contract.name} 不支持生成器或异步生成器函数",
                line,
                function.col_offset,
                function.col_offset + len(function.name),
            )
        )
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    if len(positional) != 1 or args.vararg or args.kwarg or args.kwonlyargs:
        diagnostics.append(
            _diagnostic(
                f"{contract.name} 必须且只能接收一个位置参数 {contract.parameter_name}",
                line,
                function.col_offset,
                function.col_offset + len(function.name),
            )
        )
        return {"diagnostics": diagnostics, "fixes": fixes}
    argument = positional[0]
    if argument.annotation is None:
        diagnostics.append(
            _diagnostic(
                f"可为 {argument.arg} 标注 {contract.type_name}，以获得类型检查和补全",
                argument.lineno - 1,
                _utf16_column(code, argument.lineno - 1, argument.col_offset),
                _utf16_column(code, argument.lineno - 1, argument.col_offset + len(argument.arg.encode())),
                3,
            )
        )
        fixes.append(_annotation_fix(tree, code, argument, contract))
    return {"diagnostics": diagnostics, "fixes": fixes}
