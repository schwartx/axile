//! 私有码表只作为数据传入；修补、摘要物化和字段恢复共用调用方事务。

use std::collections::BTreeSet;

use rusqlite::{params, Connection, Params, Row};
use serde_json::{json, Map, Value};

use crate::business_detail;
use crate::business_facts_ops::{result_extensions, result_head, result_symbols};
use crate::error::AnalysisError;

use super::legacy::{self, Change};
use crate::json_codec::parse as parse_legacy_json;

type Result<T> = std::result::Result<T, AnalysisError>;
type Object = Map<String, Value>;

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("MIGRATION_SQL", error.to_string())
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MIGRATION_ERROR", message)
}

fn conflict(table: &str, id: &str) -> AnalysisError {
    AnalysisError::new(
        "MIGRATION_CONFLICT",
        format!("migration restore conflict: {table}:{id}"),
    )
}

fn identifier(name: &str) -> Result<String> {
    if name.is_empty()
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        || name.as_bytes()[0].is_ascii_digit()
    {
        return Err(invalid("invalid migration backup identifier"));
    }
    Ok(format!("\"{name}\""))
}

fn typed(connection: &Connection) -> Result<bool> {
    let mut statement = connection
        .prepare("PRAGMA table_info(executerecord)")
        .map_err(sql)?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(sql)?;
    for column in columns {
        if column.map_err(sql)? == "raw_result" {
            return Ok(false);
        }
    }
    Ok(true)
}

fn rows(
    connection: &Connection,
    sql_text: &str,
    values: impl Params,
    table: &str,
) -> Result<Vec<Object>> {
    let mut statement = connection.prepare(sql_text).map_err(sql)?;
    let mut cursor = statement.query(values).map_err(sql)?;
    let mut result = Vec::new();
    while let Some(row) = cursor.next().map_err(sql)? {
        result.push(business_detail::decode(row, table).map_err(AnalysisError::from)?);
    }
    Ok(result)
}

fn id(row: &Object) -> Result<i64> {
    row.get("id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("invalid migration row id"))
}

fn text<'a>(row: &'a Object, key: &str) -> Option<&'a str> {
    row.get(key).and_then(Value::as_str)
}

fn detail(connection: &Connection, record: &Object) -> Result<Value> {
    let account_id = record
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("invalid migration account id"))?;
    let detail =
        business_detail::read(connection, account_id, id(record)?).map_err(AnalysisError::from)?;
    Ok(detail)
}

fn content(artifact: &Object) -> Value {
    match artifact.get("content") {
        Some(Value::String(value)) => parse_legacy_json(value).unwrap_or(Value::Null),
        Some(value) => value.clone(),
        None => Value::Null,
    }
}

fn evidence(connection: &Connection, execution_id: Option<&str>) -> Result<Vec<Value>> {
    let Some(execution_id) = execution_id else {
        return Ok(Vec::new());
    };
    let records = rows(
        connection,
        "SELECT * FROM executerecord WHERE execution_id=?1 ORDER BY id",
        [execution_id],
        "executerecord",
    )?;
    let mut evidence = Vec::new();
    for record in records {
        let projected = detail(connection, &record)?;
        evidence.push(projected["input"].clone());
        evidence.push(projected["result"].clone());
    }
    for artifact in rows(connection,
        "SELECT * FROM execution_artifact WHERE execution_id=?1 AND artifact_type IN ('STANDARD_INPUT','EXECUTION_SUMMARY') ORDER BY id",
        [execution_id], "execution_artifact")?
    {
        evidence.push(content(&artifact));
    }
    let mut statement = connection
        .prepare("SELECT DISTINCT channel FROM execution_event WHERE execution_id=?1")
        .map_err(sql)?;
    let channels = statement
        .query_map([execution_id], |row| row.get::<_, Option<String>>(0))
        .map_err(sql)?;
    for channel in channels {
        evidence.push(json!({"channel": channel.map_err(sql)?}));
    }
    Ok(evidence)
}

fn backup(connection: &Connection, name: &str) -> Result<()> {
    connection
        .execute_batch(&format!(
        "CREATE TABLE {} (id INTEGER NOT NULL PRIMARY KEY, table_name TEXT NOT NULL, row_id TEXT NOT NULL,
         json_column TEXT NOT NULL, path JSON NOT NULL, before_present BOOLEAN NOT NULL,
         before_value JSON, after_present BOOLEAN NOT NULL, after_value JSON,
         rule TEXT NOT NULL, identity JSON NOT NULL)", identifier(name)?
    ))
        .map_err(sql)
}

