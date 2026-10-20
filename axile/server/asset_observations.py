"""共享资产观测口径；保留失败审计，仅排除可精确识别的占位值。"""

import sys
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlmodel import col

from axile.executor.models.unified_account_assets import is_degraded_snapshot_source
from axile.server.business_facts.assets import asset_snapshot
from axile.server.db.models import AccountAssetSnapshot


def is_legacy_placeholder(result: Mapping[str, Any]) -> bool:
    """识别旧构造器的完整结构，不按失败状态或错误文案猜测余额。"""
    assets = result.get("account_assets")
    if not isinstance(assets, dict) or assets.get("source", "real") != "real":
        return False
    if not all(
        type(assets.get(key)) in (int, float) and assets[key] == 0
        for key in ("available_cash", "total_asset", "market_value")
    ):
        return False
    if assets.get("positions") != [] or assets.get("extra") != {} or result.get("symbol_results") != {}:
        return False
    return _worker_placeholder(result) or _blocked_placeholder(result)


def _worker_placeholder(result: Mapping[str, Any]) -> bool:
    extra = result.get("extra")
    return (
        result.get("status") == "FAILED"
        and isinstance(extra, dict)
        and isinstance(extra.get("worker_error"), dict)
        and result.get("inputs") in (None, {})
        and type(result.get("execution_time")) in (int, float)
        and result["execution_time"] == 0
    )


def _blocked_placeholder(result: Mapping[str, Any]) -> bool:
    return (
        result.get("status") == "BLOCKED"
        and result.get("error") == "当前不在交易时间"
        and result.get("memory") == {"message": "当前不在交易时间"}
        and isinstance(result.get("inputs"), dict)
        and type(result.get("execution_time")) in (int, float)
        and result["execution_time"] >= 0
    )


def is_asset_observation(assets: object, result: Mapping[str, Any] | None = None) -> bool:
    """真实零余额有效；缺失、非有限权益与未取得资产均不是观测。"""
    if not isinstance(assets, dict) or is_degraded_snapshot_source(assets.get("source")):
        return False
    value = assets.get("total_asset")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -sys.float_info.max <= value <= sys.float_info.max
        and (result is None or not is_legacy_placeholder(result))
    )


def _json_value(document, path):
    return sa.func.json_extract(document, path)


def _numeric(document, path):
    return sa.func.json_type(document, path).in_(["integer", "real"])


def legacy_placeholder_condition(document):
    """SQLite JSON 谓词，与 Python 历史识别规则逐项对应。"""

    def get(path):
        return _json_value(document, path)

    zero = sa.and_(
        *[
            sa.and_(_numeric(document, f"$.account_assets.{key}"), get(f"$.account_assets.{key}") == 0)
            for key in ("available_cash", "total_asset", "market_value")
        ]
    )
    worker = sa.and_(
        get("$.status") == "FAILED",
        sa.func.json_type(document, "$.extra.worker_error") == "object",
        sa.or_(
            get("$.inputs").is_(None),
            sa.and_(sa.func.json_type(document, "$.inputs") == "object", get("$.inputs") == "{}"),
        ),
        _numeric(document, "$.execution_time"),
        get("$.execution_time") == 0,
    )
    blocked = sa.and_(
        get("$.status") == "BLOCKED",
        get("$.error") == "当前不在交易时间",
        get("$.memory.message") == "当前不在交易时间",
        sa.select(sa.func.count())
        .select_from(sa.func.json_each(document, "$.memory").table_valued("key"))
        .scalar_subquery()
        == 1,
        sa.func.json_type(document, "$.inputs") == "object",
        _numeric(document, "$.execution_time"),
        get("$.execution_time") >= 0,
    )
    return sa.func.coalesce(
        sa.and_(
            sa.or_(
                sa.func.json_type(document, "$.account_assets.source").is_(None),
                get("$.account_assets.source") == "real",
            ),
            zero,
            sa.func.json_type(document, "$.account_assets.positions") == "array",
            get("$.account_assets.positions") == "[]",
            sa.func.json_type(document, "$.account_assets.extra") == "object",
            get("$.account_assets.extra") == "{}",
            sa.func.json_type(document, "$.symbol_results") == "object",
            get("$.symbol_results") == "{}",
            sa.or_(worker, blocked),
        ),
        False,
    )


def valid_asset_snapshot_condition():
    """在排序、分页前使用迁移或写入时冻结的资产有效性。"""
    return sa.exists(
        sa.select(1).where(
            asset_snapshot.c.asset_id == col(AccountAssetSnapshot.asset_fact_id),
            asset_snapshot.c.observation_valid.is_(True),
        )
    )
