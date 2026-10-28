//! Compute execution totals and persist fees using the caller's staging transaction.

use crate::cost_summary::Summary;
use crate::error::AnalysisError;
use crate::publication::{finite, stage_error};
use rusqlite::{params, Connection};

pub(crate) fn write_fees(
    target: &Connection,
    id: i64,
    fills: &[crate::fact_trades::Trade],
) -> Result<Summary, AnalysisError> {
    let mut summary = Summary::empty();
    for trade in fills {
        summary.push(trade);
    }
    let mut insert = target.prepare_cached(
        "INSERT INTO native_stage.performance_execution_fee(record_id,currency,value,count) VALUES(?1,?2,?3,?4)"
    ).map_err(stage_error)?;
    for (currency, (value, count)) in &summary.fees {
        insert
            .execute(params![id, currency, finite(Some(*value)), count])
            .map_err(stage_error)?;
    }
    Ok(summary)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub(crate) fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&connection).unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT,result_staged INTEGER DEFAULT 0);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,unit_multiplier REAL);
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,parent_path TEXT,state TEXT);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT,ordinal INTEGER,trade_time TEXT,trade_price REAL,trade_volume REAL,order_id TEXT,extra TEXT,fee_currency TEXT);
             CREATE TABLE execution_order(record_id INTEGER,path TEXT,order_id TEXT,ordinal INTEGER,direction TEXT);
             INSERT INTO executerecord(id,account_id,created_at) VALUES(1,2,'2026-01-01T09:00:00'),(2,2,'2026-01-02T09:00:00'),(3,9,'2026-01-03T09:00:00');
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
    fn fees_and_unknown_amounts_are_written_without_json_rows() {
        let source = fixture();
        let target = Connection::open_in_memory().unwrap();
        target.execute_batch("ATTACH DATABASE '' AS native_stage; CREATE TABLE native_stage.performance_execution_fee(record_id INTEGER,currency TEXT,value REAL,count INTEGER);").unwrap();
        let mut ticks = crate::fact_ticks::TickReader::new(&source).unwrap();
        let mut trades = crate::fact_trades::TradeReader::new(&source).unwrap();
        let fills = trades
            .read(1, "2026-01-01T09:00:00", &ticks.read(1).unwrap())
            .unwrap();
        let summary = write_fees(&target, 1, &fills).unwrap();
        assert_eq!(summary.value, Some(102.0));
        assert_eq!(summary.cost, Some(2.0));
        assert_eq!(summary.loss_bp(), Some(200.0));
        assert_eq!(summary.coverage(), None);
        assert_eq!(summary.count, 2);
        assert_eq!(summary.covered, 1);
        assert!(!summary.amount_complete());
        let fees = target.prepare("SELECT currency,value,count FROM native_stage.performance_execution_fee ORDER BY currency").unwrap().query_map([],|row| Ok((row.get::<_,String>(0)?,row.get::<_,f64>(1)?,row.get::<_,i64>(2)?))).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
        assert_eq!(fees, vec![("CNY".into(), 0.0, 1), ("USD".into(), -0.2, 1)]);
        let empty = write_fees(&target, 2, &[]).unwrap();
        assert_eq!(empty.value, None);
        assert!(empty.amount_complete());
        target
            .execute_batch("DROP TABLE native_stage.performance_execution_fee;")
            .unwrap();
        let failure = write_fees(&target, 1, &fills).err().unwrap();
        assert_eq!(failure.code, "PUBLICATION_STAGE");
        assert!(std::error::Error::source(&failure).is_some());
    }
}