fn store_changes(
    connection: &Connection,
    backup: &str,
    table: &str,
    row: &Object,
    changes: &[Change],
) -> Result<()> {
    let identity: Object = ["execution_id", "created_at", "account_id", "artifact_type"]
        .into_iter()
        .filter_map(|key| row.get(key).map(|value| (key.to_owned(), value.clone())))
        .collect();
    let sql_text = format!("INSERT INTO {} (table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,identity) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)", identifier(backup)?);
    let mut statement = connection.prepare(&sql_text).map_err(sql)?;
    for change in changes {
        statement
            .execute(params![
                table,
                id(row)?.to_string(),
                if table == "executerecord" {
                    "raw_result"
                } else {
                    "content"
                },
                json!(change.path).to_string(),
                change.before_present,
                change.before.to_string(),
                change.after_present,
                change.after.to_string(),
                change.rule,
                Value::Object(identity.clone()).to_string(),
            ])
            .map_err(sql)?;
    }
    Ok(())
}

fn write_artifact(connection: &Connection, artifact: &Object, document: &Value) -> Result<()> {
    connection
        .execute(
            "UPDATE execution_artifact SET content=?1 WHERE id=?2",
            params![document.to_string(), id(artifact)?],
        )
        .map_err(sql)?;
    legacy::invalidate(connection, text(artifact, "execution_id"))
}

fn write_result(
    connection: &Connection,
    record: &Object,
    document: &Value,
    changes: &[Change],
) -> Result<()> {
    let record_id = id(record)?;
    let head = result_head::execution_head_values(
        document,
        record.get("is_success").and_then(Value::as_i64),
    )?;
    connection.execute(
        "UPDATE executerecord SET error=?1,display_error=?2,technical_detail=?3,audit_evidence=?4 WHERE id=?5",
        params![text(&head,"error"), text(&head,"display_error"), text(&head,"technical_detail"), head["audit_evidence"].to_string(), record_id],
    ).map_err(sql)?;
    let document = document
        .as_object()
        .ok_or_else(|| invalid("invalid migration result document"))?;
    let extension =
        result_extensions::encode_extensions(record_id, document).map_err(AnalysisError::from)?;
    let changed = connection
        .execute(
            "UPDATE execution_extension SET audit_evidence=?1,reconciliation=?2 WHERE record_id=?3",
            params![
                extension["audit_evidence"].to_string(),
                extension
                    .get("reconciliation")
                    .filter(|value| !value.is_null())
                    .map(Value::to_string),
                record_id
            ],
        )
        .map_err(sql)?;
    if changed != 1 {
        return Err(invalid(format!(
            "migration result extension missing: executerecord:{record_id}"
        )));
    }
    let changed_symbols: BTreeSet<&str> = changes
        .iter()
        .filter_map(|change| {
            (change.path.len() > 2 && change.path[0] == "symbol_results")
                .then(|| change.path[1].as_str())
                .flatten()
        })
        .collect();
    if changed_symbols.is_empty() {
        return Ok(());
    }
    let (_, symbols, _, _, _, _) =
        result_symbols::encode_symbols(record_id, &Value::Object(document.clone()))?;
    {
        for symbol in &symbols {
            if !text(symbol, "map_key").is_some_and(|key| changed_symbols.contains(key)) {
                continue;
            }
            let changed = connection.execute(
                "UPDATE execution_symbol_result SET error=?1,technical_detail=?2,audit_evidence=?3 WHERE record_id=?4 AND path=?5",
                params![text(symbol,"error"), text(symbol,"technical_detail"), symbol["audit_evidence"].to_string(), record_id, text(symbol,"path")],
            ).map_err(sql)?;
            if changed != 1 {
                return Err(invalid(format!(
                    "migration symbol result missing: executerecord:{record_id}:{}",
                    symbol["path"]
                )));
            }
        }
    }
    Ok(())
}

