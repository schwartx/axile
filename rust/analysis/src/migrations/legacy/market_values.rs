use std::collections::{HashMap, HashSet};

use chrono::{DateTime, NaiveDate, NaiveDateTime};
use rusqlite::{params, OptionalExtension};
use serde_json::json;

use super::*;

fn number(value: Option<&Value>) -> Option<f64> {
    value
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
}

fn positive(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let number = match value {
        Value::String(value) => value.trim().parse().ok(),
        Value::Bool(value) => Some(if *value { 1.0 } else { 0.0 }),
        _ => value.as_f64(),
    }?;
    (number.is_finite() && number > 0.0).then_some(number)
}

fn close(a: f64, b: f64, relative: f64, absolute: f64) -> bool {
    a == b
        || (a.is_finite()
            && b.is_finite()
            && (a - b).abs() <= absolute.max(relative * a.abs().max(b.abs())))
}

type Quote = (f64, f64, Value);

fn ctp_quotes(result: &Value) -> HashMap<String, Quote> {
    let mut quotes = HashMap::new();
    let Some(symbols) = result.get("symbol_results").and_then(Value::as_object) else {
        return quotes;
    };
    for (symbol, item) in symbols {
        let (Some(tick), Some(sizing)) = (
            item.get("first_tick").filter(|value| value.is_object()),
            item.get("sizing").filter(|value| value.is_object()),
        ) else {
            continue;
        };
        let (Some(price), Some(multiplier)) = (
            number(tick.get("last_price")).filter(|value| *value > 0.0),
            number(sizing.get("unit_multiplier")).filter(|value| *value > 0.0),
        ) else {
            continue;
        };
        let timestamp = tick
            .get("timestamp")
            .filter(|value| truthy(value))
            .or_else(|| tick.get("update_time"))
            .cloned()
            .unwrap_or_default();
        quotes.insert(symbol.clone(), (price, multiplier, timestamp));
    }
    quotes
}

fn legacy_positions(assets: &Value) -> Option<&Vec<Value>> {
    let positions = assets
        .get("positions")?
        .as_array()
        .filter(|value| !value.is_empty())?;
    if positions.iter().any(|position| {
        !position.is_object()
            || position
                .get("extra")
                .is_some_and(|extra| !extra.is_object())
            || position.get("market_value_provenance").is_some()
            || position
                .get("extra")
                .and_then(|extra| extra.get("market_value_provenance"))
                .is_some()
    }) {
        return None;
    }
    Some(positions)
}

fn repair_ctp(assets: &Value, quotes: &HashMap<String, Quote>) -> Option<Value> {
    let positions = legacy_positions(assets)?;
    for position in positions {
        let volume = number(position.get("volume"))?;
        let cost = number(position.get("market_value"))?;
        let average = number(position.get("avg_price"));
        let extra_cost = number(
            position
                .get("extra")
                .and_then(|extra| extra.get("position_cost")),
        );
        if extra_cost.is_some_and(|extra| !close(cost, extra, 1e-9, 1e-6))
            || average.is_some_and(|average| average <= 0.0)
        {
            return None;
        }
        if let Some(average) = average.filter(|average| *average > 0.0 && volume > 0.0) {
            let ratio = cost / volume / average;
            if !close(ratio, ratio.round(), 1e-6, 1e-6) {
                return None;
            }
        }
        let quote = position
            .get("symbol")
            .and_then(Value::as_str)
            .and_then(|symbol| quotes.get(symbol));
        if extra_cost.is_none()
            && average.is_none()
            && quote.is_some_and(|quote| close(cost, volume.abs() * quote.0 * quote.1, 1e-6, 0.0))
        {
            return None;
        }
    }
    let mut repaired = assets.clone();
    let mut total = 0.0;
    let mut complete = true;
    for position in repaired["positions"].as_array_mut()? {
        let quote = position
            .get("symbol")
            .and_then(Value::as_str)
            .and_then(|symbol| quotes.get(symbol));
        let value = number(position.get("volume"))
            .zip(quote)
            .map(|(volume, quote)| volume.abs() * quote.0 * quote.1)
            .filter(|value| value.is_finite());
        let cost = position.get("market_value").cloned().unwrap_or_default();
        let extra = position
            .as_object_mut()?
            .entry("extra")
            .or_insert_with(|| json!({}))
            .as_object_mut()?;
        extra.entry("position_cost").or_insert(cost);
        if let Some(value) = value {
            extra.insert(
                "market_value_provenance".into(),
                json!({"source":"ctp_first_tick_last_price_0014","quote_timestamp":quote?.2}),
            );
            total += value;
        } else {
            extra.insert(
                "market_value_provenance".into(),
                json!({"source":"unavailable","quote_timestamp":null}),
            );
            complete = false;
        }
        position["market_value"] = value.map_or(Value::Null, |value| json!(value));
    }
    repaired["market_value"] = if complete && total.is_finite() {
        json!(total)
    } else {
        Value::Null
    };
    Some(repaired)
}

