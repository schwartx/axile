//! Live activity symbols from scalar trade facts, in the caller's read transaction.

use std::collections::HashMap;

use rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::cost_summary::Summary;
use crate::journal::historical_summary;

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
    let since = (!since.is_empty()).then(|| crate::calendar::canonical_time(since)).transpose()?;
    let until = (!until.is_empty()).then(|| crate::calendar::canonical_time(until)).transpose()?;
    let mut groups: Vec<(String, f64, Summary, Vec<Value>)> = Vec::new();
    let mut indices = HashMap::new();
    let mut ticks = crate::fact_ticks::TickReader::new(conn).map_err(database)?;
    let mut trades = crate::fact_trades::TradeReader::new(conn).map_err(database)?;
    let mut statement = conn
        .prepare(
            "SELECT id,created_at,execution_id,facts_complete FROM executerecord
        WHERE account_id=?1 AND (?2 IS NULL OR axile_time(created_at)>=?2) AND (?3 IS NULL OR axile_time(created_at)<?3)
        ORDER BY axile_time(created_at) DESC,id DESC",
        )
        .map_err(database)?;
    let mut rows = statement
        .query(params![account_id, since, until])
        .map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let id: i64 = row.get(0).map_err(database)?;
        let created: String = row.get(1).map_err(database)?;
        let execution: Option<String> = row.get(2).map_err(database)?;
        if !row.get::<_, bool>(3).map_err(database)? {
            return Err(format!("SOURCE_FACTS_INCOMPLETE: record {id}"));
        }
        let prices = ticks.read(id).map_err(database)?;
        for trade in trades.read(id, &created, &prices)? {
            if symbol.is_some_and(|name| name != trade.symbol) {
                continue;
            }
            let index = *indices.entry(trade.symbol.clone()).or_insert_with(|| {
                groups.push((
                    trade.symbol.clone(),
                    trade.time,
                    Summary::empty(),
                    Vec::new(),
                ));
                groups.len() - 1
            });
            let (_, last, summary, fills) = &mut groups[index];
            *last = last.max(trade.time);
            summary.push(&trade);
            if symbol.is_some_and(|name| !name.is_empty()) {
                fills.push(json!({"symbol":trade.symbol,"day":trade.day,"time":trade.time,
                    "timeEstimated":trade.estimated,"side":trade.side,"quantity":trade.quantity,
                    "price":trade.price,"reference":trade.reference,"referenceSource":trade.reference_source,
                    "value":trade.value,"cost":trade.cost,"lossBp":trade.loss_bp,"fee":trade.fee,
                    "feeCurrency":trade.fee_currency,"record_id":id,"execution_id":execution}));
            }
        }
    }
    let data: Vec<_> = groups
        .into_iter()
        .map(|(name, last, summary, fills)| {
            let summary = historical_summary(summary.output());
            json!({"symbol":name,"last_time":last as i64,"n_trades":summary["count"],
            "summary":summary,"trades":fills})
        })
        .collect();
    Ok(json!({"count":data.len(),"data":data}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn activity_window_includes_utc_executions_on_the_shanghai_day() {
        let conn = crate::execution_summary::tests::fixture();
        conn.execute_batch("CREATE TABLE account(id INTEGER); INSERT INTO account VALUES(2);
            ALTER TABLE executerecord ADD COLUMN execution_id TEXT;
            ALTER TABLE executerecord ADD COLUMN facts_complete INTEGER DEFAULT 1;
            UPDATE executerecord SET created_at='2026-01-31T16:30:00Z' WHERE id=1;
            UPDATE business_trade SET trade_time=NULL;").unwrap();
        let result = query(&conn, 2, "2026-02-01T00:00:00+08:00", "2026-02-02T00:00:00+08:00", Some("A")).unwrap();
        assert_eq!(result["count"], 1);
        assert_eq!(result["data"][0]["n_trades"], 2);
        assert_eq!(result["data"][0]["trades"][0]["day"], "2026-02-01");
    }

    #[test]
    fn canonical_symbols_preserve_unknown_fees_order_and_bounds() {
        let conn = crate::execution_summary::tests::fixture();
        conn.execute_batch(
            "CREATE TABLE account(id INTEGER); INSERT INTO account VALUES(2),(9);
            ALTER TABLE executerecord ADD COLUMN execution_id TEXT;
            ALTER TABLE executerecord ADD COLUMN facts_complete INTEGER DEFAULT 1;",
        )
        .unwrap();
        let result = query(&conn, 2, "", "", Some("A")).unwrap();
        let item = &result["data"][0];
        assert_eq!(item["summary"]["count"], 2);
        assert_eq!(item["summary"]["amountComplete"], false);
        assert_eq!(item["summary"]["fees"]["USD"], -0.2);
        assert_eq!(item["trades"][0]["record_id"], 1);
        assert_eq!(item["trades"][0]["timeEstimated"], true);
        assert_eq!(item["trades"][1]["price"], Value::Null);
        assert_eq!(query(&conn, 2, "2026-01-02", "", None).unwrap()["count"], 0);
        assert_eq!(query(&conn, 2, "", "", Some("")).unwrap()["count"], 0);
        assert!(query(&conn, 1, "", "", None)
            .unwrap_err()
            .starts_with("ACCOUNT_NOT_FOUND"));
        conn.execute_batch("UPDATE executerecord SET facts_complete=0 WHERE id=1")
            .unwrap();
        assert!(query(&conn, 2, "", "", None)
            .unwrap_err()
            .starts_with("SOURCE_FACTS_INCOMPLETE"));
    }
}
