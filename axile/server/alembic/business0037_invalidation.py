"""Revision 0037 SQLite ownership and invalidation invariants.

Business tables are the only trigger sources here. Publishing analysis rows
never changes business facts and therefore cannot invalidate its own output.
"""

from collections.abc import Sequence

import sqlalchemy as sa

_PREFIX = "business_facts_0037_"
_OBSOLETE = ("analysis_facts_record_update", "analysis_structured_record_update")
_DERIVED = (
    "analysis_source_transaction_fee",
    "analysis_source_transaction",
    "analysis_source_attempt",
    "analysis_source_position",
    "analysis_source_execution_symbol",
    "analysis_source_trade",
    "analysis_source_execution",
    "replay_observation_symbol",
    "replay_observation",
)


def _quote(name: str) -> str:
    """Quote only trusted schema identifiers, never business payload values."""
    return '"' + name.replace('"', '""') + '"'


def _column(ref: str, name: str) -> str:
    return f"{ref}.{_quote(name)}"


def _ownership(table: sa.Table, ref: str, tables: dict[str, sa.Table], seen: frozenset[str] = frozenset()):
    """Resolve direct owners and parent-owned rows without loading any payload."""
    owners: dict[str, list[str]] = {
        "record": [],
        "input": [],
        "snapshot": [],
        "account": [],
        "asset": [],
        "execution": [],
    }
    direct = {"record_id": "record", "input_id": "input", "account_snapshot_id": "snapshot", "account_id": "account"}
    for name, role in direct.items():
        if name in table.c:
            owners[role].append(_column(ref, name))
    if table.name == "executerecord":
        owners["record"].append(_column(ref, "id"))
    if table.name == "execution_input":
        owners["input"].append(_column(ref, "id"))
    if table.name == "account_asset_snapshot":
        owners["snapshot"].append(_column(ref, "id"))
    if table.name == "target_weight_snapshot":
        owners["execution"].append(_column(ref, "execution_id"))
    if "asset_id" in table.c:
        owners["asset"].append(_column(ref, "asset_id"))
    if table.name in seen:
        return owners
    for constraint in table.foreign_key_constraints:
        keys = list(constraint.elements)
        parent_name = keys[0].target_fullname.rsplit(".", 1)[0]
        if all(key.parent.name in direct for key in keys) or parent_name not in tables:
            continue
        parent = tables[parent_name]
        alias = "parent_" + str(len(seen))
        inherited = _ownership(parent, alias, tables, seen | {table.name})
        predicate = " AND ".join(
            f"{_column(alias, key.target_fullname.rsplit('.', 1)[1])} IS {_column(ref, key.parent.name)}"
            for key in keys
        )
        for role, expressions in inherited.items():
            owners[role].extend(
                f"(SELECT {expression} FROM {_quote(parent_name)} AS {alias} WHERE {predicate})"
                for expression in expressions
            )
    return owners


def _scopes(table: sa.Table, ref: str, tables: dict[str, sa.Table]) -> tuple[str, str]:
    owners = _ownership(table, ref, tables)
    records = [f"SELECT {value} WHERE {value} IS NOT NULL" for value in owners["record"]]
    records.extend(
        f"SELECT id FROM executerecord WHERE {value} IS NOT NULL "
        f"AND (input_id IS {value} OR result_input_id IS {value})"
        for value in owners["input"]
    )
    records.extend(
        "SELECT e.id FROM executerecord AS e JOIN account_asset_snapshot AS s "
        "ON e.execution_id=s.execution_id AND e.account_id=s.account_id "
        f"WHERE s.id IS {value} AND s.execution_id IS NOT NULL"
        for value in owners["snapshot"]
    )
    if table.name == "account_asset_snapshot":
        execution_id = _column(ref, "execution_id")
        records.append(
            f"SELECT id FROM executerecord WHERE execution_id IS {execution_id} AND {execution_id} IS NOT NULL "
            f"AND account_id IS {_column(ref, 'account_id')}"
        )
    records.extend(
        f"SELECT id FROM executerecord WHERE execution_id IS {value} AND {value} IS NOT NULL"
        for value in owners["execution"]
    )
    if "business_asset_binding" in tables:
        records.extend(
            f"SELECT record_id FROM business_asset_binding WHERE asset_id IS {value}" for value in owners["asset"]
        )
    accounts = [f"SELECT id FROM account WHERE id IS {value}" for value in owners["account"]]
    accounts.extend(f"SELECT account_id FROM executerecord WHERE id IS {value}" for value in owners["record"])
    accounts.extend(f"SELECT account_id FROM execution_input WHERE id IS {value}" for value in owners["input"])
    accounts.extend(
        f"SELECT account_id FROM account_asset_snapshot WHERE id IS {value}" for value in owners["snapshot"]
    )
    if "business_asset_binding" in tables:
        accounts.extend(
            "SELECT account_id FROM executerecord WHERE id IN "
            f"(SELECT record_id FROM business_asset_binding WHERE asset_id IS {value})"
            for value in owners["asset"]
        )
    empty = "SELECT NULL WHERE 0"
    return " UNION ".join(records) or empty, " UNION ".join(accounts) or empty