fn timestamp(connection: &Connection, value: &Value) -> Option<f64> {
    if let Some(number) = value
        .as_f64()
        .or_else(|| value.as_bool().map(|value| if value { 1.0 } else { 0.0 }))
    {
        if !number.is_finite() {
            return None;
        }
        return Some(if number > 1e17 {
            number / 1e9
        } else if number > 1e11 {
            number / 1e3
        } else {
            number
        });
    }
    let value = value.as_str()?;
    if let Ok(time) = DateTime::parse_from_rfc3339(value) {
        return Some(time.timestamp() as f64 + f64::from(time.timestamp_subsec_nanos()) / 1e9);
    }
    let normalized = value.replace('T', " ");
    let naive = [
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y%m%d %H%M%S%.f",
    ]
    .iter()
    .find_map(|format| NaiveDateTime::parse_from_str(&normalized, format).ok())
    .or_else(|| {
        NaiveDate::parse_from_str(value, "%Y-%m-%d")
            .ok()?
            .and_hms_opt(0, 0, 0)
    })?;
    // 旧 Python 对无时区时间使用宿主本地时区；SQLite 同样读取宿主时区。
    let seconds = naive.and_utc().timestamp();
    let local: Option<i64> = connection
        .query_row(
            "SELECT CAST(strftime('%s', ?1, 'utc') AS INTEGER)",
            [naive.format("%Y-%m-%d %H:%M:%S").to_string()],
            |row| row.get(0),
        )
        .ok()
        .flatten();
    Some(
        local.unwrap_or(seconds) as f64 + f64::from(naive.and_utc().timestamp_subsec_nanos()) / 1e9,
    )
}

fn tq_quote(
    connection: &Connection,
    assets: &Value,
    position: &Value,
    observed: &Value,
) -> Option<Quote> {
    let symbol = position
        .get("extra")
        .and_then(|extra| extra.get("tq_symbol"))
        .filter(|value| truthy(value))
        .or_else(|| position.get("symbol"))?
        .as_str()?;
    let row = assets
        .get("extra")?
        .get("quotes")?
        .get(symbol)?
        .as_object()?;
    let declared = row
        .get("tq_symbol")
        .filter(|value| truthy(value))
        .or_else(|| row.get("symbol"));
    if declared != Some(&json!(symbol)) {
        return None;
    }
    let price = positive(row.get("last_price"))?;
    let multiplier = positive(row.get("volume_multiple"))?;
    let quote_time = row
        .get("datetime")
        .filter(|value| truthy(value))
        .or_else(|| row.get("timestamp").filter(|value| truthy(value)))
        .or_else(|| row.get("update_time"))?;
    let age = timestamp(connection, observed)? - timestamp(connection, quote_time)?;
    (0.0..=300.0)
        .contains(&age)
        .then(|| (price, multiplier, quote_time.clone()))
}

