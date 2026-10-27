"""文件日志协议、历史解码与共用文本渲染。"""

from __future__ import annotations

import gzip
import json
import zlib
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from string import Formatter
from typing import Any, Iterable


class LogDecodeError(ValueError):
    """日志损坏、引用缺失或定义冲突。"""


def decode_json(line: bytes) -> object:
    """拒绝 JSON 标准以外的 NaN 和 Infinity。"""

    def invalid(value: str) -> None:
        raise LogDecodeError("non-finite JSON value")

    return json.loads(line, parse_constant=invalid)


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LogDecodeError(message)


def template_fields(template: str, fields: list[str]) -> set[str]:
    """检查模板只引用显式声明的字段，不允许属性或下标访问。"""
    try:
        parts = list(Formatter().parse(template))
        used = {name for _, name, _, conversion in parts if name is not None}
        _require(used <= set(fields), "template references undeclared fields")
        for _, name, spec, conversion in parts:
            _require(not spec and conversion is None, "template format specifications are not supported")
            _require(
                name is None or (name in fields and not name.isnumeric() and not any(char in name for char in ".[]")),
                "invalid template field",
            )
        return used
    except ValueError as error:
        raise LogDecodeError(str(error)) from error


def _check_message_size(template: str, values: dict[str, str], extras: list[str], maximum: int) -> None:
    """在 format_map 拼接重复参数前检查实际 UTF-8 长度。"""
    parts = chain.from_iterable(
        (literal, values[name]) if name is not None else (literal,)
        for literal, name, _, _ in Formatter().parse(template)
    )
    size = 0
    for part in chain(parts, (" " + extra for extra in extras)):
        size += len(part.encode("utf-8"))
        if size > maximum:
            raise OverflowError("log message exceeds byte limit")


def message_text(record: dict[str, Any], *, max_bytes: int | None = None) -> tuple[str, object]:
    """展开参数，同时保留没有出现在文案中的字段。"""
    if "template" not in record:
        return str(record.get("message", "")), record.get("exception")
    return _message_text(record, template_fields(record["template"], record["fields"]), max_bytes)


def _message_text(record: dict[str, Any], used: set[str], maximum: int | None = None) -> tuple[str, object]:
    """展开已经校验过的模板；定义的校验结果只在当前解码器内复用。"""
    fields, args = record["fields"], record["args"]
    values = dict(zip(fields, args, strict=True))
    _require(used <= values.keys(), "template references undeclared fields")

    def text(value: object) -> str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    formatted = {key: text(value) for key, value in values.items()}
    extras = [f"{key}={formatted[key]}" for key in values if key not in used and key != "exception"]
    if maximum is not None:
        _check_message_size(record["template"], formatted, extras, maximum)
    message = record["template"].format_map(formatted)
    if extras:
        message += " " + " ".join(extras)
    return message, values.get("exception")


def context_text(context: dict[str, Any]) -> str:
    """沿用终端上的账户、渠道、合约和短执行标识。"""
    parts = []
    account_id, account_name = context.get("account_id"), context.get("account_name")
    if account_name:
        parts.append(str(account_name) + (f"#{account_id}" if account_id is not None else ""))
    elif account_id is not None:
        parts.append(f"账户#{account_id}")
    for field in ("channel", "symbol"):
        if context.get(field):
            parts.append(str(context[field]))
    if context.get("execution_id"):
        parts.append(str(context["execution_id"])[:8])
    return f"[{' '.join(parts)}] " if parts else ""


def _display_time(stamp: str, cache: dict[datetime, str] | None = None) -> str:
    """批量显示只缓存当前绝对秒，跨夏令时重复小时仍分别换算。"""
    if not stamp:
        return "时间未知"
    instant = datetime.fromisoformat(stamp)
    if cache is None:
        local = instant.astimezone()
        prefix = _time_prefix(local)
        micros = local.microsecond
    else:
        instant = instant.astimezone(UTC)
        second = instant.replace(microsecond=0)
        prefix = cache.get(second)
        if prefix is None:
            prefix = _time_prefix(instant.astimezone())
            cache.clear()
            cache[second] = prefix
        micros = instant.microsecond
    return f"{prefix}.{micros // 1000:03d}"