def _invalidate(records: str, accounts: str, *, bump: bool = True) -> str:
    statements = " ".join(f"DELETE FROM {name} WHERE record_id IN ({records});" for name in _DERIVED)
    if bump:
        statements += f""" INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN ({accounts}) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL;"""
    return statements


def drop_triggers(connection) -> None:
    """Remove only this revision's triggers before restoring the older schema."""
    names = connection.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE :prefix"),
        {"prefix": _PREFIX + "%"},
    ).scalars()
    for name in list(names):
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_quote(name)}")


def _install_invalidation(connection, tables: dict[str, sa.Table]) -> None:
    for table in tables.values():
        for action in ("INSERT", "UPDATE", "DELETE"):
            refs = ("OLD", "NEW") if action == "UPDATE" else ("OLD",) if action == "DELETE" else ("NEW",)
            scopes = [_scopes(table, ref, tables) for ref in refs]
            record_scope = " UNION ".join(scope[0] for scope in scopes)
            account_scope = " UNION ".join(scope[1] for scope in scopes)
            # Revision 0012 already versions terminal records in every write path.
            body = _invalidate(record_scope, account_scope, bump=table.name != "executerecord")
            condition = ""
            records = tables.get("executerecord")
            if (
                action == "INSERT"
                and records is not None
                and "facts_complete" in records.c
                and table.name not in {"executerecord", "account_asset_snapshot", "target_weight_snapshot"}
            ):
                # A new result is assembled atomically before any projection is published.
                # Its parent versions the account; child rows must not repeat nine DELETEs
                # and an account UPDATE each. Later appends to a complete graph still invalidate.
                ready = f"EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN ({record_scope}))"
                if table.name == "business_asset_snapshot":
                    ready = f"(NEW.account_snapshot_id IS NOT NULL OR {ready})"
                condition = f" WHEN {ready}"
            name = _PREFIX + table.name + "_" + action.lower()
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(name)} AFTER {action} ON {_quote(table.name)}{condition} BEGIN {body} END"
            )


def _install_input_guards(connection) -> None:
    for action in ("INSERT", "UPDATE"):
        name = _PREFIX + "record_input_" + action.lower()
        checks = " ".join(
            f"SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.{column} IS NOT NULL "
            f"AND NOT EXISTS(SELECT 1 FROM execution_input WHERE id=NEW.{column} AND account_id=NEW.account_id);"
            for column in ("input_id", "result_input_id")
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON executerecord BEGIN {checks} END"
        )
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}input_owner_update BEFORE UPDATE OF account_id,id ON execution_input "
        "BEGIN SELECT RAISE(ABORT,'Execution input owner mismatch') "
        "WHERE NEW.account_id IS NOT OLD.account_id OR NEW.id IS NOT OLD.id; END"
    )
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}input_delete_guard BEFORE DELETE ON execution_input "
        "BEGIN SELECT RAISE(ABORT,'Execution input is still referenced') WHERE EXISTS(SELECT 1 FROM executerecord "
        "WHERE (input_id=OLD.id OR result_input_id=OLD.id) "
        "AND EXISTS(SELECT 1 FROM account WHERE id=executerecord.account_id)); END"
    )