fn repair_tq(connection: &Connection, assets: &Value, observed: &Value) -> Option<Value> {
    let positions = legacy_positions(assets)?;
    if positions.iter().any(|position| {
        positive(position.get("volume")).is_none()
            || !position
                .get("market_value")
                .is_some_and(|value| value.is_number() || value.is_boolean())
    }) {
        return None;
    }
    let mut repaired = assets.clone();
    let mut values = Vec::new();
    for position in repaired["positions"].as_array_mut()? {
        let evidence = tq_quote(connection, assets, position, observed);
        let cost = position.get("market_value").cloned().unwrap_or_default();
        let volume = positive(position.get("volume"))?;
        let extra = position
            .as_object_mut()?
            .entry("extra")
            .or_insert_with(|| json!({}))
            .as_object_mut()?;
        extra.insert("position_cost".into(), cost);
        let (value, provenance) = match evidence {
            None => (
                None,
                json!({"source":"unavailable","reason":"no_same_observation_quote"}),
            ),
            Some((price, multiplier, time)) => {
                let value = volume.abs() * price * multiplier;
                if value.is_finite() {
                    values.push(value);
                    (
                        Some(value),
                        json!({"source":"tq_historical_last_price_0015","price":price,"multiplier":multiplier,"quote_time":time}),
                    )
                } else {
                    (
                        None,
                        json!({"source":"unavailable","reason":"calculation_overflow"}),
                    )
                }
            }
        };
        extra.insert("market_value_provenance".into(), provenance);
        position["market_value"] = value.map_or(Value::Null, |value| json!(value));
    }
    let total: f64 = values.iter().sum();
    repaired["market_value"] = if values.len() == positions.len() && total.is_finite() {
        json!(total)
    } else {
        Value::Null
    };
    Some(repaired)
}

struct Observation {
    id: i64,
    account: Option<i64>,
    execution: Option<String>,
    kind: Option<String>,
    created: Value,
    raw: Value,
}

fn observations(
    connection: &Connection,
    table: &str,
    column: &str,
    predicate: &str,
    args: &[&dyn rusqlite::ToSql],
    mut visit: impl FnMut(Observation) -> Result<()>,
) -> Result<()> {
    let columns = columns(connection, table)?;
    let account = if columns.contains("account_id") {
        "account_id"
    } else {
        "NULL AS account_id"
    };
    let kind = if columns.contains("artifact_type") {
        "artifact_type"
    } else {
        "NULL AS artifact_type"
    };
    let mut last = -1;
    loop {
        let rows = query_rows(connection, &format!("SELECT id,{account},execution_id,{kind},created_at,{column} FROM {table} WHERE id>{last} AND ({predicate}) ORDER BY id LIMIT 500"), args, |row| {
            Ok(Observation {
                id: row.get("id")?,
                account: row.get("account_id")?,
                execution: row.get("execution_id")?,
                kind: row.get("artifact_type")?,
                created: value(row, "created_at")?,
                raw: value(row, column)?,
            })
        })?;
        if rows.is_empty() {
            return Ok(());
        }
        for row in rows {
            last = row.id;
            visit(row)?;
        }
    }
}

fn backup_name(revision: u32) -> &'static str {
    if revision == 14 {
        "axile_0014_market_value_backup"
    } else {
        "axile_0015_tq_market_value_backup"
    }
}

fn create_backup(connection: &Connection, revision: u32) -> Result<()> {
    sql(connection,&format!("CREATE TABLE {}(id INTEGER NOT NULL PRIMARY KEY,table_name TEXT NOT NULL,row_id INTEGER NOT NULL,json_column TEXT NOT NULL,before JSON NOT NULL,after JSON NOT NULL,execution_id TEXT)",backup_name(revision)))
}

fn save_repair(
    connection: &Connection,
    revision: u32,
    table: &str,
    column: &str,
    row: &Observation,
    repair: impl FnOnce(&Value) -> Option<Value>,
) -> Result<()> {
    let Some(before) = document(&row.raw) else {
        return Ok(());
    };
    let assets = if column == "assets" {
        &before
    } else {
        before.get("account_assets").unwrap_or(&Value::Null)
    };
    let Some(repaired) = repair(assets) else {
        return Ok(());
    };
    let after = if column == "assets" {
        repaired
    } else {
        let mut after = before.clone();
        after["account_assets"] = repaired;
        after
    };
    let encoded_after = encoded(&after)?;
    connection.execute(&format!("INSERT INTO {}(table_name,row_id,json_column,before,after,execution_id) VALUES(?1,?2,?3,?4,?5,?6)",backup_name(revision)),params![table,row.id,column,encoded(&before)?,encoded_after,row.execution]).map_err(db)?;
    connection
        .execute(
            &format!("UPDATE {table} SET {column}=?1 WHERE id=?2"),
            params![encoded_after, row.id],
        )
        .map_err(db)?;
    Ok(())
}

