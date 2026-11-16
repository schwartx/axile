"""普通数据对象的载荷转换；内部构造和复制不执行输入校验。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from functools import cache
from math import isfinite
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

import msgspec


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


@cache
def _export_fields(cls: type) -> tuple[str, ...]:
    return tuple(field.name for field in fields(cls) if not field.metadata.get("exclude")) + getattr(
        cls, "__json_properties__", ()
    )


def from_payload(cls: Any, value: Any) -> Any:
    """从本程序产生的载荷恢复对象，不重复校验业务规则。"""
    if cls is Any or value is None or (isinstance(cls, type) and isinstance(value, cls)):
        return value
    origin, args = get_origin(cls), get_args(cls)
    if origin is Annotated:
        return from_payload(args[0], value)
    if origin in (Union, UnionType):
        return _union_payload(args, value)
    if origin is list:
        return [from_payload(args[0], item) for item in value]
    if origin is dict:
        return {key: from_payload(args[1], item) for key, item in value.items()}
    if origin is tuple:
        return tuple(
            from_payload(args[0] if args[-1] is Ellipsis else args[index], item) for index, item in enumerate(value)
        )
    if isinstance(cls, type) and is_dataclass(cls):
        prepare = getattr(cls, "prepare", None)
        if prepare is not None:
            value = prepare(value)
        raw = value if isinstance(value, Mapping) else vars(value)
        hints = _hints(cls)
        obj = cls(
            **{
                field.name: from_payload(hints[field.name], raw[field.name])
                for field in fields(cls)
                if field.name in raw
            }
        )
        return obj
    if cls in (datetime, date) and isinstance(value, str):
        return cls.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(cls, type) and issubclass(cls, (Enum, Path)):
        return cls(value)
    if isinstance(cls, type) and hasattr(cls, "model_validate"):
        return getattr(cls, "model_validate")(value)
    return value


def _union_payload(types: tuple[Any, ...], value: Any) -> Any:
    candidates = [item for item in types if item not in (type(None), msgspec.UnsetType)]
    if len(candidates) == 1:
        return from_payload(candidates[0], value)
    for candidate in candidates:
        if is_dataclass(candidate) and isinstance(value, dict):
            if set(value) <= {field.name for field in fields(candidate)}:
                return from_payload(candidate, value)
    return value


def to_payload(
    value: Any, *, mode: str = "python", include=None, exclude=None, exclude_none=False, exclude_unset=False
) -> Any:
    """在 HTTP、IPC 或存储出口导出所需字段，不深拷贝运行时对象。"""
    if type(value) in (str, int, bool, type(None)):
        return value
    if isinstance(value, float):
        return None if mode == "json" and not isfinite(value) else value
    if is_dataclass(value) and not isinstance(value, type):
        selected = None if include is None else set(include)
        omitted = (
            set()
            if exclude is None
            else {
                key
                for key in exclude
                if not isinstance(exclude, dict) or exclude[key] is True or exclude[key] is Ellipsis
            }
        )
        provided = provided_fields(value) if exclude_unset else None
        result = {}
        for name in _export_fields(type(value)):
            if (
                name in omitted
                or (selected is not None and name not in selected)
                or (provided is not None and name not in provided)
            ):
                continue
            item = getattr(value, name)
            if item is msgspec.UNSET:
                continue
            if exclude_none and item is None:
                continue
            nested = exclude.get(name) if isinstance(exclude, dict) else None
            # 数值事实保留 NaN/Inf；任意元数据中的非有限数值才转为 JSON null。
            result[name] = (
                item
                if type(item) in (str, int, bool, float, type(None))
                else to_payload(item, mode=mode, exclude=nested, exclude_none=exclude_none)
            )
        return result
    if isinstance(value, dict):
        omitted = (
            set()
            if exclude is None
            else {
                key
                for key in exclude
                if not isinstance(exclude, dict) or exclude[key] is True or exclude[key] is Ellipsis
            }
        )
        return {
            key: to_payload(
                item,
                mode=mode,
                exclude=exclude.get(key, exclude.get("__all__")) if isinstance(exclude, dict) else None,
                exclude_none=exclude_none,
            )
            for key, item in value.items()
            if key not in omitted and (include is None or key in include) and (not exclude_none or item is not None)
        }
    if isinstance(value, (list, tuple)):
        nested = exclude.get("__all__") if isinstance(exclude, dict) else None
        items = [to_payload(item, mode=mode, exclude=nested, exclude_none=exclude_none) for item in value]
        return tuple(items) if mode == "python" and isinstance(value, tuple) else items
    if hasattr(value, "model_dump"):
        return to_payload(
            getattr(value, "model_dump")(mode=mode),
            mode=mode,
            include=include,
            exclude=exclude,
            exclude_none=exclude_none,
        )
    if mode == "json":
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, Path):
            return str(value)
    return value


def copy_data(value: Any, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Any:
    """复制数据对象，保留调用方要求的快照隔离。"""
    if not is_dataclass(value) and callable(native_copy := getattr(value, "model_copy", None)):
        return native_copy(update=update, deep=deep)
    result = copy.deepcopy(value) if deep else copy.copy(value)
    if update:
        if not is_dataclass(result) or isinstance(result, type):
            return getattr(result, "model_copy")(update=update, deep=False)
        result = replace(result, **update)
        if hasattr(value, "_provided_fields"):
            object.__setattr__(result, "_provided_fields", provided_fields(value) | update.keys())
    return result


def provided_fields(value: Any) -> frozenset[str]:
    """区分请求中省略的字段与显式提供的空值。"""
    return getattr(
        value,
        "_provided_fields",
        frozenset(field.name for field in fields(value) if getattr(value, field.name) is not msgspec.UNSET),
    )


def validate_input(value: Any) -> Any:
    """只在配置保存或执行入口检查嵌套对象的业务约束。"""
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            validate_input(getattr(value, field.name))
        validator = getattr(value, "validate", None)
        if validator is not None:
            validator()
    elif isinstance(value, dict):
        for item in value.values():
            validate_input(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_input(item)
    return value


def parse_input(cls: type, value: Any, *, strict: bool = True) -> Any:
    """解析编辑或执行入口的输入；内部转发使用 from_payload。"""
    value = _prepare_input(cls, value)
    result = msgspec.convert(value, type=cls, strict=strict, dec_hook=lambda target, item: target(item))
    _mark_provided(result, value)
    return validate_input(result)


def parameter_schema(cls: type) -> dict[str, Any]:
    """从参数类型生成现有表单使用的 JSON Schema。"""
    schema = msgspec.json.schema(cls)
    if "$ref" in schema:
        root = dict(schema["$defs"][schema["$ref"].rsplit("/", 1)[-1]])
        root["$defs"] = schema["$defs"]
        schema = root
    for name, item in schema.get("properties", {}).items():
        annotation = _hints(cls).get(name)
        if get_origin(annotation) is Literal:
            values = [to_payload(value, mode="json") for value in get_args(annotation)]
            item["enum"] = values
            kind = type(values[0])
            if all(type(value) is kind for value in values):
                if wire_type := {str: "string", int: "integer", float: "number", bool: "boolean"}.get(kind):
                    item["type"] = wire_type
    return schema


def _prepare_input(cls, value):
    if value is None:
        return value
    origin, args = get_origin(cls), get_args(cls)
    if origin is Annotated:
        return _prepare_input(args[0], value)
    if origin in (Union, UnionType):
        candidates = [item for item in args if item not in (type(None), msgspec.UnsetType)]
        return _prepare_input(candidates[0], value) if len(candidates) == 1 else value
    if is_dataclass(cls) and isinstance(value, dict):
        prepare = getattr(cls, "prepare", None)
        value = prepare(value) if prepare else value
        hints = _hints(cls)
        if getattr(cls, "__forbid_unknown__", False):
            unknown = set(value) - hints.keys()
            if unknown:
                raise ValueError(f"extra_forbidden: 不支持的字段: {', '.join(sorted(unknown))}")
        return {key: _prepare_input(hints[key], item) if key in hints else item for key, item in value.items()}
    if origin is list and isinstance(value, list):
        return [_prepare_input(args[0], item) for item in value]
    if origin is dict and isinstance(value, dict):
        return {key: _prepare_input(args[1], item) for key, item in value.items()}
    return value


def _mark_provided(value, raw):
    if is_dataclass(value) and isinstance(raw, dict):
        object.__setattr__(value, "_provided_fields", frozenset(raw))
        for field in fields(value):
            if field.name in raw:
                _mark_provided(getattr(value, field.name), raw[field.name])
    elif isinstance(value, dict) and isinstance(raw, dict):
        for key, item in value.items():
            _mark_provided(item, raw[key])
    elif isinstance(value, (list, tuple)) and isinstance(raw, (list, tuple)):
        for item, source in zip(value, raw, strict=True):
            _mark_provided(item, source)
