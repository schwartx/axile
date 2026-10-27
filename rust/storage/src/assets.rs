//! Account asset history; the caller supplies one Store read snapshot.

use std::collections::HashMap;

use rusqlite::{params, types::ValueRef, Connection, Row};
use serde_json::{json, Map, Value};

const ASSET_FIELDS: &[&str] = &[
    "available_cash",
    "total_asset",
    "market_value",
    "currency",
    "update_time",
    "source",
    "extra",
];
const POSITION_FIELDS: &[&str] = &[
    "symbol",
    "volume",
    "available_volume",
    "market_value",
    "direction",
    "avg_price",
    "extra",
];
const FILTER: &str = "s.account_id=?1 AND EXISTS (SELECT 1 FROM business_asset_snapshot a
    WHERE a.asset_id=s.asset_fact_id AND a.observation_valid IS TRUE)";

// Shared by history and dashboard; every matching execution owner must validate.
pub(crate) const OWNER_VALID: &str = "CASE WHEN a.account_snapshot_id IS NOT NULL THEN a.account_snapshot_id IS s.id
        ELSE EXISTS (
            SELECT 1 FROM business_asset_binding b JOIN executerecord r ON r.id=b.record_id
            WHERE b.asset_id=a.asset_id AND b.path='/account_assets'
            AND r.account_id IS s.account_id AND r.execution_id IS s.execution_id)
        AND NOT EXISTS (
            SELECT 1 FROM business_asset_binding b JOIN executerecord r ON r.id=b.record_id
            LEFT JOIN execution_input i ON i.id=a.input_id
            WHERE b.asset_id=a.asset_id AND b.path='/account_assets'
            AND r.account_id IS s.account_id AND r.execution_id IS s.execution_id
            AND (CASE WHEN a.record_id IS NOT NULL THEN a.record_id IS r.id
                ELSE i.id IS NOT NULL AND (a.input_id IS r.input_id OR a.input_id IS r.result_input_id)
                AND i.account_id IS r.account_id
                AND coalesce(i.owner_execution_id,i.execution_id) IS r.execution_id
                AND (i.execution_id IS NULL OR i.execution_id IS r.execution_id)
                AND a.path='/after_account_assets' AND a.scope='after_pending' END) IS NOT TRUE)
        END";

fn database(error: rusqlite::Error) -> String {
    format!("ASSET_READ_ERROR: {error}")
}

fn invalid(message: &str) -> String {
    format!("INVALID_DATA: {message}")
}

fn cell(row: &Row<'_>, name: &str) -> Result<Value, String> {
    if name == "payload_kind" || name == "positions_kind" || name.ends_with("_state") {
        return row
            .get::<_, String>(name)
            .map(Value::String)
            .map_err(database);
    }
    if name == "positions_count" {
        return row
            .get::<_, Option<i64>>(name)
            .map(|value| json!(value))
            .map_err(database);
    }
    let value = match row.get_ref(name).map_err(database)? {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => json!(value),
        ValueRef::Real(value) => serde_json::Number::from_f64(value)
            .map(Value::Number)
            .ok_or_else(|| invalid("nonfinite asset column"))?,
        ValueRef::Text(value) => {
            let text = std::str::from_utf8(value).map_err(|_| invalid("invalid asset text"))?;
            if [
                "payload_value",
                "audit_evidence",
                "positions_value",
                "extra",
            ]
            .contains(&name)
            {
                serde_json::from_str(text).map_err(|error| invalid(&error.to_string()))?
            } else {
                Value::String(text.into())
            }
        }
        ValueRef::Blob(_) => return Err(invalid("unexpected asset blob")),
    };
    if name == "audit_evidence" && !value.is_null() && !value.is_object() {
        return Err(invalid("invalid audit evidence"));
    }
    Ok(value)
}

pub(crate) fn position(row: &Row<'_>) -> Result<Value, String> {
    position_value(|name| cell(row, name))
}

pub(crate) fn observation(row: &Row<'_>, positions: &[(i64, Value)]) -> Result<Value, String> {
    if row.get::<_, String>("payload_kind").map_err(database)? != "object" {
        return Err(invalid("account asset observation is not an object"));
    }
    observation_with(
        |name| cell(row, name),
        |count| {
            if count != Some(positions.len() as i64) {
                return Err(invalid("incomplete position facts"));
            }
            positions
                .iter()
                .enumerate()
                .map(|(ordinal, (index, item))| {
                    if *index != ordinal as i64 {
                        return Err(invalid("invalid position occurrence sequence"));
                    }
                    Ok(item.clone())
                })
                .collect()
        },
    )
}

fn map_cell(row: &Map<String, Value>, name: &str) -> Result<Value, String> {
    Ok(row.get(name).cloned().unwrap_or(Value::Null))
}

