//! Convert declared scalar columns only; opaque SQLite values are copied unchanged.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

use regex::Regex;
use rusqlite::{
    hooks::{AuthAction, AuthContext, Authorization},
    types::Value as SqlValue,
    ErrorCode,
};

use super::*;

struct Table {
    name: &'static str,
    columns: Vec<crate::time_us::Column>,
}

fn scalar(value: &SqlValue) -> Result<Value> {
    match value {
        SqlValue::Null => Ok(Value::Null),
        SqlValue::Integer(value) => Ok(Value::from(*value)),
        SqlValue::Real(value) if value.is_finite() => Ok(Value::from(*value)),
        SqlValue::Text(value) => Ok(Value::from(value.clone())),
        _ => Err(error("Invalid SQLite scalar time")),
    }
}

fn plan(connection: &Connection, upgrade: bool) -> Result<Vec<Table>> {
    let mut tables = Vec::new();
    for name in crate::time_us::TABLES {
        let present = rows(
            connection,
            &format!("PRAGMA table_info({})", quote(name)),
            [],
        )?;
        let columns = crate::time_us::columns(name)
            .iter()
            .filter(|column| {
                let source = if upgrade { column.name } else { column.storage };
                present
                    .iter()
                    .any(|row| value(row, "name") == &Value::from(source))
            })
            .copied()
            .collect::<Vec<_>>();
        if !columns.is_empty() {
            tables.push(Table { name, columns });
        }
    }
    Ok(tables)
}

fn type_check(name: &str) -> String {
    format!(
        " CHECK ({} IS NULL OR typeof({})='integer')",
        quote(name),
        quote(name)
    )
}