def _time_prefix(value: datetime) -> str:
    """固定数字格式无需反复经过系统 strftime。"""
    return (
        f"{value.year % 100:02d}{value.month:02d}{value.day:02d} {value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    )


def render_record(record: dict[str, Any], *, color: bool = False) -> str:
    """实时和外部字典始终按当前字段展开正文。"""
    return _render_record(record, color=color)


def _render_record(
    record: dict[str, Any], *, color: bool = False, prepared: bool = False, times: dict[datetime, str] | None = None
) -> str:
    """批量读取产生的记录可复用正文，旧版字典仍按原规则解释。"""
    timestamp = _display_time(record.get("timestamp", ""), times)
    level = "WARN" if record.get("level") == "WARNING" else str(record.get("level", "INFO"))
    component = record.get("metadata", {}).get("component", record.get("component", "系统"))
    label = f"{level:<5}"
    if color:
        timestamp = f"\x1b[32m{timestamp}\x1b[0m"
        label = f"\x1b[{31 if level in ('ERROR', 'CRITICAL') else 33 if level == 'WARN' else 36}m{label}\x1b[0m"
    message, exception = (record["message"], record["exception"]) if prepared else message_text(record)
    if exception:
        detail = (
            exception.get("traceback") or exception.get("message", "")
            if isinstance(exception, dict)
            else str(exception)
        )
        message += "\n" + detail.rstrip("\n")
    anchor = (
        f"[{record['run_id']}#{record['seq']}] " if "\n" in message and "run_id" in record and "seq" in record else ""
    )
    message = message.replace("\n", "\n  | " + anchor)
    return f"{timestamp} {label} [{component}] {context_text(record.get('context', {}))}{anchor}{message}"


class LogDecoder:
    """独立解码一个分片，拒绝冲突定义和未定义事件引用。"""

    def __init__(self, definitions: dict[tuple, object] | None = None) -> None:
        self.run_id: str | None = None
        self.base: datetime | None = None
        self.templates: dict[int, list[Any]] = {}
        self._template_fields: dict[int, set[str]] = {}
        self.contexts: dict[int, dict[str, Any]] = {0: {}}
        self.spans: dict[int, dict[str, Any]] = {}
        self.sequence = 0
        self.time_us = 0
        self.definitions = definitions if definitions is not None else {}

    def _definition(self, table: dict, key: int, value: object, kind: str) -> None:
        assert self.run_id is not None
        _require(_integer(key, minimum=1), "definition id must be positive")
        _require(key not in table or table[key] == value, "conflicting definition")
        identity = (self.run_id, kind, key)
        _require(
            identity not in self.definitions or self.definitions[identity] == value,
            "conflicting definition across files",
        )
        self.definitions[identity] = value
        table[key] = value

    def _timestamp(self, micros: int) -> str:
        assert self.base is not None
        try:
            return (self.base + timedelta(microseconds=micros)).isoformat(timespec="microseconds")
        except OverflowError as error:
            raise LogDecodeError("timestamp outside supported range") from error

    def feed(self, row: Any, *, render: bool = True) -> dict[str, Any] | None:
        """接收定义或事件；证据收集可延迟正文展开至归属和预算检查后。"""
        if isinstance(row, dict):
            _require(self.run_id is None and isinstance(row.get("context"), dict), "invalid legacy log record")
            return row
        _require(isinstance(row, list) and bool(row), "log record must be a nonempty array")
        kind = row[0]
        if kind == "H":
            _require(
                len(row) == 3 and type(row[1]) is int and row[1] == 1 and isinstance(row[2], dict), "invalid log header"
            )
            _require(
                self.run_id is None
                and row[2].get("time_unit") == "us"
                and isinstance(row[2].get("run_id"), str)
                and bool(row[2]["run_id"]),
                "invalid or repeated log header",
            )
            try:
                base = datetime.fromisoformat(row[2]["time_base"])
                _require(base.tzinfo is not None, "time_base must include timezone")
                base = base.astimezone(UTC)
            except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
                raise LogDecodeError("invalid time_base") from error
            identity = (row[2]["run_id"], "H")
            _require(
                identity not in self.definitions or self.definitions[identity] == base,
                "conflicting time_base across files",
            )
            self.definitions[identity] = base
            self.run_id = row[2]["run_id"]
            self.base = base
            return None
        _require(self.run_id is not None, "missing log header")
        if kind == "T":
            _require(
                len(row) == 6
                and isinstance(row[2], str)
                and isinstance(row[3], str)
                and isinstance(row[4], list)
                and isinstance(row[5], dict),
                "invalid template",
            )
            _require(all(isinstance(field, str) and bool(field) for field in row[4]), "invalid field names")
            _require(len(set(row[4])) == len(row[4]), "duplicate field names")
            _require(row[5].get("kind", "event") in ("event", "span_start", "span_end"), "invalid event kind")
            used = template_fields(row[3], row[4])
            self._definition(self.templates, row[1], row[2:], "T")
            self._template_fields[row[1]] = used
            return None
        if kind == "C":
            _require(len(row) == 3 and isinstance(row[2], dict), "invalid context")
            self._definition(self.contexts, row[1], row[2], "C")
            return None
        if kind == "S":
            _require(
                len(row) == 6
                and (row[2] is None or _integer(row[2], minimum=1))
                and isinstance(row[3], str)
                and _integer(row[4])
                and row[4] in self.contexts
                and _integer(row[5]),
                "invalid span",
            )
            parent, seen = row[2], {row[1]}
            while parent is not None:
                _require(parent not in seen, "cyclic span parent")
                seen.add(parent)
                parent = self.spans[parent]["parent"] if parent in self.spans else None
            self._definition(
                self.spans,
                row[1],
                {
                    "id": row[1],
                    "parent": row[2],
                    "name": row[3],
                    "context": self.contexts[row[4]],
                    "context_id": row[4],
                    "start_us": row[5],
                    "start_time": self._timestamp(row[5]),
                },
                "S",
            )
            return None
        if kind == "E":
            return self._event(row, render=render)
        raise LogDecodeError("unknown record type")

    def _event(self, row: list[Any], *, render: bool) -> dict[str, Any]:
        assert self.run_id is not None
        _require(len(row) == 7 and _integer(row[1], minimum=1) and _integer(row[2]), "invalid event")
        _require(row[1] > self.sequence and row[2] >= self.time_us, "event order is not monotonic")
        _require(_integer(row[3], minimum=1) and row[3] in self.templates, "undefined template")
        _require(_integer(row[4]) and row[4] in self.contexts, "undefined context")
        _require(row[5] is None or (_integer(row[5], minimum=1) and row[5] in self.spans), "undefined span")
        severity, template, fields, metadata = self.templates[row[3]]
        _require(isinstance(row[6], list) and len(row[6]) == len(fields), "event argument count mismatch")
        kind = metadata.get("kind", "event")
        _require(kind == "event" or row[5] is not None, "span event requires a span")
        if kind == "span_end":
            _require(
                "outcome" in fields and row[6][fields.index("outcome")] in ("ok", "error", "cancelled"),
                "invalid span outcome",
            )
        spans, span_id = [], row[5]
        while span_id in self.spans:
            spans.append(self.spans[span_id])
            span_id = self.spans[span_id]["parent"]
        self.sequence, self.time_us = row[1], row[2]
        record = {
            "run_id": self.run_id,
            "seq": row[1],
            "timestamp": self._timestamp(row[2]),
            "level": severity,
            "template": template,
            "fields": fields,
            "args": row[6],
            "metadata": metadata,
            "context": self.contexts[row[4]],
            "span_id": row[5],
            "spans": [dict(span) for span in reversed(spans)],
        }
        if render:
            record["message"], record["exception"] = _message_text(record, self._template_fields[row[3]])
        record["component"] = metadata.get("component", "系统")
        record["source"] = metadata.get("source", {})
        return record


def iter_file(path: Path, definitions: dict[tuple, object] | None = None) -> Iterable[dict[str, Any]]:
    """读取普通文件或 gzip；损坏记录带文件与行号报告，不静默跳过。"""
    for record, _ in _iter_file(path, definitions):
        yield record


def _iter_file(path: Path, definitions: dict[tuple, object] | None) -> Iterable[tuple[dict[str, Any], bool]]:
    """正文复用标识仅随内部结果传递，不混入日志字段。"""
    decoder = LogDecoder(definitions)
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rb") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    _require(line.endswith(b"\n"), "incomplete log line")
                    record = decoder.feed(decode_json(line))
                    if record is not None:
                        yield record, decoder.run_id is not None
                except (ValueError, TypeError, UnicodeDecodeError) as error:
                    raise LogDecodeError(f"{path.name}:{number}: {error}") from error
    except (OSError, EOFError, zlib.error) as error:
        raise LogDecodeError(f"{path.name}: {error}") from error


def read_logs(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """合并进程及轮转分片；同一事件重复输入不重复展示。"""
    return [record for record, _ in _read_logs(paths)]


def _read_logs(paths: Iterable[Path]) -> list[tuple[dict[str, Any], bool]]:
    """保留记录来源，避免把旧版字典中的 message 当作可信展开结果。"""
    events: dict[tuple, tuple[dict[str, Any], bool]] = {}
    definitions: dict[tuple, object] = {}
    for path in paths:
        for index, (record, prepared) in enumerate(_iter_file(path, definitions)):
            key = (record.get("run_id", str(path)), record.get("seq", index))
            _require(key not in events or events[key][0] == record, "conflicting event across files")
            events[key] = record, prepared
    records = list(events.values())
    del events
    records.sort(key=lambda item: _log_order(item[0]))
    return records


def format_logs(paths: Iterable[Path], mode: str = "timeline", *, color: bool = False) -> str:
    """批量输出时间线、过程树或计数，复用解码时已展开的正文。"""
    records = _read_logs(paths)
    if mode == "stats":
        return json.dumps(log_statistics(record for record, _ in records), ensure_ascii=False, indent=2)
    times: dict[datetime, str] = {}
    if mode == "tree":
        unprepared = {id(record) for record, expanded in records if not expanded}
        return _render_tree((record for record, _ in records), unprepared, times)
    if mode != "timeline":
        raise ValueError("unknown log display mode")
    return "\n".join(
        _render_record(record, color=color, prepared=expanded, times=times) for record, expanded in records
    )


def statistics_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """识别并校验累计快照，避免将任意事件参数当作计数。"""
    if not record.get("metadata", {}).get("log_stats"):
        return None
    args = record.get("args")
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], dict):
        raise LogDecodeError("invalid logging statistics")
    _require(isinstance(record.get("run_id"), str), "logging statistics require run_id")
    _require(_integer(args[0].get("dropped_total")), "invalid dropped_total")
    return args[0]


