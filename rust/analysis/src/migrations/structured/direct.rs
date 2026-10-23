use super::*;
use schema::{DERIVED, SUMMARY_COLUMNS};

const OUTPUTS: &[&str] = &["performance_cost_range", "performance_execution_summary"];
fn exists(connection: &Connection, table: &str) -> Result<bool> {
    Ok(!rows(
        connection,
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        [table],
    )?
    .is_empty())
}
pub(super) fn memory_preflight(connection: &Connection) -> Result<()> {
    if !rows(connection,"SELECT 1 FROM performance_cost_execution_symbol WHERE memory_message IS NOT NULL OR memory_reason_code IS NOT NULL LIMIT 1",[])?.is_empty(){return Err(error("Cannot downgrade: frozen symbol memory evidence cannot be represented by schema 0038"));}
    Ok(())
}
fn merge_outputs(connection: &Connection) -> Result<()> {
    let existing = analysis::table_fields(connection, "performance_cost_execution", &[])?;
    for (name, kind) in SUMMARY_COLUMNS {
        if !existing.iter().any(|column| column == name) {
            execute(
                connection,
                &format!(
                    "ALTER TABLE performance_cost_execution ADD COLUMN {} {kind}",
                    quote(name)
                ),
            )?;
        }
    }
    if exists(connection, "performance_execution_summary")? {
        if !rows(connection,"SELECT 1 FROM performance_execution_summary s LEFT JOIN performance_cost_execution e ON e.snapshot_id=s.snapshot_id AND e.record_id=s.record_id WHERE e.record_id IS NULL OR e.time IS NOT s.time OR e.symbol_count IS NOT s.symbol_count LIMIT 1",[])?.is_empty(){return Err(error("Cannot merge inconsistent frozen execution summary"));}
        let names = SUMMARY_COLUMNS
            .iter()
            .map(|(name, _)| quote(name))
            .collect::<Vec<_>>()
            .join(",");
        execute(connection,&format!("UPDATE performance_cost_execution AS e SET ({names})=(SELECT {names} FROM performance_execution_summary s WHERE s.snapshot_id=e.snapshot_id AND s.record_id=e.record_id); DROP INDEX IF EXISTS ix_performance_execution_summary_time"))?;
    }
    execute(connection,"CREATE INDEX IF NOT EXISTS ix_performance_execution_summary_time ON performance_cost_execution(snapshot_id,time,record_id)")?;
    if exists(connection, "performance_cost_range")? {
        let mut query=connection.prepare("SELECT c.snapshot_id,c.range_key,c.baseline,c.end,r.snapshot_id,r.baseline,r.end FROM performance_cost_range c LEFT JOIN performance_result_range r ON r.snapshot_id=c.snapshot_id AND r.range_key=c.range_key").map_err(sql)?;
        let mut cursor = query.query([]).map_err(sql)?;
        while let Some(row) = cursor.next().map_err(sql)? {
            let snapshot = row.get::<_, String>(0).map_err(sql)?;
            let key = row.get::<_, String>(1).map_err(sql)?;
            let lower = row.get::<_, Option<f64>>(2).map_err(sql)?;
            let upper = row.get::<_, Option<f64>>(3).map_err(sql)?;
            let owner = row.get::<_, Option<String>>(4).map_err(sql)?;
            let text_lower = row.get::<_, Option<String>>(5).map_err(sql)?;
            let text_upper = row.get::<_, Option<String>>(6).map_err(sql)?;
            let expected_lower = text_lower
                .as_deref()
                .map(|source| millis(source, false))
                .transpose()?;
            let expected_upper = text_upper
                .as_deref()
                .map(|source| millis(source, false))
                .transpose()?;
            if owner.is_none() || lower != expected_lower || upper != expected_upper {
                return Err(error(format!(
                    "Cannot remove unmatched frozen cost range: {snapshot}/{key}"
                )));
            }
        }
    }
    Ok(())
}
pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    let names = DERIVED.iter().chain(OUTPUTS).copied().collect::<Vec<_>>();
    let delete = regex::RegexBuilder::new(&format!(
        r#"\bDELETE\s+FROM\s+"?(?:{})"?\s+[^;]*;"#,
        names.join("|")
    ))
    .case_insensitive(true)
    .build()
    .map_err(|e| error(e.to_string()))?;
    let empty = regex::RegexBuilder::new(r"\bBEGIN\s*END\s*$")
        .case_insensitive(true)
        .build()
        .map_err(|e| error(e.to_string()))?;
    let mut replacements = Vec::new();
    for row in rows(
        connection,
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name",
        [],
    )? {
        let name = text(&row, "name")?.to_owned();
        let owner = text(&row, "tbl_name")?;
        let statement = text(&row, "sql")?;
        if names.contains(&owner) {
            replacements.push((name, None));
            continue;
        }
        if !names.iter().any(|table| statement.contains(table)) {
            continue;
        }
        let replaced = delete.replace_all(statement, "").into_owned();
        if names.iter().any(|table| replaced.contains(table)) {
            return Err(error(format!("Unsupported analysis input trigger: {name}")));
        }
        replacements.push((
            name,
            if empty.is_match(&replaced) {
                None
            } else {
                Some(replaced)
            },
        ));
    }
    merge_outputs(connection)?;
    let existing = analysis::table_fields(connection, "performance_cost_execution_symbol", &[])?;
    for name in ["memory_message", "memory_reason_code"] {
        if !existing.iter().any(|column| column == name) {
            execute(
                connection,
                &format!(
                    "ALTER TABLE performance_cost_execution_symbol ADD COLUMN {} TEXT",
                    quote(name)
                ),
            )?;
        }
    }
    for (name, replacement) in replacements {
        execute(connection, &format!("DROP TRIGGER {}", quote(&name)))?;
        if let Some(statement) = replacement {
            execute(connection, &statement)?;
        }
    }
    for table in names {
        execute(
            connection,
            &format!("DROP TABLE IF EXISTS {}", quote(table)),
        )?;
    }
    Ok(())
}
fn restore_outputs(connection: &Connection) -> Result<()> {
    execute(
        connection,
        "DROP INDEX IF EXISTS ix_performance_execution_summary_time",
    )?;
    if !exists(connection, "performance_cost_range")? {
        schema::create_cost_range(connection)?;
    }
    if !exists(connection, "performance_execution_summary")? {
        schema::create_summary(connection)?;
    }
    let names = ["snapshot_id", "record_id", "time"]
        .into_iter()
        .chain(SUMMARY_COLUMNS.iter().map(|(name, _)| *name))
        .chain(["symbol_count"])
        .map(quote)
        .collect::<Vec<_>>()
        .join(",");
    execute(connection,&format!("INSERT INTO performance_execution_summary({names}) SELECT {names} FROM performance_cost_execution WHERE amount_complete IS NOT NULL"))?;
    let mut query = connection
        .prepare("SELECT snapshot_id,range_key,baseline,end FROM performance_result_range")
        .map_err(sql)?;
    let mut cursor = query.query([]).map_err(sql)?;
    while let Some(row) = cursor.next().map_err(sql)? {
        let snapshot = row.get::<_, String>(0).map_err(sql)?;
        let key = row.get::<_, String>(1).map_err(sql)?;
        let lower = row.get::<_, Option<String>>(2).map_err(sql)?;
        let upper = row.get::<_, Option<String>>(3).map_err(sql)?;
        connection
            .execute(
                "INSERT INTO performance_cost_range VALUES(?,?,?,?)",
                rusqlite::params![
                    snapshot,
                    key,
                    lower
                        .as_deref()
                        .map(|value| millis(value, false))
                        .transpose()?,
                    upper
                        .as_deref()
                        .map(|value| millis(value, false))
                        .transpose()?
                ],
            )
            .map_err(sql)?;
    }
    drop(cursor);
    drop(query);
    for (name, _) in SUMMARY_COLUMNS {
        execute(
            connection,
            &format!(
                "ALTER TABLE performance_cost_execution DROP COLUMN {}",
                quote(name)
            ),
        )?;
    }
    execute(
        connection,
        "DROP TRIGGER IF EXISTS analysis_read_views_snapshot_delete",
    )?;
    guards::install36(connection)?;
    execute(
        connection,
        "DROP TRIGGER IF EXISTS analysis_facts_snapshot_delete",
    )
}
pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    memory_preflight(connection)?;
    restore_outputs(connection)?;
    for name in ["memory_message", "memory_reason_code"] {
        execute(
            connection,
            &format!(
                "ALTER TABLE performance_cost_execution_symbol DROP COLUMN {}",
                quote(name)
            ),
        )?;
    }
    schema::create_replay(connection, true)?;
    schema::create_sources(connection, true)?;
    guards::install34(connection)?;
    guards::install35(connection)?;
    for table in schema::BUSINESS_TABLES {
        for action in ["insert", "update", "delete"] {
            execute(
                connection,
                &format!(
                    "DROP TRIGGER IF EXISTS {}",
                    quote(&format!("business_facts_0037_{table}_{action}"))
                ),
            )?;
        }
    }
    guards::install_business(connection, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cleanup_recognizes_only_delete_statements() {
        let regex =
            regex::RegexBuilder::new(r#"\bDELETE\s+FROM\s+"?(?:analysis_source_trade)"?\s+[^;]*;"#)
                .case_insensitive(true)
                .build()
                .unwrap();
        let result=regex.replace_all("BEGIN DELETE FROM analysis_source_trade WHERE record_id=OLD.id; UPDATE account SET id=id; END","");
        assert_eq!(result, "BEGIN  UPDATE account SET id=id; END");
    }

    #[test]
    fn downgrade_rejects_memory_before_any_schema_write() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE performance_cost_execution_symbol(memory_message TEXT,memory_reason_code TEXT); INSERT INTO performance_cost_execution_symbol VALUES('frozen',NULL)").unwrap();
        let before = connection
            .query_row("PRAGMA schema_version", [], |row| row.get::<_, i64>(0))
            .unwrap();
        assert!(downgrade(&connection)
            .unwrap_err()
            .error
            .contains("frozen symbol memory evidence"));
        assert_eq!(
            connection
                .query_row("PRAGMA schema_version", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            before
        );
        assert_eq!(
            analysis::table_fields(&connection, "performance_cost_execution_symbol", &[]).unwrap(),
            vec!["memory_message", "memory_reason_code"]
        );
    }
}
