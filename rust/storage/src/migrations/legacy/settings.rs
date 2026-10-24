use rusqlite::{params, params_from_iter};
use serde_json::{json, Map};

use super::*;

#[path = "notification_code.rs"]
mod notification_code;

pub(super) const GROUPS: &[(&str, &[&str])] = &[
    (
        "execution",
        &[
            "account_control_preset",
            "account_control_override",
            "weight_precision",
            "long_leverage",
            "short_leverage",
            "algorithm",
            "empty_positions_algorithm",
            "trade_rules",
            "forbidden_symbols",
            "risk_symbols",
            "write_empty_record",
            "execution_timeout",
        ],
    ),
    ("schedule", &["is_started", "cron_expr"]),
    (
        "notification",
        &["feishu_key", "execution_notification_code"],
    ),
    ("backtest", &["backtest_weight_type", "backtest_fee_rate"]),
];
const JSON_FIELDS: &[&str] = &[
    "account_config",
    "account_control_override",
    "algorithm",
    "empty_positions_algorithm",
    "trade_rules",
    "forbidden_symbols",
    "risk_symbols",
];
const FLOAT_FIELDS: &[&str] = &[
    "weight_precision",
    "long_leverage",
    "short_leverage",
    "backtest_fee_rate",
];
const REQUIRED: &[&str] = &[
    "account_config",
    "account_control_preset",
    "weight_precision",
    "is_started",
    "cron_expr",
    "execution_timeout",
    "backtest_weight_type",
    "backtest_fee_rate",
];

pub(super) fn notification_upgrade(connection: &Connection) -> Result<()> {
    connection.execute("UPDATE account SET execution_notification_code=?1 WHERE execution_notification_mode='default' AND feishu_key IS NOT NULL AND feishu_key!=''",[notification_code::DEFAULT]).map_err(db)?;
    sql(connection,"UPDATE account SET execution_notification_code=NULL WHERE execution_notification_mode='default' AND (feishu_key IS NULL OR feishu_key=''); ALTER TABLE account DROP COLUMN execution_notification_mode")
}

pub(super) fn notification_downgrade(connection: &Connection) -> Result<()> {
    sql(connection,"ALTER TABLE account ADD COLUMN execution_notification_mode TEXT DEFAULT 'default' NOT NULL")?;
    connection.execute("UPDATE account SET execution_notification_mode='function' WHERE execution_notification_code IS NOT NULL AND execution_notification_code!=?1",[notification_code::DEFAULT]).map_err(db)?;
    connection.execute("UPDATE account SET execution_notification_code=NULL WHERE execution_notification_code=?1",[notification_code::DEFAULT]).map_err(db)?;
    Ok(())
}

pub(super) fn notification_type_upgrade(connection: &Connection) -> Result<()> {
    connection.execute("UPDATE account SET execution_notification_code=?1 WHERE execution_notification_code=?2",params![notification_code::DEFAULT,notification_code::OLD_TYPE]).map_err(db)?;
    Ok(())
}

pub(super) fn supplement_notification(connection: &Connection, upgrade: bool) -> Result<()> {
    let (old, new) = if upgrade {
        (
            notification_code::OLD_SUPPLEMENT,
            notification_code::DEFAULT,
        )
    } else {
        (
            notification_code::DEFAULT,
            notification_code::OLD_SUPPLEMENT,
        )
    };
    let rows = query_rows(
        connection,
        "SELECT account_id,notification FROM account_settings",
        [],
        |row| Ok((row.get::<_, i64>(0)?, value(row, "notification")?)),
    )?;
    for (id, raw) in rows {
        let mut value = decode(&raw)?;
        let object = value
            .as_object_mut()
            .ok_or_else(|| error("invalid notification settings"))?;
        if object.get("execution_notification_code") == Some(&json!(old)) {
            object.insert("execution_notification_code".into(), json!(new));
            connection
                .execute(
                    "UPDATE account_settings SET notification=?1 WHERE account_id=?2",
                    params![encoded(&value)?, id],
                )
                .map_err(db)?;
        }
    }
    Ok(())
}