fn columns(
    read: &impl Fn(&str) -> Result<Value, String>,
    fields: &[&str],
) -> Result<Map<String, Value>, String> {
    let mut row = Map::new();
    for &name in fields {
        row.insert(name.into(), read(name)?);
    }
    let evidence = read("audit_evidence")?;
    let evidence = if evidence.is_object() {
        evidence
    } else {
        json!({})
    };
    crate::business_detail::columns(&row, fields, &evidence)
}

fn availability(
    view: &mut Map<String, Value>,
    read: &impl Fn(&str) -> Result<Value, String>,
    fields: &[(&str, &str, &str)],
) -> Result<(), String> {
    let mut states = Map::new();
    for &(label, field, column) in fields {
        let state = read(column)?;
        let Some(state) = state.as_str() else {
            return Err(invalid("invalid availability"));
        };
        if !["absent", "null", "value", "invalid"].contains(&state) {
            return Err(invalid("invalid availability"));
        }
        states.insert(label.into(), json!(state));
        if state == "null" {
            view.insert(field.into(), Value::Null);
        }
    }
    view.insert("availability".into(), Value::Object(states));
    Ok(())
}

/// 已解码事实行上的观测还原,供写入端等价比较复用,不必经过 SQL 行。
pub(crate) fn observation_value(
    root: &Map<String, Value>,
    positions: &[Map<String, Value>],
) -> Result<Value, String> {
    observation_with(
        |name| map_cell(root, name),
        |count| {
            if count != Some(positions.len() as i64) {
                return Err(invalid("incomplete position facts"));
            }
            let mut sorted: Vec<_> = positions.iter().collect();
            sorted.sort_by_key(|row| row.get("ordinal").and_then(Value::as_i64).unwrap_or(-1));
            sorted
                .into_iter()
                .enumerate()
                .map(|(ordinal, row)| {
                    if row.get("ordinal").and_then(Value::as_i64) != Some(ordinal as i64) {
                        return Err(invalid("invalid position occurrence sequence"));
                    }
                    position_value(|name| map_cell(row, name))
                })
                .collect()
        },
    )
}

fn observation_with(
    read: impl Fn(&str) -> Result<Value, String>,
    positions: impl FnOnce(Option<i64>) -> Result<Vec<Value>, String>,
) -> Result<Value, String> {
    if read("payload_kind")?.as_str() != Some("object") {
        return read("payload_value");
    }
    let mut view = columns(&read, ASSET_FIELDS)?;
    availability(
        &mut view,
        &read,
        &[
            ("cash", "available_cash", "cash_state"),
            ("equity", "total_asset", "equity_state"),
            ("market_value", "market_value", "market_state"),
        ],
    )?;
    match read("positions_kind")?.as_str() {
        Some("array") => {
            view.insert(
                "positions".into(),
                Value::Array(positions(read("positions_count")?.as_i64())?),
            );
        }
        Some("absent") => {}
        _ => {
            view.insert("positions".into(), read("positions_value")?);
        }
    }
    Ok(Value::Object(view))
}

fn position_value(read: impl Fn(&str) -> Result<Value, String>) -> Result<Value, String> {
    if read("payload_kind")?.as_str() != Some("object") {
        return read("payload_value");
    }
    let mut view = columns(&read, POSITION_FIELDS)?;
    availability(
        &mut view,
        &read,
        &[
            ("volume", "volume", "volume_state"),
            (
                "available_volume",
                "available_volume",
                "available_volume_state",
            ),
            ("market_value", "market_value", "market_value_state"),
            ("avg_price", "avg_price", "avg_price_state"),
        ],
    )?;
    Ok(Value::Object(view))
}

pub fn page(
    connection: &Connection,
    account_id: i64,
    skip: i64,
    limit: i64,
) -> Result<Value, String> {
    let data = read_page(connection, account_id, skip, limit)?;
    let count: i64 = connection
        .query_row(
            &format!("SELECT count(*) FROM account_asset_snapshot s WHERE {FILTER}"),
            [account_id],
            |row| row.get(0),
        )
        .map_err(database)?;
    Ok(json!({"data":data,"count":count}))
}

pub(crate) fn latest(connection: &Connection, account_id: i64) -> Result<Value, String> {
    Ok(read_page(connection, account_id, 0, 1)?
        .into_iter()
        .next()
        .unwrap_or(Value::Null))
}

