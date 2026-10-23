//! Execution-sized summaries prepared once from the frozen source facts.
//!
//! This module deliberately never reconstructs positions, symbol evidence or
//! transactions. The result belongs to a snapshot, rather than to a display
//! range, so changing a chart range cannot multiply the stored summaries.

use rusqlite::{Connection, Row};
use serde_json::{json, Value};

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

/// Aggregate only the account's trades, retaining the historical chart rules.
/// A cost without a known amount is not covered execution cost; missing amounts
/// also make coverage unknown. Fee currencies remain separate, including zero
/// fees and negative rebates.
const AGGREGATE_SQL: &str = "
    SELECT e.record_id, COUNT(t.trade_index), COUNT(t.value), SUM(t.value),
           SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.cost END),
           COUNT(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN 1 END),
           SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.value ELSE 0 END),
           SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.value*t.loss_bp ELSE 0 END),
           COALESCE(SUM(t.estimated),0),
           COUNT(CASE WHEN t.fee IS NOT NULL AND t.fee_currency IS NOT NULL
                           AND t.fee_currency!='' THEN 1 END),
           e.time,
           CASE WHEN e.symbol_count IS NULL THEN NULL
                ELSE COUNT(DISTINCT CASE WHEN t.quantity IS NOT NULL THEN t.symbol END) END,
           EXISTS(SELECT 1 FROM analysis_source_transaction x WHERE x.record_id=e.record_id)
    FROM analysis_source_execution e
    LEFT JOIN analysis_source_trade t ON t.record_id=e.record_id
    WHERE e.account_id=?1
    GROUP BY e.record_id";

const FEE_SQL: &str = "
    SELECT t.record_id,t.fee_currency,SUM(t.fee),COUNT(*)
    FROM analysis_source_execution e
    JOIN analysis_source_trade t ON t.record_id=e.record_id
    WHERE e.account_id=?1 AND t.fee IS NOT NULL
      AND t.fee_currency IS NOT NULL AND t.fee_currency!=''
    GROUP BY t.record_id,t.fee_currency
    ORDER BY t.record_id,t.fee_currency";

fn sql_error(error: rusqlite::Error) -> String {
    format!("ANALYSIS_READ_ERROR:{error}")
}

#[derive(Debug, PartialEq)]
struct Summary {
    record_id: i64,
    value: Option<f64>,
    cost: Option<f64>,
    loss_bp: Option<f64>,
    coverage: Option<f64>,
    covered: i64,
    count: i64,
    amount_complete: bool,
    covered_value: f64,
    estimated: i64,
    fee_covered: i64,
}

impl Summary {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        let count = row.get(1)?;
        let valued: i64 = row.get(2)?;
        let value: Option<f64> = row.get(3)?;
        let covered_value: f64 = row.get(6)?;
        let weighted: Option<f64> = row.get(7)?;
        let amount_complete = valued == count;
        Ok(Self {
            record_id: row.get(0)?,
            value,
            cost: row.get(4)?,
            loss_bp: weighted
                .filter(|_| covered_value > 0.0)
                .map(|weighted| weighted / covered_value),
            coverage: value
                .filter(|value| amount_complete && *value > 0.0)
                .map(|value| covered_value / value),
            covered: row.get(5)?,
            count,
            amount_complete,
            covered_value,
            estimated: row.get(8)?,
            fee_covered: row.get(9)?,
        })
    }

    fn values(row: &Row<'_>) -> rusqlite::Result<Vec<Value>> {
        let summary = Self::from_row(row)?;
        Ok(vec![
            json!(summary.record_id),
            json!(row.get::<_, f64>(10)?),
            json!(summary.value),
            json!(summary.cost),
            json!(summary.loss_bp),
            json!(summary.coverage),
            json!(summary.covered),
            json!(summary.count),
            json!(summary.fee_covered),
            json!(summary.amount_complete),
            json!(summary.covered_value),
            json!(summary.estimated),
            json!(row.get::<_, Option<i64>>(11)?),
            json!(row.get::<_, bool>(12)?),
        ])
    }
}

