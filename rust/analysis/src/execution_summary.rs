//! Execution-sized summaries prepared once from the frozen source facts.
//!
//! This module deliberately never reconstructs positions, symbol evidence or
//! transactions. The result belongs to a snapshot, rather than to a display
//! range, so changing a chart range cannot multiply the stored summaries.

#[cfg(test)]
use rusqlite::Connection;
use serde_json::{json, Value};

#[cfg(test)]
pub(crate) const SUMMARY_COLUMNS: &[&str] = &[
    "record_id",
    "time",
    "value",
    "cost",
    "loss_bp",
    "coverage",
    "covered",
    "count",
    "fee_covered",
    "amount_complete",
    "covered_value",
    "estimated",
    "symbol_count",
    "has_transactions",
];
pub(crate) const FEE_COLUMNS: &[&str] = &["record_id", "currency", "value", "count"];

#[cfg(test)]
fn sql_error(error: rusqlite::Error) -> String {
    format!("ANALYSIS_READ_ERROR:{error}")
}

/// Prepare each execution summary once, independently of chart display ranges.
/// The emitter assigns the snapshot ID and persists bounded frames internally;
/// no per-execution details or serialized historical payloads are produced.
pub(crate) fn values(
    id: i64,
    fills: &[crate::fact_trades::Trade],
    emit: &mut crate::publication::RowEmitter<'_>,
) -> Result<Vec<Value>, String> {
    let mut summary = crate::costs::Summary::empty();
    let mut symbols = std::collections::BTreeSet::new();
    for trade in fills {
        summary.push(trade);
        if trade.quantity.is_some() {
            symbols.insert(&trade.symbol);
        }
    }
    let values = vec![
        json!(summary.value),
        json!(summary.cost),
        json!(summary.loss_bp()),
        json!(summary.coverage()),
        json!(summary.covered),
        json!(summary.count),
        json!(summary.fee_count),
        json!(summary.amount_complete()),
        json!(summary.covered_value),
        json!(summary.estimated),
        json!(!symbols.is_empty()),
    ];
    crate::publication::rows(
        "performance_execution_fee",
        FEE_COLUMNS,
        summary.fees.into_iter().map(|(currency, (value, count))| {
            Ok(vec![json!(id), json!(currency), json!(value), json!(count)])
        }),
        emit,
    )?;
    Ok(values)
}

#[cfg(test)]
fn write(connection:&Connection,account:i64,emit:&mut crate::publication::RowEmitter<'_>)->Result<(),String>{
    let mut ticks = crate::fact_ticks::TickReader::new(connection).map_err(sql_error)?;
    let mut trades = crate::fact_trades::TradeReader::new(connection).map_err(sql_error)?;
    let mut statement = connection
        .prepare("SELECT id,created_at FROM executerecord WHERE account_id=?1 ORDER BY id")
        .map_err(sql_error)?;
    let mut cursor = statement.query([account]).map_err(sql_error)?;
    while let Some(row) = cursor.next().map_err(sql_error)? {
        let id: i64 = row.get(0).map_err(sql_error)?;
        let created_at: String = row.get(1).map_err(sql_error)?;
        let prices = ticks.read(id).map_err(sql_error)?;
        let fills = trades.read(id, &created_at, &prices)?;
        let values = values(id,&fills,emit)?;
        let mut row = vec![json!(id),json!(crate::calendar::timestamp_ms(&created_at)?)];
        row.extend(values[..10].iter().cloned());
        row.push(json!(fills.iter().filter(|t|t.quantity.is_some()).map(|t|&t.symbol).collect::<std::collections::BTreeSet<_>>().len()));
        row.push(values[10].clone());
        emit("performance_execution_summary",SUMMARY_COLUMNS,&[row])?;
    }
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&connection).unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,unit_multiplier REAL);
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,parent_path TEXT,state TEXT);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT,ordinal INTEGER,trade_time TEXT,trade_price REAL,trade_volume REAL,order_id TEXT,extra TEXT,fee_currency TEXT);
             CREATE TABLE execution_order(record_id INTEGER,path TEXT,order_id TEXT,ordinal INTEGER,direction TEXT);
             INSERT INTO executerecord VALUES(1,2,'2026-01-01T09:00:00'),(2,2,'2026-01-02T09:00:00'),(3,9,'2026-01-03T09:00:00');
             INSERT INTO execution_symbol_result VALUES(1,'/symbol_results/A','A',0);
             INSERT INTO execution_symbol_tick VALUES(1,'/symbol_results/A/first_tick','A',0,100,100,100,1);
             INSERT INTO execution_symbol_sizing VALUES(1,'/symbol_results/A/sizing',1);
             INSERT INTO business_trade_collection VALUES(1,'/symbol_results/A/trades','/symbol_results/A','array');
             INSERT INTO business_trade VALUES
               (1,'/symbol_results/A/trades',0,'invalid',102,1,'1','{\"direction\":\"buy\",\"commission\":0}','CNY'),
               (1,'/symbol_results/A/trades',1,'2026-01-01T09:00:00',NULL,1,'2','{\"commission\":-0.2}','USD');"
        ).unwrap();
        connection
    }

    #[test]
    fn direct_summaries_preserve_unknown_amounts_fees_and_empty_executions() {
        let connection = fixture();
        let mut summaries = Vec::new();
        let mut fees = Vec::new();
        write(&connection, 2, &mut |table, _, rows| {
            if table == "performance_execution_summary" {
                summaries.extend_from_slice(rows);
            } else {
                fees.extend_from_slice(rows);
            }
            Ok(())
        })
        .unwrap();
        assert_eq!(summaries.len(), 2);
        assert_eq!(
            &summaries[0][2..],
            &[
                json!(102.0),
                json!(2.0),
                json!(200.0),
                Value::Null,
                json!(1),
                json!(2),
                json!(2),
                json!(false),
                json!(102.0),
                json!(1),
                json!(1),
                json!(true)
            ]
        );
        assert_eq!(
            &summaries[1][2..10],
            &[
                Value::Null,
                Value::Null,
                Value::Null,
                Value::Null,
                json!(0),
                json!(0),
                json!(0),
                json!(true),
            ]
        );
        assert_eq!(summaries[1][10].as_f64(), Some(0.0));
        assert_eq!(&summaries[1][11..], &[json!(0), json!(0), json!(false)]);
        assert_eq!(
            fees,
            vec![
                vec![json!(1), json!("CNY"), json!(0.0), json!(1)],
                vec![json!(1), json!("USD"), json!(-0.2), json!(1)]
            ]
        );
    }

    #[test]
    fn errors_and_empty_accounts_do_not_emit_phantom_summaries() {
        let connection = fixture();
        assert_eq!(
            write(&connection, 2, &mut |_, _, _| Err("sink failed".into())).unwrap_err(),
            "sink failed"
        );
        write(&connection, 99, &mut |_, _, _| {
            panic!("empty account emitted rows")
        })
        .unwrap();
        connection
            .execute("UPDATE executerecord SET created_at='invalid'", [])
            .unwrap();
        assert!(write(&connection, 2, &mut |_, _, _| panic!(
            "invalid timestamp emitted rows"
        ))
        .is_err());
    }
}