fn declared_channel(document: &Value) -> Option<&Value> {
    document
        .get("channel_type")
        .or_else(|| document.get("channel"))
}

fn ctp_eligible(result: &Value, input: &Value, channel: &str) -> bool {
    let declared = declared_channel(result)
        .filter(|value| !value.is_null())
        .or_else(|| declared_channel(input));
    channel == "ctp"
        && declared
            .filter(|value| !value.is_null())
            .map_or(true, |value| {
                value
                    .as_str()
                    .map_or_else(|| value.to_string(), str::to_owned)
                    .to_lowercase()
                    == "ctp"
            })
}

pub(super) fn upgrade_ctp(connection: &Connection) -> Result<()> {
    create_backup(connection, 14)?;
    observations(connection, "executerecord", "raw_result", "1", &[], |row| {
        let channel: Option<String> = connection
            .query_row(
                "SELECT trade_channel FROM account WHERE id=?1",
                [row.account],
                |row| row.get(0),
            )
            .optional()
            .map_err(db)?;
        let Some(channel) = channel else {
            return Ok(());
        };
        let Some(result) = document(&row.raw) else {
            return Ok(());
        };
        let raw_input: Option<String> = connection
            .query_row(
                "SELECT raw_input FROM executerecord WHERE id=?1",
                [row.id],
                |row| row.get(0),
            )
            .map_err(db)?;
        let input = raw_input
            .and_then(|value| serde_json::from_str(&value).ok())
            .unwrap_or_default();
        if !ctp_eligible(&result, &input, &channel) || row.execution.is_none() {
            return Ok(());
        }
        let quotes = ctp_quotes(&result);
        let repair = |table: &str, column: &str, target: &Observation| {
            save_repair(connection, 14, table, column, target, |assets| {
                repair_ctp(assets, &quotes)
            })
        };
        repair("executerecord", "raw_result", &row)?;
        for (table,column,predicate) in [("account_asset_snapshot","assets","execution_id=?1 AND account_id=?2"),("execution_artifact","content","execution_id=?1 AND artifact_type IN('ACCOUNT_SNAPSHOT','ACCOUNT_SNAPSHOT_BEFORE')")] {
            let args: Vec<&dyn rusqlite::ToSql> = if table == "account_asset_snapshot" { vec![&row.execution, &row.account] } else { vec![&row.execution] };
            observations(connection, table, column, predicate, &args, |target| repair(table, column, &target))?;
        }
        Ok(())
    })
}

pub(super) fn upgrade_tq(connection: &Connection) -> Result<()> {
    create_backup(connection, 15)?;
    let accounts: HashSet<i64> = query_rows(
        connection,
        "SELECT id FROM account WHERE trade_channel='tq'",
        [],
        |row| row.get(0),
    )?
    .into_iter()
    .collect();
    let mut execution_accounts = HashMap::new();
    observations(connection, "executerecord", "raw_result", "1", &[], |row| {
        if let Some(account) = row.account.filter(|account| accounts.contains(account)) {
            if let Some(execution) = row.execution.filter(|execution| !execution.is_empty()) {
                execution_accounts.insert(execution, account);
            }
        }
        Ok(())
    })?;
    for (table, column) in [
        ("account_asset_snapshot", "assets"),
        ("executerecord", "raw_result"),
        ("execution_artifact", "content"),
    ] {
        observations(connection, table, column, "1", &[], |row| {
            let account = row.account.or_else(|| {
                row.execution
                    .as_ref()
                    .and_then(|execution| execution_accounts.get(execution).copied())
            });
            if !account.is_some_and(|account| accounts.contains(&account))
                || (table == "execution_artifact"
                    && !matches!(
                        row.kind.as_deref(),
                        Some("ACCOUNT_SNAPSHOT" | "ACCOUNT_SNAPSHOT_BEFORE")
                    ))
            {
                return Ok(());
            }
            save_repair(connection, 15, table, column, &row, |assets| {
                let observed = assets
                    .get("update_time")
                    .filter(|value| truthy(value))
                    .unwrap_or(&row.created);
                repair_tq(connection, assets, observed)
            })
        })?;
    }
    Ok(())
}

