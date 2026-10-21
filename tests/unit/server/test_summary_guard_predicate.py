"""摘要保护预筛必须与原 JSON 引用判断等价，并实际短路无关正文。"""

import json
import sqlite3

import pytest

from axile.server.alembic.business0037_invalidation import _summary_reference_exists


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema():
    yield


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
        "CREATE TABLE detail(record_id INTEGER,value INTEGER);"
    )
    connection.execute("INSERT INTO execution_artifact VALUES('execution',?)", (content,))
    return connection


@pytest.mark.parametrize("content", _cases())
def test_summary_prefilter_preserves_reference_predicate(content):
    with _connection(content) as connection:
        for dependency in ("1", "0"):
            previous = _summary_reference_exists("SELECT 1", dependency, optimize=False)
            optimized = _summary_reference_exists("SELECT 1", dependency)
            assert (
                connection.execute("SELECT " + optimized).fetchone()
                == connection.execute("SELECT " + previous).fetchone()
            )


def test_summary_prefilter_bypasses_all_json_parsing_for_unrelated_text():
    content = json.dumps({"summary": "orders" * 120000})
    with _connection(content) as connection, sqlite3.connect(":memory:") as oracle:
        calls = []

        def valid(value):
            calls.append(value)
            return oracle.execute("SELECT json_valid(?)", (value,)).fetchone()[0]

        connection.create_function("json_valid", 1, valid)
        assert connection.execute("SELECT " + _summary_reference_exists("SELECT 1")).fetchone() == (0,)
        assert calls == []
        assert connection.execute("SELECT " + _summary_reference_exists("SELECT 1", optimize=False)).fetchone() == (0,)
        assert calls


@pytest.mark.parametrize("action", ["INSERT", "UPDATE", "DELETE"])
def test_summary_reference_still_aborts_actual_detail_mutation(action):
    content = '{"\\u0062usiness_facts_ref":{"view":"result","record_id":1}}'
    with _connection(content) as connection:
        connection.execute("INSERT INTO detail VALUES(1,1)")
        ref = "NEW" if action == "INSERT" else "OLD"
        predicate = _summary_reference_exists(f"SELECT {ref}.record_id")
        connection.execute(
            f"CREATE TRIGGER guard BEFORE {action} ON detail BEGIN "
            "SELECT RAISE(ABORT,'Execution summary must be materialized') WHERE " + predicate + "; END"
        )
        statement = {
            "INSERT": "INSERT INTO detail VALUES(1,2)",
            "UPDATE": "UPDATE detail SET value=2",
            "DELETE": "DELETE FROM detail",
        }[action]
        with pytest.raises(sqlite3.IntegrityError, match="summary must be materialized"):
            connection.execute(statement)


def test_binary_json_preserves_previous_behavior_when_available():
    with _connection(None) as connection:
        try:
            blob = connection.execute(
                "SELECT jsonb(?)", ('{"business_facts_ref":{"view":"result","record_id":1}}',)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pytest.skip("SQLite build has no JSONB support")
        connection.execute("UPDATE execution_artifact SET content=?", (blob,))
        previous = _summary_reference_exists("SELECT 1", optimize=False)
        optimized = _summary_reference_exists("SELECT 1")
        assert (
            connection.execute("SELECT " + optimized).fetchone() == connection.execute("SELECT " + previous).fetchone()
        )