def _install_parent_guards(connection, tables: dict[str, sa.Table]) -> None:
    """Retain owner existence invariants even when SQLite foreign keys are off."""
    for table in tables.values():
        for constraint in table.foreign_key_constraints:
            keys = list(constraint.elements)
            parent_name = keys[0].target_fullname.rsplit(".", 1)[0]
            if parent_name not in tables:
                continue
            present = " AND ".join(f"{_column('NEW', key.parent.name)} IS NOT NULL" for key in keys)
            predicate = " AND ".join(
                f"{_quote(key.target_fullname.rsplit('.', 1)[1])} IS {_column('NEW', key.parent.name)}" for key in keys
            )
            suffix = "_".join(key.parent.name for key in keys)
            for action in ("INSERT", "UPDATE"):
                name = _PREFIX + table.name + "_owner_" + suffix + "_" + action.lower()
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS {_quote(name)} BEFORE {action} ON {_quote(table.name)} "
                    f"BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE {present} "
                    f"AND NOT EXISTS(SELECT 1 FROM {_quote(parent_name)} WHERE {predicate}); END"
                )


def _result_record_scope(table: sa.Table, ref: str, tables: dict[str, sa.Table]) -> str:
    """Follow result ownership, excluding independent account and target observations."""
    if table.name in {"account_asset_snapshot", "target_weight_snapshot", "target_weight_snapshot_weight"}:
        return "SELECT NULL WHERE 0"
    owners = _ownership(table, ref, tables)
    queries = [f"SELECT {value} WHERE {value} IS NOT NULL" for value in owners["record"]]
    if "business_asset_binding" in tables:
        queries.extend(
            f"SELECT record_id FROM business_asset_binding WHERE asset_id IS {value}" for value in owners["asset"]
        )
    return " UNION ".join(queries) or "SELECT NULL WHERE 0"


def _summary_reference_exists(record_scope: str, dependency: str = "1") -> str:
    return f"""EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN ({record_scope}))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN ({record_scope})
        AND ({dependency}))"""


def _summary_dependency(table: sa.Table, ref: str) -> str | None:
    """业务引用冻结所属结果图，不扫描逐字段 JSON 指针。"""
    if table.name in {"account_asset_snapshot", "target_weight_snapshot", "target_weight_snapshot_weight"}:
        return None
    return "1"


def _summary_sensitive_update(table: sa.Table) -> str:
    identity = {
        "executerecord": ("id", "account_id", "execution_id", "input_id", "result_input_id"),
        "execution_extension": ("root_kind", "root_value"),
        "execution_symbol_result": (
            "record_id",
            "path",
            "ordinal",
            "map_key",
            "node_kind",
            "invalid_value",
            "audit_evidence",
            "memory",
        ),
    }
    fields = identity.get(table.name)
    return " OR ".join(f"{_column('OLD', key)} IS NOT {_column('NEW', key)}" for key in fields) if fields else "1"


def _install_summary_guards(connection, tables: dict[str, sa.Table]) -> None:
    """Require materialization before changing a result referenced by a frozen journal."""
    for table in tables.values():
        for action in ("INSERT", "UPDATE", "DELETE"):
            ref = "NEW" if action == "INSERT" else "OLD"
            dependency = _summary_dependency(table, ref)
            if dependency is None:
                continue
            scope = _result_record_scope(table, ref, tables)
            if action == "UPDATE":
                scope += " UNION " + _result_record_scope(table, "NEW", tables)
                dependency = f"(({dependency}) OR ({_summary_dependency(table, 'NEW')}))"
                dependency += f" AND ({_summary_sensitive_update(table)})"
            name = _PREFIX + table.name + "_summary_" + action.lower()
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(name)} BEFORE {action} ON {_quote(table.name)} BEGIN "
                "SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') "
                f"WHERE {_summary_reference_exists(scope, dependency)}; END"
            )


