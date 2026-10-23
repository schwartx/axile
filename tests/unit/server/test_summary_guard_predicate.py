"""摘要保护预筛必须与原 JSON 引用判断等价，并实际短路无关正文。"""

import json
import sqlite3
from contextlib import closing

import pytest

from tests.conftest import initialize_native_database


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema():
    yield


@pytest.fixture(scope="session", params=("0038", "0039"))
def summary_guards(request, tmp_path_factory):
    database = tmp_path_factory.mktemp("summary-guards") / "guards.sqlite"
    initialize_native_database(database, revision=request.param)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (request.param,)
        guards = {}
        for action in ("INSERT", "UPDATE", "DELETE"):
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"business_facts_0037_execution_extension_summary_{action.lower()}",),
            ).fetchone()
            assert row is not None
            guards[action] = row[0]
    return guards


def _unoptimized_reference_exists(record_scope: str, dependency: str = "1") -> str:
    """冻结 0037 的原始判断，独立核验原生迁移安装的 SQL。"""
    return f"""EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN ({record_scope}))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN ({record_scope})
        AND ({dependency}))"""


def _guard_result(connection, guard, *, changed=True):
    _, separator, predicate = guard.partition(" WHERE ")
    assert separator and predicate.endswith("; END")
    return connection.execute(
        "SELECT "
        + predicate.removesuffix("; END")
        + " FROM (SELECT 1 AS record_id,'object' AS root_kind,'1' AS root_value) AS OLD"
        " CROSS JOIN (SELECT 1 AS record_id,? AS root_kind,'1' AS root_value) AS NEW",
        ("value" if changed else "object",),
    ).fetchone()


def _assert_guard_matches_oracle(connection, guards):
    for action, guard in guards.items():
        for dependency in ("1", "0") if action == "UPDATE" else ("1",):
            previous = _unoptimized_reference_exists("SELECT 1", dependency)
            assert (
                _guard_result(connection, guard, changed=dependency == "1")
                == connection.execute("SELECT " + previous).fetchone()
            ), (action, dependency)


def _cases():
    reference = {"business_facts_ref": {"view": "result", "record_id": 1}}
    encoded = json.dumps(reference)
    cases = [
        ("literal", encoded),
        (
            "escaped-all",
            '{"'
            + "".join(f"\\u{ord(char):04x}" for char in "business_facts_ref")
            + '": {"view":"result","record_id":1}}',
        ),
        ("unrelated", json.dumps({"summary": "普通摘要", "orders": [1, 2]})),
        ("marker-in-value", json.dumps({"summary": "business_facts_ref"})),
        ("unicode-in-value", '{"summary":"\\u0061"}'),
        ("wrong-view", json.dumps({"business_facts_ref": {"view": "input", "record_id": 1}})),
        ("wrong-record", json.dumps({"business_facts_ref": {"view": "result", "record_id": 2}})),
        ("missing-id", '{"business_facts_ref":{"view":"result"}}'),
        ("missing-view", '{"business_facts_ref":{"record_id":1}}'),
        ("duplicate-key", '{"business_facts_ref":null,"business_facts_ref":{"view":"result","record_id":1}}'),
        ("null-id", '{"business_facts_ref":{"view":"result","record_id":null}}'),
        ("zero-id", '{"business_facts_ref":{"view":"result","record_id":0}}'),
        ("float-id", '{"business_facts_ref":{"view":"result","record_id":1.0}}'),
        ("string-id", '{"business_facts_ref":{"view":"result","record_id":"1"}}'),
        ("boolean-id", '{"business_facts_ref":{"view":"result","record_id":true}}'),
        ("array", "[]"),
        ("null", "null"),
        ("sql-null", None),
        ("integer", 1),
        ("float", 1.0),
        ("string", '"business_facts_ref"'),
        ("malformed", "{broken"),
        ("malformed-marker", "{business_facts_ref"),
        ("empty", ""),
        ("nul", '{"x":"\0","business_facts_ref":{"view":"result","record_id":1}}'),
        ("blob-json", encoded.encode()),
        ("blob-malformed", b"{business_facts_ref"),
        ("blob-nonutf8", b"\xff\xfe"),
    ]
    for index, character in enumerate("business_facts_ref"):
        key = "business_facts_ref"[:index] + f"\\u{ord(character):04x}" + "business_facts_ref"[index + 1 :]
        cases.append((f"escaped-key-{index}", '{"' + key + '":{"view":"result","record_id":1}}'))
    for key in ("view", "record_id"):
        escaped = "".join(f"\\u{ord(character):04x}" for character in key)
        cases.append((f"escaped-{key}", encoded.replace('"' + key + '"', '"' + escaped + '"')))
    cases.extend(
        (f"json-scalar-{index}", json.dumps(value))
        for index, value in enumerate(
            (True, False, 0, 0.0, {}, {"business_facts_ref": []}, {"business_facts_ref": None})
        )
    )
    return [pytest.param(content, id=name) for name, content in cases]


def _connection(content):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,execution_id TEXT);"
        "INSERT INTO executerecord VALUES(1,'execution');"
        "CREATE TABLE execution_artifact(execution_id TEXT,content);"
        "CREATE TABLE execution_extension(record_id INTEGER,root_kind TEXT,root_value TEXT);"
    )
    connection.execute("INSERT INTO execution_artifact VALUES('execution',?)", (content,))
    return connection


@pytest.mark.parametrize("content", _cases())
def test_summary_prefilter_preserves_reference_predicate(content, summary_guards):
    with closing(_connection(content)) as connection:
        _assert_guard_matches_oracle(connection, summary_guards)


def test_summary_prefilter_bypasses_all_json_parsing_for_unrelated_text(summary_guards):
    content = json.dumps({"summary": "orders" * 120000})
    with closing(_connection(content)) as connection, closing(sqlite3.connect(":memory:")) as oracle:
        calls = []

        def valid(value):
            calls.append(value)
            return oracle.execute("SELECT json_valid(?)", (value,)).fetchone()[0]

        connection.create_function("json_valid", 1, valid)
        assert _guard_result(connection, summary_guards["INSERT"]) == (0,)
        assert calls == []
        assert connection.execute("SELECT " + _unoptimized_reference_exists("SELECT 1")).fetchone() == (0,)
        assert calls


@pytest.mark.parametrize("action", ["INSERT", "UPDATE", "DELETE"])
def test_summary_reference_still_aborts_actual_detail_mutation(action, summary_guards):
    content = '{"\\u0062usiness_facts_ref":{"view":"result","record_id":1}}'
    with closing(_connection(content)) as connection:
        connection.execute("INSERT INTO execution_extension VALUES(1,'object','1')")
        connection.execute(summary_guards[action])
        statement = {
            "INSERT": "INSERT INTO execution_extension VALUES(1,'object','2')",
            "UPDATE": "UPDATE execution_extension SET root_value='2'",
            "DELETE": "DELETE FROM execution_extension",
        }[action]
        with pytest.raises(sqlite3.IntegrityError, match="summary must be materialized"):
            connection.execute(statement)
        assert connection.execute("SELECT * FROM execution_extension").fetchall() == [(1, "object", "1")]


def test_binary_json_preserves_previous_behavior_when_available(summary_guards):
    with closing(_connection(None)) as connection:
        try:
            blob = connection.execute(
                "SELECT jsonb(?)", ('{"business_facts_ref":{"view":"result","record_id":1}}',)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pytest.skip("SQLite build has no JSONB support")
        connection.execute("UPDATE execution_artifact SET content=?", (blob,))
        _assert_guard_matches_oracle(connection, summary_guards)
