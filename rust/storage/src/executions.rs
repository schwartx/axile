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
pub(crate) use crate::execution_symbol_display::symbol_from_row;

fn database(error: rusqlite::Error) -> String {
    format!("EXECUTION_READ_ERROR: {error}")
}

fn account_exists(connection: &Connection, account_id: i64) -> Result<(), String> {
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
    Ok(())
}

fn records(
    connection: &Connection,
    page_ids: &str,
    arguments: impl rusqlite::Params + Copy,
) -> Result<Vec<Value>, String> {
    let mut symbol_statement = connection.prepare(&format!(
        "WITH page AS ({page_ids})
         SELECT s.record_id,s.map_key AS symbol,s.node_kind,s.status,s.error,s.outcome,s.outcome_reason,s.reason_code,s.memory
         FROM execution_symbol_result s JOIN page p ON p.id=s.record_id
         ORDER BY s.record_id,s.map_key"
    )).map_err(database)?;
    let mut symbols: HashMap<i64, Map<String, Value>> = HashMap::new();
    let mut symbol_rows = symbol_statement.query(arguments).map_err(database)?;
    while let Some(row) = symbol_rows.next().map_err(database)? {
        let fields =
            symbol_from_row(row).map_err(|error| format!("EXECUTION_READ_ERROR: {error}"))?;
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
            "WITH page AS ({page_ids}) SELECT {headers},r.facts_complete,
         EXISTS(SELECT 1 FROM execution_symbol_collection c WHERE c.record_id=r.id AND c.path='/symbol_results' AND c.node_kind='object') AS has_symbol_results,
         (SELECT COUNT(*) FROM business_trade t JOIN business_trade_collection c ON c.record_id=t.record_id AND c.path=t.collection_path JOIN execution_symbol_result s ON s.record_id=c.record_id AND s.path=c.parent_path WHERE t.record_id=r.id AND c.state='array') AS trade_count,
         (SELECT COUNT(DISTINCT s.map_key) FROM business_trade t JOIN business_trade_collection c ON c.record_id=t.record_id AND c.path=t.collection_path JOIN execution_symbol_result s ON s.record_id=c.record_id AND s.path=c.parent_path WHERE t.record_id=r.id AND c.state='array' AND t.trade_volume>0) AS symbol_count
         FROM page p JOIN executerecord r ON r.id=p.id ORDER BY r.id DESC"
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
        if !row.get::<_, bool>("facts_complete").map_err(database)? {
            return Err(format!(
                "SOURCE_FACTS_INCOMPLETE: record {id}: incomplete business facts"
            ));
        }
        let symbol_results = if row.get::<_, bool>("has_symbol_results").map_err(database)? {
            Value::Object(symbols.remove(&id).unwrap_or_default())
        } else {
            Value::Null
        };
        header.insert("symbol_results".into(), symbol_results);
        for name in ["trade_count", "symbol_count"] {
            header.insert(
                name.into(),
                json!(row.get::<_, Option<i64>>(name).map_err(database)?),
            );
        }
        data.push(Value::Object(header));
    }
    Ok(data)
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
    account_exists(connection, account_id)?;
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
    let data = records(connection, &page_ids, arguments)?;
    Ok(json!({"data":data,"count":count}))
}