pub(super) fn settings_trigger(connection: &Connection) -> Result<()> {
    sql(
        connection,
        "DROP TRIGGER IF EXISTS analysis_account_settings",
    )?;
    sql(connection,&format!("CREATE TRIGGER analysis_account_settings AFTER UPDATE OF backtest ON account_settings WHEN json_extract(OLD.backtest,'$.backtest_weight_type') IS NOT json_extract(NEW.backtest,'$.backtest_weight_type') OR json_extract(OLD.backtest,'$.backtest_fee_rate') IS NOT json_extract(NEW.backtest,'$.backtest_fee_rate') BEGIN INSERT INTO account_analysis(account_id,source_version) VALUES(NEW.account_id,1) {}; END",early::ANALYSIS_UPSERT))
}

pub(super) fn delete_trigger(connection: &Connection) -> Result<()> {
    sql(connection,"CREATE TRIGGER account_settings_delete AFTER DELETE ON account BEGIN DELETE FROM account_settings WHERE account_id=OLD.id; DELETE FROM account_notification_state WHERE account_id=OLD.id; END")
}

fn fields() -> Vec<&'static str> {
    std::iter::once("account_config")
        .chain(GROUPS.iter().flat_map(|(_, fields)| fields.iter().copied()))
        .collect()
}

fn account_value(row: &Row<'_>, name: &str) -> rusqlite::Result<Value> {
    let result = value(row, name)?;
    if name == "is_started" {
        return Ok(if result.is_null() {
            Value::Null
        } else {
            json!(result.as_i64().unwrap_or(0) != 0)
        });
    }
    if JSON_FIELDS.contains(&name) {
        return decode(&result).map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(
                0,
                rusqlite::types::Type::Text,
                Box::new(error),
            )
        });
    }
    Ok(result)
}

pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    sql(connection, include_str!("0020.sql"))?;
    {
        let rows = query_rows(connection, "SELECT * FROM account", [], |row| {
            let mut output = vec![
                json!(row.get::<_, i64>("id")?),
                account_value(row, "account_config")?,
            ];
            for (_, fields) in GROUPS {
                let mut group = Map::new();
                for field in *fields {
                    group.insert((*field).into(), account_value(row, field)?);
                }
                output.push(Value::Object(group));
            }
            Ok(output)
        })?;
        for row in rows {
            connection.execute("INSERT INTO account_settings(account_id,connection,execution,schedule,notification,backtest) VALUES(?1,?2,?3,?4,?5,?6)",params![row[0].as_i64(),encoded(&row[1])?,encoded(&row[2])?,encoded(&row[3])?,encoded(&row[4])?,encoded(&row[5])?]).map_err(db)?;
        }
    }
    sql(
        connection,
        "DROP TRIGGER IF EXISTS analysis_account_settings",
    )?;
    for field in fields() {
        sql(
            connection,
            &format!("ALTER TABLE account DROP COLUMN {}", identifier(field)?),
        )?;
    }
    settings_trigger(connection)?;
    delete_trigger(connection)
}

fn add_flat_columns(connection: &Connection) -> Result<()> {
    for field in fields() {
        let kind = if JSON_FIELDS.contains(&field) {
            "JSON"
        } else if FLOAT_FIELDS.contains(&field) {
            "FLOAT"
        } else if field == "is_started" {
            "BOOLEAN"
        } else if matches!(field, "execution_timeout" | "write_empty_record") {
            "INTEGER"
        } else {
            "TEXT"
        };
        let default = match field {
            "execution_timeout" => " DEFAULT 180",
            "backtest_weight_type" => " DEFAULT 'ts'",
            "backtest_fee_rate" => " DEFAULT 0",
            _ => "",
        };
        sql(
            connection,
            &format!(
                "ALTER TABLE account ADD COLUMN {} {kind}{default}",
                identifier(field)?
            ),
        )?;
    }
    Ok(())
}