/// Prepare only: SQLite resolves quoted names and expressions without running SQL.
fn check_statement(
    connection: &Connection,
    statement: &str,
    object: &str,
    columns: &[(String, String)],
    trigger: Option<&str>,
) -> Result<bool> {
    let columns = columns.to_owned();
    let trigger = trigger.map(str::to_owned);
    let is_trigger = trigger.is_some();
    let seen = Arc::new(AtomicBool::new(false));
    let accessed = Arc::clone(&seen);
    let mut autoindex = false;
    connection.authorizer(Some(move |context: AuthContext<'_>| {
        if trigger
            .as_ref()
            .is_some_and(|name| context.accessor != Some(name.as_str()))
        {
            return Authorization::Allow;
        }
        accessed.store(true, Ordering::Relaxed);
        // PK/UNIQUE key reads precede the schema write; CHECK expression reads follow it.
        let structural_read = autoindex && matches!(context.action, AuthAction::Read { .. });
        autoindex = match context.action {
            AuthAction::CreateIndex { index_name, .. } => {
                index_name.starts_with("sqlite_autoindex_")
            }
            AuthAction::Read { .. } => autoindex,
            _ => false,
        };
        if structural_read {
            return Authorization::Allow;
        }
        let affected = match context.action {
            AuthAction::Read {
                table_name,
                column_name,
            }
            | AuthAction::Update {
                table_name,
                column_name,
            } => columns.iter().any(|(table, column)| {
                table.eq_ignore_ascii_case(table_name) && column.eq_ignore_ascii_case(column_name)
            }),
            // INSERT has no column-level callback, so custom trigger writes fail closed.
            AuthAction::Insert { table_name } if is_trigger => columns
                .iter()
                .any(|(table, _)| table.eq_ignore_ascii_case(table_name)),
            _ => false,
        };
        if affected {
            Authorization::Deny
        } else {
            Authorization::Allow
        }
    }));
    let prepared = connection.prepare(statement).map(drop);
    connection.authorizer(None::<fn(AuthContext<'_>) -> Authorization>);
    match prepared {
        Err(failure)
            if failure.sqlite_error_code() == Some(ErrorCode::AuthorizationForStatementDenied) =>
        {
            Err(error(format!("Cannot convert scalar time: custom {object} depends on a time column; preserve or update it before migrating")))
        }
        // A view may support only one of INSERT/UPDATE/DELETE through INSTEAD OF.
        Err(_) if is_trigger && !seen.load(Ordering::Relaxed) => Ok(false),
        Err(failure) => Err(sql(failure)),
        Ok(()) => Ok(seen.load(Ordering::Relaxed)),
    }
}

fn check_index(
    connection: &Connection,
    declarations: &Connection,
    object: &Object,
    tables: &[Table],
    columns: &[(String, String)],
) -> Result<()> {
    let name = text(object, "name")?;
    let owner = text(object, "tbl_name")?;
    if !tables.iter().any(|table| table.name == owner)
        || matches!(
            name,
            "ix_executerecord_account_time_id" | "ix_schedule_skip_account_time_id"
        )
    {
        return Ok(());
    }
    let expression = rows(
        connection,
        &format!("PRAGMA index_xinfo({})", quote(name)),
        [],
    )?
    .iter()
    .any(|row| value(row, "cid") == &Value::from(-2));
    let partial = rows(
        connection,
        &format!("PRAGMA index_list({})", quote(owner)),
        [],
    )?
    .iter()
    .any(|row| {
        value(row, "name") == &Value::from(name) && value(row, "partial") == &Value::from(1)
    });
    if expression || partial {
        check_statement(
            declarations,
            &format!("EXPLAIN {}", text(object, "sql")?),
            &format!("index {name}"),
            columns,
            None,
        )?;
    }
    Ok(())
}

fn check_trigger(
    connection: &Connection,
    object: &Object,
    columns: &[(String, String)],
) -> Result<()> {
    let name = text(object, "name")?;
    let owner = text(object, "tbl_name")?;
    if schema::BUSINESS_TABLES.iter().any(|table| {
        owner == *table
            && ["insert", "update", "delete"]
                .iter()
                .any(|action| name == format!("business_facts_0034_{table}_{action}"))
    }) {
        return Ok(());
    }
    let assignments = rows(
        connection,
        &format!("PRAGMA table_info({})", quote(owner)),
        [],
    )?
    .iter()
    .map(|column| Ok(format!("{}=NULL", quote(text(column, "name")?))))
    .collect::<Result<Vec<_>>>()?
    .join(",");
    let mut seen = false;
    for statement in [
        format!("EXPLAIN INSERT INTO {} DEFAULT VALUES", quote(owner)),
        format!("EXPLAIN UPDATE {} SET {assignments}", quote(owner)),
        format!("EXPLAIN DELETE FROM {}", quote(owner)),
    ] {
        seen |= check_statement(
            connection,
            &statement,
            &format!("trigger {name}"),
            columns,
            Some(name),
        )?;
    }
    if !seen {
        return Err(error(format!(
            "Cannot validate custom trigger {name} before converting scalar time"
        )));
    }
    Ok(())
}

fn check_dependencies(connection: &Connection, tables: &[Table], upgrade: bool) -> Result<()> {
    let columns = tables
        .iter()
        .flat_map(|table| {
            table.columns.iter().map(move |column| {
                (
                    table.name.to_owned(),
                    if upgrade { column.name } else { column.storage }.to_owned(),
                )
            })
        })
        .collect::<Vec<_>>();
    let declarations = Connection::open_in_memory().map_err(sql)?;
    crate::calendar::register_time_function(&declarations).map_err(sql)?;
    for table in tables {
        let mut definition: String = connection
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?1",
                [table.name],
                |row| row.get(0),
            )
            .map_err(sql)?;
        if !upgrade {
            for column in &table.columns {
                definition = definition.replace(&type_check(column.storage), "");
            }
        }
        check_statement(
            &declarations,
            &format!("EXPLAIN {definition}"),
            &format!("table {}", table.name),
            &columns,
            None,
        )?;
        execute(&declarations, &definition)?;
    }
    for object in rows(connection, "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type IN ('trigger','view','index') AND sql IS NOT NULL", [])? {
        let name = text(&object, "name")?;
        match text(&object, "type")? {
            "view" => {
                check_statement(connection, &format!("EXPLAIN SELECT * FROM {}", quote(name)), &format!("view {name}"), &columns, None)?;
            }
            "index" => check_index(connection, &declarations, &object, tables, &columns)?,
            "trigger" => check_trigger(connection, &object, &columns)?,
            _ => unreachable!(),
        }
    }
    Ok(())
}