def log_statistics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """每个运行只取最新累计快照，避免重复相加。"""
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        stats = statistics_record(record)
        if stats is not None:
            latest[record["run_id"]] = stats
    return {
        "runs": latest,
        "dropped_total": sum(stats["dropped_total"] for stats in latest.values()),
        "coverage": "last_saved_checkpoint",
    }


def _tree(records: Iterable[dict[str, Any]]) -> tuple[list[dict], dict[tuple, dict]]:
    """先建立过程索引，再连接父子；缺失父过程的节点保留在根部。"""
    nodes: dict[tuple, dict[str, Any]] = {}
    loose = []
    for record in records:
        run = record.get("run_id", "legacy")
        for span in record.get("spans", []):
            key = (run, span["id"])
            if key in nodes:
                _require(nodes[key]["span"] == span, "conflicting span across files")
            else:
                nodes[key] = {"span": span, "events": [], "children": [], "run_id": run}
        if record.get("span_id") is None:
            loose.append(record)
        else:
            nodes[(run, record["span_id"])]["events"].append(record)
    roots = []
    for key, node in nodes.items():
        parent = (key[0], node["span"]["parent"])
        if parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    return roots + loose, nodes


def _log_order(item: dict) -> tuple:
    stamp = item["span"]["start_time"] if "span" in item else item.get("timestamp", "")
    try:
        instant = datetime.fromisoformat(stamp).astimezone(UTC) if stamp else datetime.min.replace(tzinfo=UTC)
    except (ValueError, TypeError, OverflowError, OSError) as error:
        raise LogDecodeError("invalid timestamp") from error
    return (
        instant,
        item.get("run_id", ""),
        item.get("span", {}).get("id", item.get("seq", 0)),
    )