/// 复用冻结规则；类型化库只写错误字段、对应扩展及摘要正文。
pub(super) fn patch_errors(
    connection: &Connection,
    backup_name: &str,
    codes: &Value,
    dry_run: bool,
) -> Result<Value> {
    if !typed(connection)? {
        return legacy::patch_errors(connection, backup_name, codes, dry_run);
    }
    if !dry_run {
        backup(connection, backup_name)?;
    }
    let mut fields = 0usize;
    let mut scan = Map::new();
    for table in ["executerecord", "execution_artifact"] {
        let mut last_id = -1;
        loop {
            let predicate = if table == "execution_artifact" {
                " AND artifact_type='EXECUTION_SUMMARY'"
            } else {
                ""
            };
            let batch = rows(
                connection,
                &format!("SELECT * FROM {table} WHERE id>?1{predicate} ORDER BY id LIMIT 500"),
                [last_id],
                table,
            )?;
            if batch.is_empty() {
                break;
            }
            for row in &batch {
                let (own_input, original) = if table == "executerecord" {
                    let projected = detail(connection, row)?;
                    (projected["input"].clone(), projected["result"].clone())
                } else {
                    (Value::Null, content(row))
                };
                let mut channels = vec![own_input];
                channels.extend(evidence(connection, text(row, "execution_id"))?);
                let (patched, changes) = legacy::patch_document(&original, &channels, codes)?;
                for key in [
                    Some("scanned_rows"),
                    changes.is_empty().then_some(if original.is_object() {
                        "unchanged_rows"
                    } else {
                        "invalid_shape_rows"
                    }),
                ]
                .into_iter()
                .flatten()
                {
                    let count = scan.get(key).and_then(Value::as_u64).unwrap_or_default();
                    scan.insert(key.into(), json!(count + 1));
                }
                fields += changes.len();
                if dry_run || changes.is_empty() {
                    continue;
                }
                store_changes(connection, backup_name, table, row, &changes)?;
                if table == "executerecord" {
                    write_result(connection, row, &patched, &changes)?;
                } else {
                    write_artifact(connection, row, &patched)?;
                }
            }
            last_id = id(batch.last().expect("nonempty batch"))?;
        }
    }
    Ok(json!({"fields":fields,"scan":scan}))
}

struct Backup {
    table: String,
    row_id: String,
    column: String,
    identity: Value,
    change: Change,
}

impl Backup {
    fn read(row: &Row<'_>) -> Result<Self> {
        fn json_value(row: &Row<'_>, index: usize) -> Result<Value> {
            let text: Option<String> = row.get(index).map_err(sql)?;
            match text {
                None => Ok(Value::Null),
                Some(text) => parse_legacy_json(&text).map_err(invalid),
            }
        }
        let path = json_value(row, 3)?
            .as_array()
            .cloned()
            .ok_or_else(|| invalid("invalid migration backup path"))?;
        Ok(Self {
            table: row.get(0).map_err(sql)?,
            row_id: row.get(1).map_err(sql)?,
            column: row.get(2).map_err(sql)?,
            identity: json_value(row, 10)?,
            change: Change {
                path,
                before_present: row.get(4).map_err(sql)?,
                before: json_value(row, 5)?,
                after_present: row.get(6).map_err(sql)?,
                after: json_value(row, 7)?,
                rule: row.get(8).map_err(sql)?,
            },
        })
    }
    fn key(&self) -> (&str, &str, &str) {
        (&self.table, &self.row_id, &self.column)
    }
}

fn restore_group(connection: &Connection, group: &[Backup], dry_run: bool) -> Result<()> {
    let first = &group[0];
    if !matches!(
        (first.table.as_str(), first.column.as_str()),
        ("executerecord", "raw_result") | ("execution_artifact", "content")
    ) {
        return Err(invalid(format!(
            "unsupported migration backup target: {}:{}",
            first.table, first.column
        )));
    }
    let row_id = first
        .row_id
        .parse::<i64>()
        .map_err(|_| conflict(&first.table, &first.row_id))?;
    let loaded = business_detail::load(connection, &first.table, "id", &json!(row_id))
        .map_err(AnalysisError::from)?;
    let row = loaded
        .first()
        .ok_or_else(|| conflict(&first.table, &first.row_id))?;
    let identity = first
        .identity
        .as_object()
        .ok_or_else(|| conflict(&first.table, &first.row_id))?;
    if identity
        .iter()
        .any(|(key, value)| row.get(key) != Some(value))
    {
        return Err(conflict(&first.table, &first.row_id));
    }
    let mut document = if first.table == "executerecord" {
        detail(connection, row)?["result"].clone()
    } else {
        content(row)
    };
    if !document.is_object() {
        return Err(conflict(&first.table, &first.row_id));
    }
    for backup in group {
        if legacy::path_get(&document, &backup.change.path)
            != (backup.change.after_present, backup.change.after.clone())
        {
            return Err(AnalysisError::new(
                "MIGRATION_CONFLICT",
                format!(
                    "migration restore conflict: {}:{}:{}",
                    first.table,
                    first.row_id,
                    json!(backup.change.path)
                ),
            ));
        }
    }
    if dry_run {
        return Ok(());
    }
    for backup in group.iter().rev() {
        legacy::path_set(
            &mut document,
            &backup.change.path,
            backup.change.before_present,
            &backup.change.before,
        )?;
    }
    let changes: Vec<Change> = group.iter().map(|backup| backup.change.clone()).collect();
    if first.table == "executerecord" {
        write_result(connection, row, &document, &changes)
    } else {
        write_artifact(connection, row, &document)
    }
}