fn legacy_type(table: &str, column: &crate::time_us::Column) -> &'static str {
    match column.unit {
        crate::time_us::Unit::Iso
            if matches!(
                table,
                "notification_event"
                    | "supplement_group"
                    | "supplement_step"
                    | "system_notification_state"
            ) =>
        {
            "VARCHAR"
        }
        crate::time_us::Unit::Iso => "TEXT",
        crate::time_us::Unit::Millis => "INTEGER",
        crate::time_us::Unit::Seconds | crate::time_us::Unit::DurationSeconds => "FLOAT",
    }
}

fn table_sql(source: &str, table: &Table, temporary: &str, upgrade: bool) -> Result<String> {
    let declaration = Regex::new(r#"(?i)^CREATE\s+TABLE\s+(?:"[^"]+"|\[[^]]+\]|`[^`]+`|\w+)"#)
        .map_err(|failure| error(failure.to_string()))?;
    let mut source = declaration
        .replace(source, format!("CREATE TABLE {}", quote(temporary)))
        .into_owned();
    for column in &table.columns {
        let name = if upgrade { column.storage } else { column.name };
        if !upgrade {
            source = source.replace(&type_check(name), "");
        }
        let pattern = format!(
            r#"(?i)([,(]\s*"?{}"?\s+)(?:TEXT|VARCHAR(?:\(\d+\))?|FLOAT|REAL|INTEGER|BIGINT|DATETIME|TIMESTAMP)\b"#,
            regex::escape(name)
        );
        let pattern = Regex::new(&pattern).map_err(|failure| error(failure.to_string()))?;
        if !pattern.is_match(&source) {
            return Err(error(format!(
                "Missing scalar time declaration: {}.{name}",
                table.name
            )));
        }
        let kind = if upgrade {
            "INTEGER"
        } else {
            legacy_type(table.name, column)
        };
        let check = if upgrade {
            type_check(name)
        } else {
            String::new()
        };
        source = pattern
            .replace(&source, format!("${{1}}{kind}{check}"))
            .into_owned();
    }
    Ok(source)
}

fn copy_rows(connection: &Connection, table: &Table, temporary: &str, upgrade: bool) -> Result<()> {
    let mut select = connection
        .prepare(&format!("SELECT * FROM {}", quote(table.name)))
        .map_err(sql)?;
    let names = select
        .column_names()
        .iter()
        .map(|name| (*name).to_owned())
        .collect::<Vec<_>>();
    let mut insert = connection
        .prepare(&format!(
            "INSERT INTO {} ({}) VALUES ({})",
            quote(temporary),
            names
                .iter()
                .map(|name| quote(name))
                .collect::<Vec<_>>()
                .join(","),
            vec!["?"; names.len()].join(",")
        ))
        .map_err(sql)?;
    let mut source = select.query([]).map_err(sql)?;
    let mut ordinal = 0usize;
    while let Some(row) = source.next().map_err(sql)? {
        ordinal += 1;
        let mut values = (0..names.len())
            .map(|index| row.get::<_, SqlValue>(index).map_err(sql))
            .collect::<Result<Vec<_>>>()?;
        let mut times = Object::new();
        for column in &table.columns {
            let index = names
                .iter()
                .position(|name| name == if upgrade { column.storage } else { column.name })
                .unwrap();
            times.insert(
                if upgrade { column.name } else { column.storage }.into(),
                scalar(&values[index])?,
            );
        }
        let converted = if upgrade {
            crate::time_us::encode_columns(table.name, &mut times)
        } else {
            crate::time_us::decode_columns(table.name, &mut times)
        };
        converted.map_err(|reason| {
            error(format!(
                "Cannot convert {} row {ordinal}: {reason}",
                table.name
            ))
        })?;
        for column in &table.columns {
            let target = if upgrade { column.storage } else { column.name };
            let index = names.iter().position(|name| name == target).unwrap();
            values[index] = db_value(&times[target])?;
        }
        insert.execute(params_from_iter(values)).map_err(sql)?;
    }
    Ok(())
}

fn convert(connection: &Connection, upgrade: bool) -> Result<()> {
    let tables = plan(connection, upgrade)?;
    if tables.is_empty() {
        return Ok(());
    }
    check_dependencies(connection, &tables, upgrade)?;
    // SQLite rewrites dependent indexes and triggers without touching JSON values.
    for table in &tables {
        for column in &table.columns {
            let (from, to) = if upgrade {
                (column.name, column.storage)
            } else {
                (column.storage, column.name)
            };
            execute(
                connection,
                &format!(
                    "ALTER TABLE {} RENAME COLUMN {} TO {}",
                    quote(table.name),
                    quote(from),
                    quote(to)
                ),
            )?;
        }
    }
    let triggers = rows(
        connection,
        "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND sql IS NOT NULL",
        [],
    )?;
    for trigger in &triggers {
        execute(
            connection,
            &format!("DROP TRIGGER {}", quote(text(trigger, "name")?)),
        )?;
    }
    let sequences =
        rows(connection, "SELECT name,seq FROM sqlite_sequence", []).unwrap_or_default();
    // Views may temporarily reference a table between DROP and RENAME.
    let old_legacy: bool = connection
        .query_row("PRAGMA legacy_alter_table", [], |row| row.get(0))
        .map_err(sql)?;
    execute(connection, "PRAGMA legacy_alter_table=ON")?;
    for table in &tables {
        let source: String = connection
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?1",
                [table.name],
                |row| row.get(0),
            )
            .map_err(sql)?;
        let indexes = rows(connection, "SELECT name,sql FROM sqlite_schema WHERE type='index' AND tbl_name=?1 AND sql IS NOT NULL", [table.name])?;
        let temporary = format!("_axile_scalar_time_{}", table.name);
        execute(connection, &table_sql(&source, table, &temporary, upgrade)?)?;
        copy_rows(connection, table, &temporary, upgrade)?;
        execute(
            connection,
            &format!(
                "DROP TABLE {}; ALTER TABLE {} RENAME TO {}",
                quote(table.name),
                quote(&temporary),
                quote(table.name)
            ),
        )?;
        for index in indexes {
            let mut definition = text(&index, "sql")?.to_owned();
            if upgrade
                && matches!(
                    text(&index, "name")?,
                    "ix_executerecord_account_time_id" | "ix_schedule_skip_account_time_id"
                )
            {
                for column in &table.columns {
                    let expression = Regex::new(&format!(
                        r#"axile_time\(\s*"?{}"?\s*\)"#,
                        regex::escape(column.storage)
                    ))
                    .map_err(|failure| error(failure.to_string()))?;
                    definition = expression
                        .replace_all(&definition, column.storage)
                        .into_owned();
                }
            }
            execute(connection, &definition)?;
        }
    }
    execute(
        connection,
        if old_legacy {
            "PRAGMA legacy_alter_table=ON"
        } else {
            "PRAGMA legacy_alter_table=OFF"
        },
    )?;
    for sequence in sequences {
        let name = text(&sequence, "name")?;
        if tables.iter().any(|table| table.name == name) {
            connection
                .execute(
                    "UPDATE sqlite_sequence SET seq=?2 WHERE name=?1",
                    (name, integer(&sequence, "seq")?),
                )
                .map_err(sql)?;
        }
    }
    for trigger in triggers {
        execute(connection, text(&trigger, "sql")?)?;
    }
    Ok(())
}

pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    convert(connection, true)
}
pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    convert(connection, false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scalar_rebuild_preserves_opaque_json_blobs_sequences_and_dependencies() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,opaque JSON);
             CREATE INDEX account_time ON account(created_at);
             CREATE TABLE audit(account_id INTEGER,observed TEXT);
             CREATE TRIGGER account_audit AFTER UPDATE ON account BEGIN INSERT INTO audit VALUES(NEW.id,NEW.id); END;
             CREATE VIEW account_view AS SELECT id /* created_at is not selected */ FROM account;
             INSERT INTO account VALUES(1,'1970-01-01T00:00:00.000001Z','1970-01-01T08:00:00+08:00',X'7B2022637265617465645F617422203A20226E6F742D612D74696D6522207D');
             UPDATE sqlite_sequence SET seq=99 WHERE name='account';"
        ).unwrap();
        let original: Vec<u8> = connection
            .query_row("SELECT opaque FROM account", [], |row| row.get(0))
            .unwrap();
        upgrade(&connection).unwrap();
        let row: (i64, i64, Vec<u8>) = connection
            .query_row(
                "SELECT created_at_us,updated_at_us,opaque FROM account",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row, (1, 0, original.clone()));
        assert!(connection
            .execute("UPDATE account SET created_at_us='bad-time'", [])
            .is_err());
        connection
            .execute("UPDATE account SET created_at_us=2", [])
            .unwrap();
        assert_eq!(
            connection
                .query_row("SELECT observed FROM audit", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "1"
        );
        assert_eq!(
            connection
                .query_row("SELECT id FROM account_view", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT seq FROM sqlite_sequence WHERE name='account'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            99
        );
        downgrade(&connection).unwrap();
        assert_eq!(
            connection
                .query_row("SELECT opaque FROM account", [], |row| row
                    .get::<_, Vec<u8>>(0))
                .unwrap(),
            original
        );
        assert_eq!(
            connection
                .query_row("SELECT created_at FROM account", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "1970-01-01T00:00:00.000002Z"
        );
    }

    #[test]
    fn malformed_required_time_rolls_back_the_entire_schema_change() {
        let mut connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE account(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,updated_at TEXT NOT NULL); INSERT INTO account VALUES(7,'bad-time','1970-01-01T00:00:00Z');").unwrap();
        let transaction = connection.transaction().unwrap();
        let failure = upgrade(&transaction).unwrap_err();
        assert!(
            failure.error.contains("Cannot convert account row 1"),
            "{failure:?}"
        );
        transaction.rollback().unwrap();
        assert_eq!(
            connection
                .query_row("SELECT created_at FROM account", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "bad-time"
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM sqlite_schema WHERE name LIKE '_axile_scalar_time_%'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn custom_time_expressions_are_rejected_without_touching_their_schema() {
        for definition in [
            "CREATE TRIGGER business_facts_0034_time_dependency AFTER UPDATE ON account WHEN NEW.created_at>'2025-01-01' BEGIN SELECT 1; END",
            "CREATE TRIGGER analysis_time_dependency AFTER UPDATE ON account WHEN NEW.created_at>'2025-01-01' BEGIN SELECT 1; END",
            "CREATE TRIGGER time_dependency AFTER UPDATE ON account WHEN NEW.created_at>'2025-01-01' BEGIN SELECT 1; END",
            "CREATE VIEW time_dependency AS SELECT created_at FROM account",
            "CREATE INDEX time_dependency ON account(julianday(created_at))",
            "CREATE INDEX time_dependency ON account(id) WHERE created_at>'2025-01-01'",
            "CREATE VIEW time_dependency AS SELECT * FROM account",
            "CREATE VIEW time_dependency AS SELECT /* readable comment */ * FROM account",
            "CREATE VIEW time_dependency AS SELECT -- readable comment\n * FROM account",
            "CREATE VIEW time_dependency AS SELECT DISTINCT \"a/*\"\"--\".* FROM account AS \"a/*\"\"--\"",
            "CREATE TRIGGER time_dependency AFTER UPDATE ON account BEGIN SELECT * FROM account; END",
        ] {
            let mut connection=Connection::open_in_memory().unwrap();
            connection.execute_batch("CREATE TABLE account(id INTEGER PRIMARY KEY,created_at TEXT,updated_at TEXT); INSERT INTO account VALUES(1,'1970-01-01T00:00:00Z','1970-01-01T00:00:00Z');").unwrap();
            connection.execute_batch(definition).unwrap();
            let transaction=connection.transaction().unwrap();
            assert!(upgrade(&transaction).unwrap_err().error.contains("time_dependency"));
            transaction.rollback().unwrap();
            assert_eq!(connection.query_row("SELECT created_at FROM account",[],|row|row.get::<_,String>(0)).unwrap(),"1970-01-01T00:00:00Z");
            assert_eq!(connection.query_row("SELECT sql FROM sqlite_schema WHERE name IN ('time_dependency','analysis_time_dependency','business_facts_0034_time_dependency')",[],|row|row.get::<_,String>(0)).unwrap(),definition);
        }
    }
}