def _install_outbox_guards(connection, tables: dict[str, sa.Table]) -> None:
    if "execution_extension" not in tables:
        return
    referenced = """EXISTS(SELECT 1 FROM execution_extension WHERE
        CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context."$axile_notification_context"') END=1
        AND CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context.event_id') END=OLD.id)"""
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}notification_outbox_insert BEFORE INSERT ON notification_event "
        "BEGIN SELECT RAISE(ABORT,'Frozen notification context still references this outbox') "
        f"WHERE {referenced.replace('OLD.id', 'NEW.id')} AND NOT EXISTS(SELECT 1 FROM notification_event "
        "WHERE id=NEW.id AND account_id IS NEW.account_id AND event_type IS NEW.event_type AND context IS NEW.context); END"
    )
    for action in ("UPDATE", "DELETE"):
        changed = (
            "1"
            if action == "DELETE"
            else " OR ".join(
                f"{_column('OLD', key)} IS NOT {_column('NEW', key)}"
                for key in ("id", "account_id", "event_type", "context")
            )
        )
        update_columns = " OF id,account_id,event_type,context" if action == "UPDATE" else ""
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}notification_outbox_{action.lower()} BEFORE {action}{update_columns} "
            "ON notification_event BEGIN SELECT RAISE(ABORT,'Frozen notification context still references this outbox') "
            f"WHERE ({changed}) AND {referenced}; END"
        )


def _install_asset_guards(connection, tables: dict[str, sa.Table]) -> None:
    if "business_asset_snapshot" not in tables or "business_asset_binding" not in tables:
        return
    owner = """SELECT COALESCE(r.account_id,i.account_id,s.account_id)
        FROM business_asset_snapshot AS a
        LEFT JOIN executerecord AS r ON r.id=a.record_id
        LEFT JOIN execution_input AS i ON i.id=a.input_id
        LEFT JOIN account_asset_snapshot AS s ON s.id=a.account_snapshot_id
        WHERE a.asset_id=NEW.asset_id"""
    for action in ("INSERT", "UPDATE"):
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}asset_binding_owner_{action.lower()} "
            f"BEFORE {action} ON business_asset_binding BEGIN "
            f"SELECT RAISE(ABORT,'Asset binding owner mismatch') WHERE ({owner}) IS NOT "
            "(SELECT account_id FROM executerecord WHERE id=NEW.record_id); END"
        )
    new_owner = """COALESCE((SELECT account_id FROM executerecord WHERE id=NEW.record_id),
        (SELECT account_id FROM execution_input WHERE id=NEW.input_id),
        (SELECT account_id FROM account_asset_snapshot WHERE id=NEW.account_snapshot_id))"""
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}asset_owner_update BEFORE UPDATE ON business_asset_snapshot BEGIN "
        "SELECT RAISE(ABORT,'Asset identity is immutable') WHERE NEW.asset_id IS NOT OLD.asset_id; "
        f"SELECT RAISE(ABORT,'Asset owner mismatch') WHERE EXISTS(SELECT 1 FROM business_asset_binding AS b "
        "JOIN executerecord AS r ON r.id=b.record_id "
        f"WHERE b.asset_id=OLD.asset_id AND r.account_id IS NOT ({new_owner})) "
        "OR EXISTS(SELECT 1 FROM account_asset_snapshot AS s "
        f"WHERE s.asset_fact_id=OLD.asset_id AND s.account_id IS NOT ({new_owner})); END"
    )
    for action in ("INSERT", "UPDATE"):
        snapshot_owner = owner.replace("NEW.asset_id", "NEW.asset_fact_id")
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}snapshot_asset_owner_{action.lower()} "
            f"BEFORE {action} ON account_asset_snapshot WHEN NEW.asset_fact_id IS NOT NULL BEGIN "
            f"SELECT RAISE(ABORT,'Snapshot asset owner mismatch') WHERE ({snapshot_owner}) IS NOT NEW.account_id; END"
        )
    retained = """EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id=a.asset_id)
        OR EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
            THEN json_extract(content,'$.business_facts_ref.asset_id') END=a.asset_id)"""
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}record_retain_assets BEFORE DELETE ON executerecord "
        "WHEN EXISTS(SELECT 1 FROM account WHERE id=OLD.account_id) BEGIN "
        "SELECT RAISE(ABORT,'Referenced asset requires a durable input owner') WHERE OLD.input_id IS NULL "
        f"AND EXISTS(SELECT 1 FROM business_asset_snapshot AS a WHERE a.record_id=OLD.id AND ({retained})); "
        "UPDATE business_asset_snapshot SET record_id=NULL,input_id=OLD.input_id "
        f"WHERE record_id=OLD.id AND ({retained.replace('a.', 'business_asset_snapshot.')}); END"
    )