fn read_page(
    connection: &Connection,
    account_id: i64,
    skip: i64,
    limit: i64,
) -> Result<Vec<Value>, String> {
    if skip < 0 || !(1..=500).contains(&limit) {
        return Err(
            "INVALID_REQUEST: skip must be nonnegative and limit must be 1 through 500".into(),
        );
    }
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(database)?;
    if !exists {
        return Err("ACCOUNT_NOT_FOUND: account does not exist".into());
    }
    let page_sql = format!(
        "SELECT s.* FROM account_asset_snapshot s WHERE {FILTER}
        ORDER BY s.id DESC LIMIT ?3 OFFSET ?2"
    );
    // Every matching record must validate, not just one favorable owner.
    let sql = format!(
        "WITH page AS ({page_sql})
        SELECT a.*,s.id AS snapshot_id,s.account_id AS snapshot_account_id,
        s.source AS snapshot_source,s.execution_id AS snapshot_execution_id,s.created_at,
        {OWNER_VALID} AS owner_valid
        FROM page s JOIN business_asset_snapshot a ON a.asset_id=s.asset_fact_id
        ORDER BY s.id DESC"
    );
    let mut statement = connection.prepare(&sql).map_err(database)?;
    let mut cursor = statement
        .query(params![account_id, skip, limit])
        .map_err(database)?;
    let mut positions: HashMap<String, Vec<(i64, Value)>> = HashMap::new();
    let position_sql = format!(
        "WITH page AS ({page_sql}) SELECT p.* FROM business_asset_position p
         WHERE p.asset_id IN (SELECT asset_fact_id FROM page) ORDER BY p.asset_id,p.ordinal"
    );
    let mut position_statement = connection.prepare(&position_sql).map_err(database)?;
    let mut position_cursor = position_statement
        .query(params![account_id, skip, limit])
        .map_err(database)?;
    while let Some(row) = position_cursor.next().map_err(database)? {
        positions
            .entry(row.get("asset_id").map_err(database)?)
            .or_default()
            .push((row.get("ordinal").map_err(database)?, position(row)?));
    }
    let mut data = Vec::new();
    while let Some(row) = cursor.next().map_err(database)? {
        if !row.get::<_, bool>("owner_valid").map_err(database)? {
            return Err(invalid(
                "asset observation belongs to another snapshot, account or execution",
            ));
        }
        let source: String = row.get("snapshot_source").map_err(database)?;
        if !matches!(source.as_str(), "manual" | "execution") {
            return Err(invalid("invalid snapshot source"));
        }
        let id: String = row.get("asset_id").map_err(database)?;
        let assets = observation(row, positions.get(&id).map(Vec::as_slice).unwrap_or(&[]))?;
        data.push(
            json!({"id":row.get::<_, i64>("snapshot_id").map_err(database)?,
            "account_id":account_id,"assets":assets,"source":source,
            "execution_id":row.get::<_, Option<String>>("snapshot_execution_id").map_err(database)?,
            "created_at":row.get::<_, String>("created_at").map_err(database)?}),
        );
    }
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scalars_preserve_null_absent_and_invalid_evidence() {
        let row = json!({"payload_kind":"object","available_cash":null,"total_asset":0,
            "market_value":null,"currency":null,"update_time":null,"source":null,"extra":null,
            "audit_evidence":{"invalid_fields":{"market_value":"bad"}},
            "cash_state":"null","equity_state":"value","market_state":"invalid",
            "positions_kind":"array","positions_count":0});
        let db = Connection::open_in_memory().unwrap();
        let projection = row
            .as_object()
            .unwrap()
            .keys()
            .map(|key| format!("json_extract(?1, '$.{key}') AS {key}"))
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!("SELECT {projection}");
        let read = |value: &Value| {
            let mut statement = db.prepare(&sql).unwrap();
            let text = value.to_string();
            let mut rows = statement.query([text]).unwrap();
            observation(rows.next().unwrap().unwrap(), &[])
        };
        let result = read(&row).unwrap();
        assert_eq!(result["available_cash"], Value::Null);
        assert!(result.get("market_value").is_none());
        assert!(result.get("currency").is_none());
        assert_eq!(
            result["audit_evidence"]["invalid_fields"]["market_value"],
            "bad"
        );
        assert_eq!(result["positions"], json!([]));
        assert_eq!(
            result,
            observation_value(row.as_object().unwrap(), &[]).unwrap()
        );
        let mut broken = row.clone();
        broken["positions_count"] = json!(1);
        assert_eq!(
            read(&broken).unwrap_err(),
            observation_value(broken.as_object().unwrap(), &[]).unwrap_err()
        );
        broken = row.clone();
        broken["cash_state"] = Value::Null;
        assert!(read(&broken).unwrap_err().starts_with("ASSET_READ_ERROR:"));
        assert!(observation_value(broken.as_object().unwrap(), &[])
            .unwrap_err()
            .contains("invalid availability"));
        broken = row;
        broken["audit_evidence"] = json!([]);
        assert!(read(&broken)
            .unwrap_err()
            .contains("invalid audit evidence"));
        assert!(observation_value(broken.as_object().unwrap(), &[])
            .unwrap()
            .get("audit_evidence")
            .is_none());
    }
}
