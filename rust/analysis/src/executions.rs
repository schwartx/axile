//! Bounded public execution headers and scalar analysis summaries.

use std::collections::HashMap;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

const FILTER: &str = "r.account_id=?1 AND (?2 IS NULL OR r.execution_id=?2)
    AND (?3 IS NULL OR r.id=?3)";
const INTEGER_FIELDS: &[&str] = &["id", "account_id", "is_success"];
const BOOLEAN_FIELDS: &[&str] = &[
    "success",
    "termination_cancel_attempted",
    "termination_forced",
    "termination_cancel_unconfirmed",
];
const TEXT_FIELDS: &[&str] = &[
    "created_at",
    "execution_id",
    "input_id",
    "result_input_id",
    "status",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
    "channel_type",
    "display_status",
    "display_error",
    "display_outcome",
    "display_outcome_reason",
    "termination_reason",
    "termination_mode",
    "termination_trigger",
    "termination_requested_at",
    "termination_acked_at",
    "termination_finished_at",
];
const SYMBOL_FIELDS: &[&str] = &[
    "status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
];

fn database(error: rusqlite::Error) -> String {
    format!("EXECUTION_READ_ERROR: {error}")
}

/// Read within the caller's Store transaction; never load execution payloads.
pub fn page(
    connection: &Connection,
    account_id: i64,
    skip: i64,
    limit: i64,
    execution_id: Option<&str>,
    record_id: Option<i64>,
) -> Result<Value, String> {
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
    let arguments = params![account_id, execution_id, record_id, limit, skip];
    let count: i64 = connection
        .query_row(
            &format!("SELECT count(*) FROM executerecord r WHERE {FILTER}"),
            params![account_id, execution_id, record_id],
            |row| row.get(0),
        )
        .map_err(database)?;
    let page_ids = format!(
        "SELECT r.id FROM executerecord r WHERE {FILTER}
        ORDER BY r.id DESC LIMIT ?4 OFFSET ?5"
    );
    let mut symbol_statement = connection.prepare(&format!(
        "WITH page AS ({page_ids})
         SELECT s.record_id,s.symbol,s.present,s.status,s.error,s.outcome,s.outcome_reason,s.reason_code
         FROM analysis_source_execution_symbol s JOIN page p ON p.id=s.record_id
         JOIN analysis_source_execution e ON e.record_id=s.record_id
         WHERE e.projection_error IS NULL AND e.has_symbol_results
         ORDER BY s.record_id,s.symbol"
    )).map_err(database)?;
    let mut symbols: HashMap<i64, Map<String, Value>> = HashMap::new();
    let mut symbol_rows = symbol_statement.query(arguments).map_err(database)?;
    while let Some(row) = symbol_rows.next().map_err(database)? {
        let present: i64 = row.get("present").map_err(database)?;
        let mut fields = Map::new();
        for (bit, name) in SYMBOL_FIELDS.iter().enumerate() {
            if present & (1 << bit) != 0 {
                fields.insert(
                    (*name).into(),
                    json!(row.get::<_, Option<String>>(*name).map_err(database)?),
                );
            }
        }
        symbols
            .entry(row.get("record_id").map_err(database)?)
            .or_default()
            .insert(row.get("symbol").map_err(database)?, Value::Object(fields));
    }
    let headers = TEXT_FIELDS
        .iter()
        .chain(INTEGER_FIELDS)
        .chain(BOOLEAN_FIELDS)
        .copied()
        .chain(std::iter::once("execution_time"))
        .map(|name| format!("r.{name}"))
        .collect::<Vec<_>>()
        .join(",");
    let mut statement = connection
        .prepare(&format!(
            "WITH page AS ({page_ids}) SELECT {headers},s.record_id AS source_id,
         s.projection_error,s.has_symbol_results,s.trade_count,s.symbol_count
         FROM page p JOIN executerecord r ON r.id=p.id
         LEFT JOIN analysis_source_execution s ON s.record_id=r.id ORDER BY r.id DESC"
        ))
        .map_err(database)?;
    let mut rows = statement.query(arguments).map_err(database)?;
    let mut data = Vec::new();
    while let Some(row) = rows.next().map_err(database)? {
        let id: i64 = row.get("id").map_err(database)?;
        let mut header = Map::new();
        for name in TEXT_FIELDS {
            header.insert(
                (*name).into(),
                json!(row.get::<_, Option<String>>(*name).map_err(database)?),
            );
        }
        for &name in INTEGER_FIELDS {
            header.insert(
                name.into(),
                json!(row.get::<_, i64>(name).map_err(database)?),
            );
        }
        header.insert(
            "execution_time".into(),
            json!(row
                .get::<_, Option<f64>>("execution_time")
                .map_err(database)?),
        );
        for &name in BOOLEAN_FIELDS {
            header.insert(
                name.into(),
                json!(row.get::<_, Option<bool>>(name).map_err(database)?),
            );
        }
        let projected = row
            .get::<_, Option<i64>>("source_id")
            .map_err(database)?
            .is_some()
            && row
                .get::<_, Option<String>>("projection_error")
                .map_err(database)?
                .is_none();
        let symbol_results =
            if projected && row.get::<_, bool>("has_symbol_results").map_err(database)? {
                Value::Object(symbols.remove(&id).unwrap_or_default())
            } else {
                Value::Null
            };
        header.insert("symbol_results".into(), symbol_results);
        for name in ["trade_count", "symbol_count"] {
            header.insert(
                name.into(),
                if projected {
                    json!(row.get::<_, Option<i64>>(name).map_err(database)?)
                } else {
                    Value::Null
                },
            );
        }
        data.push(Value::Object(header));
    }
    Ok(json!({"data":data,"count":count}))
}