def _install_cleanup(connection, tables: dict[str, sa.Table]) -> None:
    result_children = [table for table in tables.values() if table.name != "executerecord" and "record_id" in table.c]
    deletes = " ".join(
        f"DELETE FROM {_quote(table.name)} WHERE record_id=OLD.id;" for table in reversed(result_children)
    )
    if deletes:
        orphan_cleanup = ""
        if "execution_input" in tables:
            orphan_cleanup = """ DELETE FROM execution_input
                WHERE id IN (OLD.input_id,OLD.result_input_id)
                AND NOT EXISTS(SELECT 1 FROM executerecord WHERE input_id=execution_input.id OR result_input_id=execution_input.id)
                AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
                    THEN json_extract(content,'$.business_facts_ref.input_id') END=execution_input.id)
                AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
                    THEN json_extract(content,'$.business_facts_ref.asset_id') END IN
                    (SELECT asset_id FROM business_asset_snapshot WHERE input_id=execution_input.id))
                AND NOT EXISTS(SELECT 1 FROM execution_event WHERE CASE WHEN json_valid(details)
                    THEN json_extract(details,'$.decision.business_facts_ref.input_id') END=execution_input.id)
                AND NOT EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id IN
                    (SELECT asset_id FROM business_asset_snapshot WHERE input_id=execution_input.id));"""
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}record_cleanup AFTER DELETE ON executerecord "
            f"BEGIN {deletes} {orphan_cleanup} END"
        )
    # Explicit child cleanup also works for installations that do not enable FKs.
    for table in tables.values():
        for parent in tables.values():
            if parent.name == table.name:
                continue
            keys = [
                key
                for key in table.foreign_keys
                if key.target_fullname.startswith(parent.name + ".") and key.ondelete == "CASCADE"
            ]
            if not keys:
                continue
            predicates = " AND ".join(
                f"{_quote(key.parent.name)} IS {_column('OLD', key.target_fullname.rsplit('.', 1)[1])}" for key in keys
            )
            name = _PREFIX + parent.name + "_cleanup_" + table.name
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(name)} AFTER DELETE ON {_quote(parent.name)} "
                f"BEGIN DELETE FROM {_quote(table.name)} WHERE {predicates}; END"
            )
    if "execution_input" in tables:
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS {_PREFIX}account_inputs_cleanup BEFORE DELETE ON account "
            "BEGIN DELETE FROM execution_artifact WHERE execution_id IN "
            "(SELECT execution_id FROM executerecord WHERE account_id=OLD.id "
            "UNION SELECT COALESCE(owner_execution_id,execution_id) FROM execution_input WHERE account_id=OLD.id "
            "UNION SELECT execution_id FROM execution_event WHERE account_id=OLD.id); "
            "DELETE FROM execution_event WHERE account_id=OLD.id; "
            "DELETE FROM account_asset_snapshot WHERE account_id=OLD.id; "
            "DELETE FROM executerecord WHERE account_id=OLD.id; "
            "DELETE FROM execution_input WHERE account_id=OLD.id; "
            "UPDATE target_weight_snapshot SET account_id=NULL WHERE account_id=OLD.id; END"
        )


def install_triggers(connection, tables: Sequence[sa.Table]) -> None:
    """Install canonical ownership/invalidation after the upgrade backfill completes.

    Parameters
    ----------
    tables
        Canonical domain tables including the terminal executerecord header.
        Analysis publication tables must never be included.
    """
    registry = {table.name: table for table in tables}
    for name in _OBSOLETE:
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    _install_invalidation(connection, registry)
    _install_input_guards(connection)
    _install_parent_guards(connection, registry)
    _install_summary_guards(connection, registry)
    _install_outbox_guards(connection, registry)
    _install_asset_guards(connection, registry)
    _install_cleanup(connection, registry)
