//! Convert declared scalar columns only; opaque SQLite values are copied unchanged.

use regex::Regex;
use rusqlite::types::Value as SqlValue;

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

fn check_dependencies(connection: &Connection, tables: &[Table], upgrade: bool) -> Result<()> {
    let literals = Regex::new(r"'(?:''|[^'])*'").map_err(|failure| error(failure.to_string()))?;
    let identifiers = Regex::new(r#""[^"]+"|\[[^]]+\]|`[^`]+`|\b\w+\b"#)
        .map_err(|failure| error(failure.to_string()))?;
    let wildcard =
        Regex::new(r#"(?i)(?:\bSELECT\s+(?:(?:DISTINCT|ALL)\s+)?|,)\s*(?:"?\w+"?\.)?\*"#)
            .map_err(|failure| error(failure.to_string()))?;
    for object in rows(connection,"SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type IN ('trigger','view','index') AND sql IS NOT NULL",[])? {
        let name=text(&object,"name")?;
        let kind=text(&object,"type")?;
        if kind=="trigger" && (name.starts_with("business_facts_0034_") || name.starts_with("analysis_") || matches!(name,"execution_result_staged"|"execution_summary_removed"|"account_settings_delete")) {
            continue;
        }
        if kind=="index" {
            if matches!(name,"ix_executerecord_account_time_id"|"ix_schedule_skip_account_time_id") { continue; }
            let expression=rows(connection,&format!("PRAGMA index_xinfo({})",quote(name)),[])?.iter().any(|row|value(row,"cid")==&Value::from(-2));
            let partial=rows(connection,&format!("PRAGMA index_list({})",quote(text(&object,"tbl_name")?)),[])?.iter().any(|row|value(row,"name")==&Value::from(name) && value(row,"partial")==&Value::from(1));
            if !expression && !partial { continue; }
        }
        let without_literals=literals.replace_all(text(&object,"sql")?,"");
        let tokens=identifiers.find_iter(&without_literals).map(|token|token.as_str().trim_matches(['"','`','[',']']).to_ascii_lowercase()).collect::<std::collections::BTreeSet<_>>();
        let affected=tables.iter().any(|table|tokens.contains(table.name) && (kind=="view" && wildcard.is_match(&without_literals) || table.columns.iter().any(|column|tokens.contains(if upgrade {column.name}else{column.storage}))));
        if affected { return Err(error(format!("Cannot convert scalar time: custom {kind} {name} depends on a time column; preserve or update it before migrating"))); }
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
            source = source.replace(
                &format!(
                    " CHECK ({} IS NULL OR typeof({})='integer')",
                    quote(name),
                    quote(name)
                ),
                "",
            );
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
            format!(
                " CHECK ({} IS NULL OR typeof({})='integer')",
                quote(name),
                quote(name)
            )
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
             CREATE VIEW account_view AS SELECT id FROM account;
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
            "CREATE TRIGGER time_dependency AFTER UPDATE ON account WHEN NEW.created_at>'2025-01-01' BEGIN SELECT 1; END",
            "CREATE VIEW time_dependency AS SELECT created_at FROM account",
            "CREATE INDEX time_dependency ON account(julianday(created_at))",
            "CREATE INDEX time_dependency ON account(id) WHERE created_at>'2025-01-01'",
            "CREATE VIEW time_dependency AS SELECT * FROM account",
        ] {
            let mut connection=Connection::open_in_memory().unwrap();
            connection.execute_batch("CREATE TABLE account(id INTEGER PRIMARY KEY,created_at TEXT,updated_at TEXT); INSERT INTO account VALUES(1,'1970-01-01T00:00:00Z','1970-01-01T00:00:00Z');").unwrap();
            connection.execute_batch(definition).unwrap();
            let transaction=connection.transaction().unwrap();
            assert!(upgrade(&transaction).unwrap_err().error.contains("time_dependency"));
            transaction.rollback().unwrap();
            assert_eq!(connection.query_row("SELECT created_at FROM account",[],|row|row.get::<_,String>(0)).unwrap(),"1970-01-01T00:00:00Z");
            assert_eq!(connection.query_row("SELECT sql FROM sqlite_schema WHERE name='time_dependency'",[],|row|row.get::<_,String>(0)).unwrap(),definition);
        }
    }
}