def _span_text(node: dict, nodes: dict[tuple, dict]) -> str:
    """只解释已观察到的起止，不推断缺失记录或业务成功。"""
    events = node["events"]
    start = next((item for item in events if item.get("metadata", {}).get("kind") == "span_start"), None)
    end = next((item for item in reversed(events) if item.get("metadata", {}).get("kind") == "span_end"), None)
    status = "未观察到结束"
    if end:
        status = str(dict(zip(end["fields"], end["args"], strict=True)).get("outcome", "未知结果"))
    if start and end:
        duration = (
            datetime.fromisoformat(end["timestamp"]) - datetime.fromisoformat(start["timestamp"])
        ).total_seconds() * 1000
        status += f" · {duration:.3f}ms"
    if not start:
        status += " · 未观察到开始"
    if node["span"]["parent"] is not None and (node["run_id"], node["span"]["parent"]) not in nodes:
        status += " · 父过程缺失"
    period = f"[{start['timestamp'] if start else '开始未观察'} → {end['timestamp'] if end else '结束未观察'}]"
    return f"{node['span']['name']} #{node['span']['id']} · {status} {period}"


def render_tree(records: Iterable[dict[str, Any]]) -> str:
    """按运行及过程标识迭代输出，避免递归复制子树文本。"""
    return _render_tree(records, None, {})


def _render_tree(records: Iterable[dict[str, Any]], unprepared: set[int] | None, times: dict[datetime, str]) -> str:
    """树结构和排序保持一致，只在受控批量调用中复用正文。"""
    roots, nodes = _tree(records)
    pending = [(item, 0) for item in reversed(sorted(roots, key=_log_order))]
    output = []
    while pending:
        item, depth = pending.pop()
        if "span" not in item:
            expanded = unprepared is not None and id(item) not in unprepared
            output.append("  " * depth + _render_record(item, prepared=expanded, times=times))
            continue
        output.append("  " * depth + _span_text(item, nodes))
        children = chain(
            item["children"],
            (
                event
                for event in item["events"]
                if event.get("metadata", {}).get("kind", "event") == "event"
                or any(field not in ("name", "outcome") for field in event.get("fields", []))
            ),
        )
        pending.extend((child, depth + 1) for child in reversed(sorted(children, key=_log_order)))
    return "\n".join(output)