/// Read one account-owned execution and its complete cold business evidence.
pub fn detail(connection: &Connection, account_id: i64, record_id: i64) -> Result<Value, String> {
    let mut page = page(connection, account_id, 0, 1, None, Some(record_id))?;
    let mut record = page["data"]
        .as_array_mut()
        .expect("execution page")
        .pop()
        .ok_or_else(|| "RECORD_NOT_FOUND: execution record does not exist".to_owned())?;
    let facts = crate::business_detail::read(connection, account_id, record_id)?;
    let Value::Object(facts) = facts else {
        return Err("EXECUTION_READ_ERROR: invalid execution detail shape".into());
    };
    record
        .as_object_mut()
        .expect("execution header")
        .extend(facts);
    Ok(record)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_page_preserves_nulls_and_projection_fallback() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY);
             INSERT INTO account VALUES(1);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,is_success INTEGER,
             execution_time REAL,success INTEGER,termination_cancel_attempted INTEGER,
             termination_forced INTEGER,termination_cancel_unconfirmed INTEGER);
             CREATE TABLE analysis_source_execution(record_id INTEGER PRIMARY KEY,projection_error TEXT,
             has_symbol_results INTEGER,trade_count INTEGER,symbol_count INTEGER);
             CREATE TABLE analysis_source_execution_symbol(record_id INTEGER,symbol TEXT,present INTEGER,
             status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT);"
        ).unwrap();
        for name in TEXT_FIELDS {
            connection
                .execute_batch(&format!(
                    "ALTER TABLE executerecord ADD COLUMN {name} TEXT;"
                ))
                .unwrap();
        }
        connection.execute_batch(
            "INSERT INTO executerecord(id,account_id,is_success,success,termination_forced,execution_id)
             VALUES(1,1,1,1,0,'one'),(2,1,0,NULL,NULL,'two'),(3,1,0,NULL,NULL,'three');
             INSERT INTO analysis_source_execution VALUES(1,NULL,1,2,1),(2,'bad',1,99,99);
             INSERT INTO analysis_source_execution_symbol VALUES(1,'BTC',3,NULL,'failed',NULL,NULL,NULL);"
        ).unwrap();
        let result = page(&connection, 1, 0, 500, None, None).unwrap();
        assert_eq!(result["count"], 3);
        assert_eq!(result["data"][0]["id"], 3);
        for index in [0, 1] {
            for name in ["symbol_results", "trade_count", "symbol_count"] {
                assert!(result["data"][index][name].is_null());
            }
        }
        let good = &result["data"][2];
        assert_eq!(
            good["symbol_results"],
            json!({"BTC":{"status":null,"error":"failed"}})
        );
        assert_eq!(good["success"], true);
        assert_eq!(good["termination_forced"], false);
        assert!(!good.as_object().unwrap().contains_key("audit_evidence"));
        let filtered = page(&connection, 1, 0, 1, Some("one"), Some(1)).unwrap();
        assert_eq!(filtered["count"], 1);
        assert_eq!(filtered["data"][0], *good);
        assert!(
            page(&connection, 1, 1, 1, Some("one"), None).unwrap()["data"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(page(&connection, 2, 0, 1, None, None)
            .unwrap_err()
            .starts_with("ACCOUNT_NOT_FOUND:"));
        assert!(page(&connection, 1, -1, 1, None, None)
            .unwrap_err()
            .starts_with("INVALID_REQUEST:"));
        connection
            .execute(
                "UPDATE analysis_source_execution SET has_symbol_results=0 WHERE record_id=1",
                [],
            )
            .unwrap();
        assert!(
            page(&connection, 1, 0, 1, None, Some(1)).unwrap()["data"][0]["symbol_results"]
                .is_null()
        );
    }
}
