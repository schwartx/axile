"""回放输入的 JSON 和安全字段校验；状态投影由 Rust 负责。"""

import json
import math
from collections.abc import Mapping

MAX_SAFE_INTEGER = 2**53 - 1
MAX_FREEZE_BYTES = 512 * 1024
MAX_FREEZE_NODES = 16_384

DATA_FIELDS = frozenset(
    "schema_version symbol symbols source source_seq source_time_us source_time_quality source_precision "
    "source_quality query_started_us query_finished_us received_time_us publish_time_us "
    "field_quality attributes native_identity execution_id origin operation command_id request_id phase kind "
    "reason error_type success outcome accepted actual_parameters requested_parameters order_id client_order_id "
    "direction position_side offset order_type price quantity filled_quantity remaining_quantity average_price "
    "status on_book confirmed_book order_stage reduce_only close_position time_in_force trigger_price trigger_condition "
    "replacement_order_id previous_order_id replaced_by command_phase create_time_us update_time_us "
    "trade_key identity_quality granularity aggressor is_maker fee fee_currency execution_type "
    "association related_capture_seq order_identity trade_identity associated_execution_id "
    "bids asks available_depth depth_coverage book_valid valid_until_us last_price last_price_kind "
    "volume volume_kind turnover scope scope_complete sync_generation synchronized query_basis orders "
    "positions position_basis target target_quantity targets algorithm stage stage_id stage_targets ending_reason instrument streams "
    "connected ready available availability boundary complete start_us end_us details environment "
    "position_mode delivery_method delivery intermediate_states source_event_id source_times trade_id assets return_code error_id "
    "today_quantity yesterday_quantity available_quantity frozen_quantity long_frozen short_frozen".split()
)
_FORBIDDEN_KEYS = frozenset(
    "password passwd secret api_key apikey api_secret apisecret access_token refresh_token authorization credentials "
    "account_password broker_password private_key privatekey signing_key auth_token cookie cookies session_token "
    "account_config raw raw_data raw_trade_data raw_order_data raw_position_data extra".split()
)
_TOPICS = frozenset({"/execution", "/command", "/order", "/order_set", "/trade", "/account_observation", "/connection"})


def _number(value: int | float) -> int | float:
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        raise ValueError("replay_integer_range")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("replay_nonfinite_number")
    return value


def freeze_json(value: object) -> object:
    """有界复制 JSON 原语，在分配容器前检查节点和字节预算。"""
    remaining_bytes = MAX_FREEZE_BYTES
    remaining_nodes = MAX_FREEZE_NODES

    def charge(size: int) -> None:
        nonlocal remaining_bytes
        remaining_bytes -= size
        if remaining_bytes < 0:
            raise ValueError("replay_capture_budget")

    def visit(item: object, depth: int) -> object:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0:
            raise ValueError("replay_structure_budget")
        if depth > 10:
            raise ValueError("replay_structure_budget")
        charge(2)
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, (int, float)):
            charge(24)
            return _number(item)
        if isinstance(item, str):
            if len(item) > MAX_FREEZE_BYTES:
                raise ValueError("replay_capture_budget")
            charge(len(item.encode("utf-8")))
            return item
        if isinstance(item, Mapping):
            if len(item) > remaining_nodes:
                raise ValueError("replay_structure_budget")
            result = {}
            for key, nested in item.items():
                if not isinstance(key, str) or key.lower() in _FORBIDDEN_KEYS:
                    raise ValueError("replay_forbidden_field")
                if len(key) > 128 or len(key.encode("utf-8")) > 128:
                    raise ValueError("replay_key_budget")
                charge(len(key.encode("utf-8")) + 3)
                result[key] = visit(nested, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            if len(item) > remaining_nodes:
                raise ValueError("replay_structure_budget")
            return [visit(nested, depth + 1) for nested in item]
        raise ValueError("replay_non_json_value")

    return visit(value, 0)


def freeze_data(topic: str, data: Mapping[str, object], context: Mapping[str, object]) -> dict[str, object]:
    """按通用字段及渠道声明冻结一份回放观察。"""
    if topic not in _TOPICS and not topic.startswith(("/quote/", "/market_trade/")):
        raise ValueError("replay_unknown_topic")
    if topic.startswith(("/quote/", "/market_trade/")) and not topic.rsplit("/", 1)[-1]:
        raise ValueError("replay_missing_symbol")
    if not isinstance(data, Mapping) or len(data) > len(DATA_FIELDS) or set(data) - DATA_FIELDS:
        raise ValueError("replay_unknown_data_field")
    frozen = freeze_json(data)
    assert isinstance(frozen, dict)
    kind = topic.strip("/").split("/", 1)[0]
    _validate_extensions(topic, kind, frozen, context)
    _validate_rows(frozen, context)
    for name in ("symbol", "order_id", "execution_id", "command_id", "trade_key"):
        value = frozen.get(name)
        if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 1_024):
            raise ValueError("replay_identity_budget")
    source_times = frozen.get("source_times", {})
    if (
        not isinstance(source_times, dict)
        or len(source_times) > 32
        or any(not isinstance(value, (str, int, float, bool, type(None))) for value in source_times.values())
    ):
        raise ValueError("replay_invalid_source_times")
    assets = frozen.get("assets", {})
    if not isinstance(assets, dict) or set(assets) - {"balance", "available_cash", "frozen_margin"}:
        raise ValueError("replay_invalid_assets")
    return frozen


def _validate_extensions(topic: str, kind: str, data: dict[str, object], context: Mapping[str, object]) -> None:
    for field, declaration in (("attributes", "attribute_fields"), ("native_identity", "native_identity_fields")):
        values = data.get(field, {})
        declarations = context.get(field, context.get(declaration, {}))
        allowed = declarations.get(topic, declarations.get(kind, [])) if isinstance(declarations, dict) else []
        if not isinstance(values, dict) or not isinstance(allowed, list) or set(values) - set(allowed):
            raise ValueError("replay_undeclared_extension")
        if any(isinstance(value, (dict, list)) for value in values.values()):
            raise ValueError("replay_extension_requires_scalar")


def _validate_rows(data: dict[str, object], context: Mapping[str, object]) -> None:
    for name, kind in (("orders", "order"), ("positions", "position")):
        rows = data.get(name, [])
        if not isinstance(rows, list):
            raise ValueError("replay_invalid_rows")
        for row in rows:
            if not isinstance(row, dict) or set(row) - DATA_FIELDS:
                raise ValueError("replay_unknown_row_field")
            if "orders" in row or "positions" in row:
                raise ValueError("replay_invalid_rows")
            _validate_extensions(f"/{kind}", kind, row, context)


def encode_json(value: object) -> str:
    """生成唯一队列持有的 UTF-8 JSON 文本，禁止非有限数。"""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