/// Prepare each execution summary once, independently of chart display ranges.
/// The emitter assigns the snapshot ID and persists bounded frames internally;
/// no per-execution details or serialized historical payloads are produced.
pub(crate) fn write(
    connection: &Connection,
    account: i64,
    emit: &mut crate::publication::RowEmitter<'_>,
) -> Result<(), String> {
    let mut statement = connection.prepare(AGGREGATE_SQL).map_err(sql_error)?;
    let values = statement
        .query_map([account], Summary::values)
        .map_err(sql_error)?
        .map(|row| row.map_err(sql_error));
    crate::publication::rows(
        "performance_execution_summary",
        SUMMARY_COLUMNS,
        values,
        emit,
    )?;
    drop(statement);

    let mut statement = connection.prepare(FEE_SQL).map_err(sql_error)?;
    let values = statement
        .query_map([account], |row| {
            Ok(vec![
                json!(row.get::<_, i64>(0)?),
                json!(row.get::<_, String>(1)?),
                json!(row.get::<_, f64>(2)?),
                json!(row.get::<_, i64>(3)?),
            ])
        })
        .map_err(sql_error)?
        .map(|row| row.map_err(sql_error));
    crate::publication::rows("performance_execution_fee", FEE_COLUMNS, values, emit)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE analysis_source_execution(record_id INTEGER PRIMARY KEY,account_id INTEGER,
                     time REAL NOT NULL,symbol_count INTEGER);
                 CREATE TABLE analysis_source_trade(record_id INTEGER,trade_index INTEGER,value REAL,
                     cost REAL,loss_bp REAL,estimated INTEGER,fee REAL,fee_currency TEXT,
                     symbol TEXT,quantity REAL,
                     PRIMARY KEY(record_id,trade_index));
                 CREATE TABLE analysis_source_transaction(record_id INTEGER,idx INTEGER,
                     PRIMARY KEY(record_id,idx));
                 INSERT INTO analysis_source_execution VALUES(1,2,100.25,2),(2,2,200,NULL),
                     (3,2,300,0),(4,9,400,1);
                 INSERT INTO analysis_source_transaction VALUES(1,0);
                 INSERT INTO analysis_source_trade VALUES
                    (1,0,100,2,200,0,0,'CNY','A',1),
                    (1,1,NULL,7,NULL,1,-0.2,'USD','A',1),
                    (1,2,NULL,NULL,NULL,1,0,'USD','B',NULL),
                    (1,3,50,NULL,NULL,0,9,'','B',NULL),
                    (3,0,0,0,500,0,NULL,NULL,'C',0),
                    (4,0,900,9,100,0,8,'CNY','D',1);",
            )
            .unwrap();
        connection
    }

    fn summaries(connection: &Connection) -> Vec<Summary> {
        connection
            .prepare(AGGREGATE_SQL)
            .unwrap()
            .query_map([2], Summary::from_row)
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap()
    }

    #[test]
    fn incomplete_amount_does_not_turn_unknown_cost_into_coverage() {
        let values = summaries(&fixture());
        assert_eq!(values.len(), 3);
        assert_eq!(
            values[0],
            Summary {
                record_id: 1,
                value: Some(150.0),
                cost: Some(2.0),
                loss_bp: Some(200.0),
                coverage: None,
                covered: 1,
                count: 4,
                amount_complete: false,
                covered_value: 100.0,
                estimated: 2,
                fee_covered: 3,
            }
        );
    }

    #[test]
    fn an_execution_without_fills_keeps_known_empty_and_unknown_amounts_distinct() {
        let values = summaries(&fixture());
        assert_eq!(
            values[1],
            Summary {
                record_id: 2,
                value: None,
                cost: None,
                loss_bp: None,
                coverage: None,
                covered: 0,
                count: 0,
                amount_complete: true,
                covered_value: 0.0,
                estimated: 0,
                fee_covered: 0,
            }
        );
    }

    #[test]
    fn actual_zero_amount_and_cost_remain_zero_without_a_coverage_ratio() {
        let values = summaries(&fixture());
        assert_eq!(values[2].value, Some(0.0));
        assert_eq!(values[2].cost, Some(0.0));
        assert_eq!(values[2].covered, 1);
        assert!(values[2].amount_complete);
        assert_eq!(values[2].loss_bp, None);
        assert_eq!(values[2].coverage, None);
    }

    #[test]
    fn complete_amounts_use_only_covered_value_for_weighted_loss() {
        let connection = fixture();
        connection
            .execute("DELETE FROM analysis_source_trade WHERE record_id=1", [])
            .unwrap();
        connection
            .execute_batch(
                "INSERT INTO analysis_source_trade VALUES
                (1,0,100,0.1,10,0,NULL,NULL,'A',1),
                (1,1,300,0.9,30,0,NULL,NULL,'B',1),
                (1,2,200,NULL,NULL,0,NULL,NULL,'C',1);",
            )
            .unwrap();
        let values = summaries(&connection);
        assert_eq!(values[0].loss_bp, Some(25.0));
        assert_eq!(values[0].coverage, Some(400.0 / 600.0));
        assert_eq!(values[0].covered_value, 400.0);
        assert_eq!(values[0].cost, Some(1.0));
        assert_eq!(values[0].covered, 2);
        assert!(values[0].amount_complete);
    }

    #[test]
    fn emitted_rows_are_flat_version_independent_and_preserve_unknown_symbols() {
        let connection = fixture();
        let mut frames = Vec::new();
        write(&connection, 2, &mut |table, columns, rows| {
            frames.push(json!({"type":"rows", "table":table, "columns":columns, "rows":rows}));
            Ok(())
        })
        .unwrap();
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0]["table"], "performance_execution_summary");
        assert_eq!(frames[0]["columns"], json!(SUMMARY_COLUMNS));
        assert_eq!(
            frames[0]["rows"][0],
            json!([1, 100.25, 150.0, 2.0, 200.0, null, 1, 4, 3, false, 100.0, 2, 1, true])
        );
        assert_eq!(
            frames[0]["rows"][1],
            json!([2, 200.0, null, null, null, null, 0, 0, 0, true, 0.0, 0, null, false])
        );
        assert_eq!(frames[0]["rows"][2][12], 1);
        assert_eq!(frames[0]["rows"][2][13], false);
    }

    #[test]
    fn fee_rows_preserve_separate_currencies_zero_totals_and_rebates() {
        let connection = fixture();
        connection
            .execute_batch(
                "INSERT INTO analysis_source_trade VALUES
                (1,4,NULL,NULL,NULL,0,0.2,'USD','A',NULL),
                (1,5,NULL,NULL,NULL,0,-3,'BTC','A',NULL),
                (1,6,NULL,NULL,NULL,0,4,NULL,'A',NULL);",
            )
            .unwrap();
        let mut frames = Vec::new();
        write(&connection, 2, &mut |table, columns, rows| {
            frames.push(json!({"type":"rows", "table":table, "columns":columns, "rows":rows}));
            Ok(())
        })
        .unwrap();
        assert_eq!(frames[1]["table"], "performance_execution_fee");
        assert_eq!(frames[1]["columns"], json!(FEE_COLUMNS));
        assert_eq!(
            frames[1]["rows"],
            json!([[1, "BTC", -3.0, 1], [1, "CNY", 0.0, 1], [1, "USD", 0.0, 3]])
        );
        let covered = frames[1]["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row[3].as_i64().unwrap())
            .sum::<i64>();
        assert_eq!(frames[0]["rows"][0][8], covered);
    }

    #[test]
    fn index_flags_use_their_facts_and_keep_legacy_unknown_symbol_counts() {
        let connection = fixture();
        connection
            .execute_batch(
                "UPDATE analysis_source_execution SET symbol_count=NULL WHERE record_id=1;
                 INSERT INTO analysis_source_transaction VALUES(2,0);",
            )
            .unwrap();
        let mut frames = Vec::new();
        write(&connection, 2, &mut |table, columns, rows| {
            frames.push(json!({"type":"rows", "table":table, "columns":columns, "rows":rows}));
            Ok(())
        })
        .unwrap();
        // Existing fills do not manufacture a formerly unknown symbol count.
        assert!(frames[0]["rows"][0][12].is_null());
        // Transaction presence is independent of whether individual fills exist.
        assert_eq!(frames[0]["rows"][1][7], 0);
        assert_eq!(frames[0]["rows"][1][13], true);
        assert_eq!(frames[0]["rows"][2][7], 1);
        assert_eq!(frames[0]["rows"][2][13], false);
    }

    #[test]
    fn invalid_row_types_and_emitter_errors_are_propagated() {
        let connection = fixture();
        let error = write(&connection, 2, &mut |_, _, _| Err("sink failed".into())).unwrap_err();
        assert_eq!(error, "sink failed");
        connection
            .execute("UPDATE analysis_source_execution SET time='invalid'", [])
            .unwrap();
        let mut emitted = false;
        let error = write(&connection, 2, &mut |_, _, _| {
            emitted = true;
            Ok(())
        })
        .unwrap_err();
        assert!(error.starts_with("ANALYSIS_READ_ERROR:"), "{error}");
        assert!(!emitted);
    }

    #[test]
    fn an_account_without_executions_emits_no_phantom_rows() {
        let connection = fixture();
        let mut count = 0;
        write(&connection, 99, &mut |_, _, _| {
            count += 1;
            Ok(())
        })
        .unwrap();
        assert_eq!(count, 0);
    }
}
