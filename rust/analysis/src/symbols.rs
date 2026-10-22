//! Live activity symbols from scalar trade facts, in the caller's read transaction.

use std::collections::HashMap;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use crate::costs::{Summary, AGGREGATES};
use crate::journal::historical_summary;

const TRADES: &str = "WITH trades AS (
    SELECT t.*,r.created_at,r.execution_id FROM analysis_source_trade t
    JOIN executerecord r ON r.id=t.record_id
    WHERE r.account_id=?1 AND (?2='' OR r.created_at>=?2)
    AND (?3='' OR r.created_at<?3) AND (?4 IS NULL OR t.symbol=?4))";

fn database(error: rusqlite::Error) -> String {
    format!("SYMBOLS_READ_ERROR: {error}")
}

/// Return the final activity-symbol HTTP shape without loading historical payloads.
pub fn query(
    conn: &Connection,
    account_id: i64,
    since: &str,
    until: &str,
    symbol: Option<&str>,
) -> Result<Value, String> {
    let exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(database)?;
    if !exists {
        return Err("ACCOUNT_NOT_FOUND: account does not exist".into());
    }
    let args = params![account_id, since, until, symbol];
    let mut fees: HashMap<String, (Map<String, Value>, i64)> = HashMap::new();
    let mut statement = conn
        .prepare(&format!(
            "{TRADES}
            SELECT symbol,fee_currency,SUM(fee),COUNT(*) FROM trades
            WHERE fee IS NOT NULL AND fee_currency IS NOT NULL AND fee_currency!=''
            GROUP BY symbol,fee_currency"
        ))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let entry = fees.entry(row.get(0).map_err(database)?).or_default();
        entry.0.insert(
            row.get(1).map_err(database)?,
            json!(row.get::<_, f64>(2).map_err(database)?),
        );
        entry.1 += row.get::<_, i64>(3).map_err(database)?;
    }

    let mut fills = Vec::new();
    // Empty symbol still filters, but historically does not include trade details.
    if symbol.is_some_and(|name| !name.is_empty()) {
        let mut statement = conn
            .prepare(&format!(
                "{TRADES}
                SELECT symbol,day,time,estimated,side,quantity,price,reference,reference_source,
                    value,cost,loss_bp,fee,fee_currency,record_id,execution_id
                FROM trades ORDER BY created_at DESC,record_id DESC,trade_index ASC"
            ))
            .map_err(database)?;
        let mut rows = statement.query(args).map_err(database)?;
        while let Some(row) = rows.next().map_err(database)? {
            fills.push(json!({
                "symbol":row.get::<_, String>(0).map_err(database)?,
                "day":row.get::<_, String>(1).map_err(database)?,
                "time":row.get::<_, f64>(2).map_err(database)?,
                "timeEstimated":row.get::<_, bool>(3).map_err(database)?,
                "side":row.get::<_, String>(4).map_err(database)?,
                "quantity":row.get::<_, Option<f64>>(5).map_err(database)?,
                "price":row.get::<_, Option<f64>>(6).map_err(database)?,
                "reference":row.get::<_, Option<f64>>(7).map_err(database)?,
                "referenceSource":row.get::<_, Option<String>>(8).map_err(database)?,
                "value":row.get::<_, Option<f64>>(9).map_err(database)?,
                "cost":row.get::<_, Option<f64>>(10).map_err(database)?,
                "lossBp":row.get::<_, Option<f64>>(11).map_err(database)?,
                "fee":row.get::<_, Option<f64>>(12).map_err(database)?,
                "feeCurrency":row.get::<_, Option<String>>(13).map_err(database)?,
                "record_id":row.get::<_, i64>(14).map_err(database)?,
                "execution_id":row.get::<_, Option<String>>(15).map_err(database)?
            }));
        }
    }

    let mut data = Vec::new();
    let mut statement = conn
        .prepare(&format!(
            "{TRADES},
            ranked AS (SELECT *,row_number() OVER (
                ORDER BY created_at DESC,record_id DESC,trade_index ASC) AS occurrence FROM trades),
            historical AS (SELECT symbol,time,occurrence,value,
                CASE WHEN value IS NOT NULL THEN cost END AS cost,loss_bp,estimated FROM ranked)
            SELECT symbol,MAX(time),{AGGREGATES} FROM historical
            GROUP BY symbol ORDER BY MIN(occurrence)"
        ))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let name: String = row.get(0).map_err(database)?;
        let last_time: f64 = row.get(1).map_err(database)?;
        let summary = Summary::read(row, 2).map_err(database)?;
        let (currencies, covered) = fees.remove(&name).unwrap_or_default();
        let summary = historical_summary(summary.output(currencies, covered));
        data.push(json!({"symbol":name,"last_time":last_time as i64,
            "n_trades":summary["count"],"summary":summary,"trades":std::mem::take(&mut fills)}));
    }
    Ok(json!({"count":data.len(),"data":data}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE account(id INTEGER);
            INSERT INTO account VALUES(1),(2);
            CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT,execution_id TEXT);
            INSERT INTO executerecord VALUES(1,1,'a','old'),(2,1,'b','new'),(3,2,'z','other');
            CREATE TABLE analysis_source_trade(record_id INTEGER,trade_index INTEGER,symbol TEXT,
                day TEXT,time REAL,estimated INTEGER,side TEXT,quantity REAL,price REAL,
                reference REAL,reference_source TEXT,value REAL,cost REAL,loss_bp REAL,fee REAL,fee_currency TEXT);
            INSERT INTO analysis_source_trade VALUES
                (2,0,'Z','day',10.9,1,'buy',1,100,99,'mid',100,2,200,1,'USD'),
                (2,1,'A','day',20,0,'none',NULL,NULL,NULL,NULL,NULL,999,999,2,'BNB'),
                (1,0,'Z','day',30,0,'sell',1,50,NULL,NULL,50,NULL,NULL,-0.5,'USD'),
                (3,0,'OTHER','day',99,0,'buy',1,1,1,'last',1,0,0,1,'USD');").unwrap();
        conn
    }

    #[test]
    fn overview_order_bounds_and_historical_costs() {
        let conn = fixture();
        let result = query(&conn, 1, "", "", None).unwrap();
        assert_eq!(result["count"], 2);
        let z = &result["data"][0];
        assert_eq!(z["symbol"], "Z");
        assert_eq!(z["last_time"], 30);
        assert_eq!(z["summary"]["coverage"], 100.0 / 150.0);
        assert_eq!(z["summary"]["fees"]["USD"], 0.5);
        assert_eq!(z["summary"]["lossBp"], 200.0);
        assert_eq!(z["trades"], json!([]));
        let a = &result["data"][1]["summary"];
        assert!(a["cost"].is_null());
        assert!(a["coverage"].is_null());
        assert_eq!(a["covered"], 0);
        assert_eq!(a["amountComplete"], false);
        assert_eq!(query(&conn, 1, "a", "b", None).unwrap()["count"], 1);
        assert_eq!(query(&conn, 1, "", "a", None).unwrap()["count"], 0);
        assert!(query(&conn, 9, "", "", None)
            .unwrap_err()
            .starts_with("ACCOUNT_NOT_FOUND:"));
    }

    #[test]
    fn selected_trades_keep_order_ids_and_boolean_estimate() {
        let conn = fixture();
        let result = query(&conn, 1, "", "", Some("Z")).unwrap();
        let fills = &result["data"][0]["trades"];
        assert_eq!(fills.as_array().unwrap().len(), 2);
        assert_eq!(fills[0]["record_id"], 2);
        assert_eq!(fills[0]["execution_id"], "new");
        assert_eq!(fills[0]["timeEstimated"], true);
        assert_eq!(fills[1]["record_id"], 1);
        assert_eq!(fills[0].as_object().unwrap().len(), 16);
        assert_eq!(query(&conn, 1, "", "", Some("")).unwrap()["count"], 0);
    }
}