/// Read one account-owned execution and its complete cold business evidence.
pub fn detail(connection: &Connection, account_id: i64, record_id: i64) -> Result<Value, String> {
    account_exists(connection, account_id)?;
    let mut record = records(
        connection,
        "SELECT id FROM executerecord WHERE account_id=?1 AND id=?2",
        params![account_id, record_id],
    )?
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

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY);
             INSERT INTO account VALUES(1);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,is_success INTEGER,
             execution_time REAL,success INTEGER,termination_cancel_attempted INTEGER,
             termination_forced INTEGER,termination_cancel_unconfirmed INTEGER,facts_complete INTEGER DEFAULT 1);
             CREATE TABLE execution_symbol_collection(record_id INTEGER,path TEXT,node_kind TEXT);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT,memory TEXT,node_kind TEXT DEFAULT 'object');
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,parent_path TEXT,state TEXT);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT,trade_volume REAL);"
        ).unwrap();
        for name in TEXT_FIELDS {
            connection
                .execute_batch(&format!(
                    "ALTER TABLE executerecord ADD COLUMN {name} TEXT;"
                ))
                .unwrap();
        }
        connection
    }

    #[test]
    fn public_page_preserves_nulls_and_projection_fallback() {
        let connection = fixture();
        connection.execute_batch(
            "INSERT INTO executerecord(id,account_id,is_success,success,termination_forced,execution_id)
             VALUES(1,1,1,1,0,'one'),(2,1,0,NULL,NULL,'two'),(3,1,0,NULL,NULL,'three');
             INSERT INTO execution_symbol_collection VALUES(1,'/symbol_results','object');
             INSERT INTO execution_symbol_result(record_id,path,map_key,status,error,outcome,outcome_reason,reason_code,memory) VALUES(1,'/symbol_results/SHFE.rb2610','SHFE.rb2610',NULL,'failed',NULL,NULL,NULL,NULL);"
        ).unwrap();
        let result = page(&connection, 1, 0, 500, None, None).unwrap();
        assert_eq!(result["count"], 3);
        assert_eq!(result["data"][0]["id"], 3);
        for index in [0, 1] {
            assert!(result["data"][index]["symbol_results"].is_null());
            assert_eq!(result["data"][index]["trade_count"], 0);
            assert_eq!(result["data"][index]["symbol_count"], 0);
        }
        let direct = records(
            &connection,
            "SELECT id FROM executerecord WHERE account_id=?1 AND id=?2",
            params![1, 1],
        )
        .unwrap();
        assert_eq!(direct, vec![result["data"][2].clone()]);
        assert!(records(
            &connection,
            "SELECT id FROM executerecord WHERE account_id=?1 AND id=?2",
            params![2, 1],
        )
        .unwrap()
        .is_empty());
        let good = &result["data"][2];
        assert_eq!(
            good["symbol_results"],
            json!({"SHFE.rb2610":{"error":"failed","outcome":null}})
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
                "DELETE FROM execution_symbol_collection WHERE record_id=1",
                [],
            )
            .unwrap();
        assert!(
            page(&connection, 1, 0, 1, None, Some(1)).unwrap()["data"][0]["symbol_results"]
                .is_null()
        );
    }

    #[test]
    fn compact_symbols_match_python_display_without_backfilling_source_facts() {
        let connection = fixture();
        connection.execute_batch(
            "INSERT INTO executerecord(id,account_id,is_success) VALUES(1,1,1);
             INSERT INTO execution_symbol_collection VALUES(1,'/symbol_results','object');
             INSERT INTO execution_symbol_result(record_id,path,map_key,status,memory,node_kind) VALUES
             (1,'/symbol_results/noop','noop','NOOP','{\"secret\":\"private\"}','object'),
             (1,'/symbol_results/partial','partial','PARTIAL','{\"message\":\"RuntimeError: SDK\",\"symbol_decision_reason_code\":\"CLOSED\"}','object'),
             (1,'/symbol_results/malformed','malformed',NULL,NULL,'value');"
        ).unwrap();
        let records = page(&connection, 1, 0, 1, None, None).unwrap();
        // 冻结 Python normalize_legacy_result 后的五字段列表投影。
        assert_eq!(
            records["data"][0]["symbol_results"],
            json!({
                "noop":{"status":"NOOP","outcome":"completed"},
                "partial":{"status":"PARTIAL","outcome":"not_reached","error":"执行不到位","reason_code":"COMMON.SESSION.CLOSED"},
                "malformed":{"outcome":null}
            })
        );
        assert_eq!(connection.query_row("SELECT count(*) FROM execution_symbol_result WHERE outcome IS NULL AND error IS NULL AND reason_code IS NULL", [], |row| row.get::<_,i64>(0)).unwrap(),3);
    }
}