/// 恢复前核对原身份和全部修补后值，冲突时保留备份供重试。
pub(super) fn restore_errors(
    connection: &Connection,
    backup_name: &str,
    dry_run: bool,
) -> Result<()> {
    if !typed(connection)? {
        return legacy::restore_errors(connection, backup_name, dry_run);
    }
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
            [backup_name],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if !exists {
        return Err(invalid(format!("migration backup missing: {backup_name}")));
    }
    let mut statement = connection.prepare(&format!("SELECT table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,id,identity FROM {} ORDER BY table_name,row_id,json_column,id", identifier(backup_name)?)).map_err(sql)?;
    let mut cursor = statement.query([]).map_err(sql)?;
    let mut group: Vec<Backup> = Vec::new();
    while let Some(row) = cursor.next().map_err(sql)? {
        let backup = Backup::read(row)?;
        if group
            .first()
            .is_some_and(|first| first.key() != backup.key())
        {
            restore_group(connection, &group, dry_run)?;
            group.clear();
        }
        group.push(backup);
    }
    if !group.is_empty() {
        restore_group(connection, &group, dry_run)?;
    }
    drop(cursor);
    drop(statement);
    if !dry_run {
        connection
            .execute_batch(&format!("DROP TABLE {}", identifier(backup_name)?))
            .map_err(sql)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn historical_artifact_numbers_do_not_hide_error_patches_or_restore_conflicts() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY);
             CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,source_version INTEGER,requested INTEGER,failures INTEGER,retry_at REAL,error TEXT,current_snapshot TEXT,running_version INTEGER);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,execution_id TEXT,account_id INTEGER,created_at TEXT);
             CREATE TABLE execution_event(id INTEGER PRIMARY KEY,execution_id TEXT,channel TEXT,account_id INTEGER);
             CREATE TABLE execution_artifact(id INTEGER PRIMARY KEY,execution_id TEXT,artifact_type TEXT,schema_version INTEGER,content JSON,created_at TEXT);"
        ).unwrap();
        let source = r#"{"channel_type":"LOCAL","error":"DENIED","metrics":[NaN,Infinity,-Infinity,1e400],"literal":{"invalid_number":"nan"},"huge":123456789012345678901234567890,"text":"NaN Infinity"}"#;
        connection
            .execute(
                "INSERT INTO execution_artifact VALUES(1,'frozen','EXECUTION_SUMMARY',1,?1,'date')",
                [source],
            )
            .unwrap();
        let codes = json!({"LOCAL":{"DENIED":"保留业务说明"}});
        let original = parse_legacy_json(source).unwrap();
        assert_eq!(
            patch_errors(&connection, "private_error_backup", &codes, true).unwrap()["fields"],
            2
        );
        assert!(connection
            .prepare("SELECT * FROM private_error_backup")
            .is_err());
        assert_eq!(
            patch_errors(&connection, "private_error_backup", &codes, false).unwrap()["fields"],
            2
        );
        let read = || {
            let source: String = connection
                .query_row("SELECT content FROM execution_artifact", [], |row| {
                    row.get(0)
                })
                .unwrap();
            parse_legacy_json(&source).unwrap()
        };
        let patched = read();
        assert_eq!(patched["error"], "保留业务说明");
        assert_eq!(patched["technical_detail"], "DENIED");
        assert!(patched["metrics"][0].is_number());
        assert_eq!(patched["metrics"], original["metrics"]);
        assert_eq!(patched["literal"], json!({"invalid_number":"nan"}));
        assert_eq!(
            patched["huge"].to_string(),
            "123456789012345678901234567890"
        );
        assert_eq!(patched["text"], "NaN Infinity");
        restore_errors(&connection, "private_error_backup", true).unwrap();
        assert_eq!(read(), patched);

        let mut edited = patched.clone();
        edited["error"] = json!("operator edit");
        connection
            .execute(
                "UPDATE execution_artifact SET content=?1",
                [edited.to_string()],
            )
            .unwrap();
        let error = restore_errors(&connection, "private_error_backup", false).unwrap_err();
        assert_eq!(error.code, "MIGRATION_CONFLICT");
        assert_eq!(read(), edited);
        let count: i64 = connection
            .query_row("SELECT count(*) FROM private_error_backup", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 2);
        connection
            .execute(
                "UPDATE execution_artifact SET content=?1",
                [patched.to_string()],
            )
            .unwrap();
        restore_errors(&connection, "private_error_backup", false).unwrap();
        assert_eq!(read(), original);
        assert!(connection
            .prepare("SELECT * FROM private_error_backup")
            .is_err());
    }

    #[test]
    fn historical_backup_values_keep_numbers_distinct_from_literal_marker_objects() {
        let connection = Connection::open_in_memory().unwrap();
        backup(&connection, "private_error_backup").unwrap();
        let before = parse_legacy_json("NaN").unwrap();
        let marker = json!({"invalid_number":"nan"});
        let row = json!({"id":1,"execution_id":"frozen","created_at":"date"});
        store_changes(
            &connection,
            "private_error_backup",
            "execution_artifact",
            row.as_object().unwrap(),
            &[Change {
                path: vec![json!("error")],
                before_present: true,
                before: before.clone(),
                after_present: true,
                after: marker.clone(),
                rule: "LOCAL:example".into(),
            }],
        )
        .unwrap();
        let saved = connection.query_row("SELECT table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,id,identity FROM private_error_backup",[],|row|Ok(Backup::read(row))).unwrap().unwrap();
        assert!(saved.change.before.is_number());
        assert_eq!(saved.change.before.to_string(), "NaN");
        assert_eq!(saved.change.after, marker);
        let scalar = content(json!({"content":"NaN"}).as_object().unwrap());
        assert!(scalar.is_number());
        assert_ne!(scalar, marker);
        assert_eq!(
            content(json!({"content":"{bad"}).as_object().unwrap()),
            Value::Null
        );
    }

    #[test]
    fn backups_keep_logical_paths_presence_and_original_identity() {
        let connection = Connection::open_in_memory().unwrap();
        backup(&connection, "private_error_backup").unwrap();
        let row = json!({"id":7,"execution_id":"frozen","account_id":1,"created_at":"date"});
        let change = Change {
            path: vec![
                json!("reconciliation"),
                json!("symbols"),
                json!(0),
                json!("error"),
            ],
            before_present: true,
            before: Value::Null,
            after_present: true,
            after: json!("readable"),
            rule: "LOCAL:example".into(),
        };
        store_changes(
            &connection,
            "private_error_backup",
            "executerecord",
            row.as_object().unwrap(),
            &[change.clone()],
        )
        .unwrap();
        let saved = connection.query_row("SELECT table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,id,identity FROM private_error_backup", [], |row| {
            // 字段解码本身保留 JSON 类型，不用 SQLite 的宽松转换猜值。
            Ok(Backup::read(row))
        }).unwrap().unwrap();
        assert_eq!(saved.key(), ("executerecord", "7", "raw_result"));
        assert_eq!(saved.change.path, change.path);
        assert!(saved.change.before_present);
        assert_eq!(saved.change.before, Value::Null);
        assert_eq!(
            saved.identity,
            json!({"execution_id":"frozen","created_at":"date","account_id":1})
        );
    }

    #[test]
    fn typed_restore_rejects_changed_identity_before_reading_owned_facts() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE executerecord(id INTEGER PRIMARY KEY,execution_id TEXT,account_id INTEGER,created_at TEXT); INSERT INTO executerecord VALUES(1,'new-owner',1,'date');").unwrap();
        let saved = Backup {
            table: "executerecord".into(),
            row_id: "1".into(),
            column: "raw_result".into(),
            identity: json!({"execution_id":"original-owner","account_id":1,"created_at":"date"}),
            change: Change {
                path: vec![json!("error")],
                before_present: true,
                before: json!("code"),
                after_present: true,
                after: json!("readable"),
                rule: "LOCAL:example".into(),
            },
        };
        let error = restore_group(&connection, &[saved], true).unwrap_err();
        assert_eq!(error.code, "MIGRATION_CONFLICT");
        assert!(error.error.contains("executerecord:1"));
    }

    #[test]
    fn backup_identifiers_cannot_execute_sql_and_detection_does_not_write() {
        let connection = Connection::open_in_memory().unwrap();
        assert!(backup(&connection, "x;DROP TABLE account").is_err());
        connection
            .execute_batch("CREATE TABLE executerecord(id INTEGER PRIMARY KEY,raw_result JSON)")
            .unwrap();
        assert!(!typed(&connection).unwrap());
        connection
            .execute_batch("ALTER TABLE executerecord DROP COLUMN raw_result")
            .unwrap();
        assert!(typed(&connection).unwrap());
        assert!(restore_errors(&connection, "missing_backup", true)
            .unwrap_err()
            .error
            .contains("migration backup missing"));
        let tables: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tables, 1);
    }
}