fn sql_value(name: &str, value: &Value) -> Result<SqlValue> {
    if JSON_FIELDS.contains(&name) {
        return Ok(SqlValue::Text(encoded(value)?));
    }
    Ok(match value {
        Value::Null => SqlValue::Null,
        Value::String(value) => SqlValue::Text(value.clone()),
        Value::Bool(value) => SqlValue::Integer(i64::from(*value)),
        Value::Number(value) => value
            .as_i64()
            .map(SqlValue::Integer)
            .or_else(|| value.as_f64().map(SqlValue::Real))
            .ok_or_else(|| error("invalid account number"))?,
        _ => return Err(error(format!("invalid flat account setting: {name}"))),
    })
}

fn restore_flat_values(connection: &Connection) -> Result<()> {
    let rows = query_rows(connection, "SELECT * FROM account_settings", [], |row| {
        let mut output = vec![("account_config".to_owned(), value(row, "connection")?)];
        for (name, _) in GROUPS {
            output.push(((*name).into(), value(row, name)?));
        }
        Ok((row.get::<_, i64>("account_id")?, output))
    })?;
    let account_columns = columns(connection, "account")?;
    for (id, raw) in rows {
        let mut values = Map::new();
        for (name, value) in raw {
            let value = decode(&value)?;
            if name == "account_config" {
                values.insert(name, value);
            } else {
                values.extend(
                    value
                        .as_object()
                        .ok_or_else(|| error("invalid account settings group"))?
                        .clone(),
                );
            }
        }
        let mut assignments = Vec::new();
        let mut parameters = Vec::new();
        for (name, value) in values {
            if !account_columns.contains(&name) {
                return Err(error(format!("unknown legacy account setting: {name}")));
            }
            assignments.push(format!("{}=?", identifier(&name)?));
            parameters.push(sql_value(&name, &value)?);
        }
        parameters.push(SqlValue::Integer(id));
        connection
            .execute(
                &format!("UPDATE account SET {} WHERE id=?", assignments.join(",")),
                params_from_iter(parameters),
            )
            .map_err(db)?;
    }
    Ok(())
}

fn split_definitions(source: &str) -> Result<(Vec<String>, String)> {
    let start = source
        .find('(')
        .ok_or_else(|| error("invalid account CREATE TABLE"))?;
    let mut definitions = Vec::new();
    let mut begin = start + 1;
    let mut depth = 0_u32;
    let mut quote = None;
    let chars: Vec<(usize, char)> = source.char_indices().collect();
    let mut index = 0;
    while index < chars.len() {
        let (offset, c) = chars[index];
        index += 1;
        if offset <= start {
            continue;
        }
        if let Some(delimiter) = quote {
            if c == delimiter {
                if index < chars.len() && chars[index].1 == delimiter {
                    index += 1;
                } else {
                    quote = None;
                }
            }
            continue;
        }
        match c {
            '\'' | '"' | '`' => quote = Some(c),
            '[' => quote = Some(']'),
            '(' => depth += 1,
            ')' if depth > 0 => depth -= 1,
            ')' => {
                definitions.push(source[begin..offset].trim().to_owned());
                return Ok((definitions, source[offset + 1..].to_owned()));
            }
            ',' if depth == 0 => {
                definitions.push(source[begin..offset].trim().to_owned());
                begin = offset + 1;
            }
            _ => {}
        }
    }
    Err(error("invalid account CREATE TABLE"))
}

fn first_identifier(definition: &str) -> &str {
    let value = definition.trim_start();
    if let Some(quote) = value
        .chars()
        .next()
        .filter(|c| matches!(c, '"' | '`' | '['))
    {
        let end = if quote == '[' { ']' } else { quote };
        let rest = &value[quote.len_utf8()..];
        return rest.split(end).next().unwrap_or("");
    }
    value.split_whitespace().next().unwrap_or("")
}

fn require_flat_columns(connection: &Connection) -> Result<()> {
    let create: String = connection
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='account'",
            [],
            |row| row.get(0),
        )
        .map_err(db)?;
    let (mut definitions, suffix) = split_definitions(&create)?;
    for definition in &mut definitions {
        if REQUIRED.contains(&first_identifier(definition))
            && !definition.to_ascii_uppercase().contains("NOT NULL")
        {
            definition.push_str(" NOT NULL");
        }
    }
    let indexes = query_rows(connection, "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='account' AND sql IS NOT NULL", [], |row| row.get::<_, String>(0))?;
    let names = columns(connection, "account")?
        .into_iter()
        .map(|name| identifier(&name))
        .collect::<Result<Vec<_>>>()?
        .join(",");
    sql(connection,&format!("CREATE TABLE _alembic_tmp_account({}){suffix}; INSERT INTO _alembic_tmp_account({names}) SELECT {names} FROM account; DROP TABLE account; ALTER TABLE _alembic_tmp_account RENAME TO account",definitions.join(",")))?;
    for index in indexes {
        sql(connection, &index)?;
    }
    Ok(())
}

pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    sql(connection,"DROP TRIGGER IF EXISTS analysis_account_settings; DROP TRIGGER IF EXISTS account_settings_delete")?;
    let triggers = query_rows(
        connection,
        "SELECT name,sql FROM sqlite_master WHERE type='trigger'",
        [],
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
    )?;
    for (name, _) in &triggers {
        let quoted = name.replace('"', "\"\"");
        sql(connection, &format!("DROP TRIGGER \"{quoted}\""))?;
    }
    add_flat_columns(connection)?;
    restore_flat_values(connection)?;
    require_flat_columns(connection)?;
    for (_, trigger) in triggers {
        sql(connection, &trigger)?;
    }
    early::flat_settings_trigger(connection)?;
    sql(
        connection,
        "DROP TABLE account_notification_state; DROP TABLE account_settings",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ddl_split_keeps_quoted_commas_and_foreign_key_constraints() {
        let (definitions,suffix)=split_definitions("CREATE TABLE account(id INTEGER PRIMARY KEY,name TEXT DEFAULT 'a,b',\"x\" JSON, FOREIGN KEY(id) REFERENCES other(id)) STRICT").unwrap();
        assert_eq!(definitions.len(), 4);
        assert_eq!(suffix, " STRICT");
        assert_eq!(first_identifier(&definitions[2]), "x");
    }

    #[test]
    fn supplement_migration_changes_only_exact_default_and_preserves_settings() {
        let connection = Connection::open_in_memory().unwrap();
        sql(&connection, "CREATE TABLE account_settings(account_id INTEGER PRIMARY KEY,notification JSON NOT NULL,schedule JSON NOT NULL); CREATE TABLE account_notification_state(account_id INTEGER PRIMARY KEY,last_success_at TEXT)").unwrap();
        let custom = format!("{}\n# custom", notification_code::OLD_SUPPLEMENT);
        for (id, code) in [(1, notification_code::OLD_SUPPLEMENT), (2, custom.as_str())] {
            connection
                .execute(
                    "INSERT INTO account_settings VALUES(?1,?2,'{}')",
                    params![
                        id,
                        encoded(
                            &json!({"feishu_key":"saved-key","execution_notification_code":code})
                        )
                        .unwrap()
                    ],
                )
                .unwrap();
        }
        super::super::upgrade(&connection, 23).unwrap();
        for (id, expected) in [(1, notification_code::DEFAULT), (2, custom.as_str())] {
            let raw: String = connection
                .query_row(
                    "SELECT notification FROM account_settings WHERE account_id=?1",
                    [id],
                    |row| row.get(0),
                )
                .unwrap();
            let value: Value = serde_json::from_str(&raw).unwrap();
            assert_eq!(value["execution_notification_code"], expected);
            assert_eq!(value["feishu_key"], "saved-key");
            let schedule: String = connection
                .query_row(
                    "SELECT schedule FROM account_settings WHERE account_id=?1",
                    [id],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(schedule, "{}");
        }
        super::super::downgrade(&connection, 23).unwrap();
        let raw: String = connection
            .query_row(
                "SELECT notification FROM account_settings WHERE account_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let value: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(
            value["execution_notification_code"],
            notification_code::OLD_SUPPLEMENT
        );
        assert_eq!(value["feishu_key"], "saved-key");
    }

    #[test]
    fn default_notification_source_is_frozen_at_historical_revision() {
        assert!(notification_code::DEFAULT.contains("supplement.cancelled"));
        assert!(!notification_code::OLD_SUPPLEMENT.contains("supplement.cancelled"));
        assert!(notification_code::OLD_TYPE.contains("    card = {"));
    }
}