pub(super) fn restore(connection: &Connection, revision: u32) -> Result<()> {
    let backup = backup_name(revision);
    let rows = query_rows(connection, &format!("SELECT table_name,row_id,json_column,before,after,execution_id FROM {backup} ORDER BY id"), [], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, String>(2)?,
                value(row, "before")?,
                value(row, "after")?,
                row.get::<_, Option<String>>(5)?,
            ))
        })?;
    for (table, id, column, _, after, execution) in &rows {
        if !matches!(
            table.as_str(),
            "executerecord" | "account_asset_snapshot" | "execution_artifact"
        ) {
            return Err(error(format!(
                "{revision:04} restore conflict: unknown table"
            )));
        }
        let current = connection
            .query_row(
                &format!(
                    "SELECT execution_id,{} FROM {} WHERE id=?1",
                    identifier(column)?,
                    identifier(table)?
                ),
                [id],
                |row| Ok((row.get::<_, Option<String>>(0)?, value(row, column)?)),
            )
            .optional()
            .map_err(db)?;
        if current
            .as_ref()
            .map_or(true, |(current_execution, current)| {
                current_execution != execution
                    || !document(current)
                        .zip(decode(after).ok())
                        .is_some_and(|(current, after)| same_json(&current, &after))
            })
        {
            return Err(error(format!(
                "{revision:04} restore conflict: {table}:{id}"
            )));
        }
    }
    for (table, id, column, before, _, _) in rows {
        connection
            .execute(
                &format!(
                    "UPDATE {} SET {}=?1 WHERE id=?2",
                    identifier(&table)?,
                    identifier(&column)?
                ),
                params![encoded(&decode(&before)?)?, id],
            )
            .map_err(db)?;
    }
    sql(connection, &format!("DROP TABLE {backup}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn observations_continue_across_pages_while_rewriting_rows() {
        let connection = Connection::open_in_memory().unwrap();
        sql(&connection, "CREATE TABLE account_asset_snapshot(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,created_at TEXT,assets JSON); WITH RECURSIVE ids(id) AS (SELECT 1 UNION ALL SELECT id+1 FROM ids WHERE id<501) INSERT INTO account_asset_snapshot SELECT id,1,'e','now','{}' FROM ids").unwrap();
        let mut visited = Vec::new();
        observations(
            &connection,
            "account_asset_snapshot",
            "assets",
            "1",
            &[],
            |row| {
                visited.push(row.id);
                connection
                    .execute(
                        "UPDATE account_asset_snapshot SET assets='null' WHERE id=?1",
                        [row.id],
                    )
                    .map_err(db)?;
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(visited, (1..=501).collect::<Vec<_>>());
    }

    #[test]
    fn ctp_repair_retains_cost_and_marks_unavailable_quotes() {
        let assets = json!({"total_asset":500,"market_value":30,"positions":[{"symbol":"rb","volume":2,"market_value":20,"avg_price":1},{"symbol":"ag","volume":1,"market_value":10,"avg_price":1}]});
        let quotes = HashMap::from([("rb".into(), (100.0, 10.0, json!("now")))]);
        let repaired = repair_ctp(&assets, &quotes).unwrap();
        assert_eq!(repaired["positions"][0]["market_value"], 2000.0);
        assert_eq!(repaired["positions"][0]["extra"]["position_cost"], 20);
        assert!(repaired["market_value"].is_null());
        assert_eq!(repaired["total_asset"], 500);
        assert!(repair_ctp(&repaired, &quotes).is_none());
    }

    #[test]
    fn tq_repair_requires_same_contract_and_five_minute_evidence() {
        let connection = Connection::open_in_memory().unwrap();
        let assets = json!({"positions":[{"symbol":"SHFE.rb","volume":2,"market_value":20}],"extra":{"quotes":{"SHFE.rb":{"symbol":"SHFE.rb","last_price":100,"volume_multiple":10,"datetime":"2026-09-22T14:59:00"}}}});
        let repaired = repair_tq(&connection, &assets, &json!("2026-09-22T15:00:00")).unwrap();
        assert_eq!(repaired["market_value"], 2000.0);
        let repaired = repair_tq(&connection, &assets, &json!("2026-09-22T15:05:00")).unwrap();
        assert!(repaired["market_value"].is_null());
    }
}
