"""Malformed native frames never become a publishable staged result."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa

from axile.server import analysis_staging as staging
from axile.server.db.models.analysis_result import result_point, result_range
from axile.server.native_analysis import NativeAnalysisProtocolError


def _row(table, **overrides):
    values = {}
    for column in table.c:
        if column.name == "snapshot_id":
            continue
        if column.name == "range_key":
            value = "all"
        elif column.nullable:
            value = None
        elif isinstance(column.type, sa.Boolean):
            value = False
        elif isinstance(column.type, sa.Integer):
            value = 0
        elif isinstance(column.type, sa.Float):
            value = 0.0
        else:
            value = "value"
        values[column.name] = value
    return values | overrides


def _frame(table, values):
    columns = [column.name for column in table.c if column.name != "snapshot_id"]
    return {
        "type": "rows",
        "table": table.name,
        "columns": columns,
        "rows": [[row[column] for column in columns] for row in values],
    }


def _frames():
    return [
        _frame(result_range, [_row(result_range, range_key=key) for key in ("30", "90", "all")]),
        _frame(result_point, [_row(result_point, date="2026-01-01", observed_at="2026-01-01T09:00:00")]),
        {
            "type": "complete",
            "account_id": 2,
            "source_version": 7,
            "data_until": "2026-01-01T09:00:00",
            "row_counts": {result_range.name: 3, result_point.name: 1},
        },
    ]


class _Native:
    def __init__(self, frames):
        self.frames = frames
        self.requests = []

    @asynccontextmanager
    async def query_stream(self, command, **fields):
        self.requests.append((command, fields))

        async def iterate():
            for frame in self.frames:
                yield frame

        yield iterate()


async def _stage(native):
    return await staging.stage_publication(
        native,
        account_id=2,
        source_version=7,
        settings={"backtest_weight_type": "cs", "backtest_fee_rate": 0.0},
        trade_channel="binance",
    )


def test_valid_frames_round_trip_through_disk_and_keep_source_scope():
    native = _Native(_frames())
    staged = asyncio.run(_stage(native))
    try:
        actual = list(staged.rows())
        assert len(actual) == 2
        assert actual[0][0] == result_range.name
        assert actual[1][2][0][actual[1][1].index("account_return")] is None
        assert staged.complete["source_version"] == 7
        command, request = native.requests[0]
        assert command == "publish"
        assert request["expected_source_version"] == 7
        assert request["account_id"] == 2
    finally:
        staged.close()
    assert staged.file.closed


@pytest.mark.parametrize(
    ("table_index", "column", "value"),
    [
        (0, "record_count", "12"),
        (0, "record_count", True),
        (0, "record_count", 2**63),
        (0, "backtest_included", 1),
        (0, "engine_version", 7),
        (0, "record_count", None),
        (1, "idx", "0"),
        (1, "account_return", "1.2"),
        (1, "account_return", True),
        (1, "account_return", float("nan")),
        (1, "account_return", float("inf")),
        (1, "date", None),
    ],
)
def test_staging_rejects_sqlite_scalar_coercion_and_closes_spool(monkeypatch, table_index, column, value):
    frames = _frames()
    frames[table_index]["rows"][0][frames[table_index]["columns"].index(column)] = value
    created = []
    original = staging.tempfile.TemporaryFile

    def temporary_file(*args, **kwargs):
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(staging.tempfile, "TemporaryFile", temporary_file)
    with pytest.raises(NativeAnalysisProtocolError) as error:
        asyncio.run(_stage(_Native(frames)))
    assert error.value.code == "INVALID_NATIVE_PUBLICATION"
    assert len(created) == 1 and created[0].closed


@pytest.mark.parametrize(
    "malformed",
    [
        "counts",
        "unknown_table",
        "unknown_frame",
        "unknown_range",
        "extra_column",
        "double_complete",
        "missing_complete",
    ],
)
def test_staging_rejects_incomplete_or_mismatched_publication_frames(malformed):
    frames = _frames()
    if malformed == "counts":
        frames[-1]["row_counts"][result_point.name] = 2
    elif malformed == "unknown_table":
        frames[1]["table"] = "account"
    elif malformed == "unknown_frame":
        frames[1]["type"] = "unexpected"
    elif malformed == "unknown_range":
        frames[1]["rows"][0][frames[1]["columns"].index("range_key")] = "365"
    elif malformed == "extra_column":
        frames[1]["columns"].append("snapshot_id")
        frames[1]["rows"][0].append("untrusted-snapshot")
    elif malformed == "double_complete":
        frames.append(dict(frames[-1]))
    else:
        frames.pop()
    with pytest.raises(NativeAnalysisProtocolError) as error:
        asyncio.run(_stage(_Native(frames)))
    assert error.value.code == "INVALID_NATIVE_PUBLICATION"
