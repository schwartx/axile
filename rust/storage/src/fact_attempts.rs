//! Frozen attempted quantities from canonical sizing and order collections.

use crate::error::AnalysisError;
use crate::publication::{finite, sql_error, stage_error};
use rusqlite::params;
use rusqlite::{Connection, Statement};

pub(crate) struct AttemptReader<'a> {
    statement: Statement<'a>,
}

impl<'a> AttemptReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self { statement: connection.prepare(
            "SELECT s.map_key,z.current_quantity,z.target_quantity,s.error,c.kind,c.item_count,
             (SELECT COUNT(*) FROM execution_order o WHERE o.record_id=s.record_id AND o.path=s.path || '/orders'),
             (SELECT COUNT(*) FROM execution_order o WHERE o.record_id=s.record_id AND o.path=s.path || '/orders' AND (o.filled_volume IS NULL OR lower(o.direction) NOT IN ('buy','sell') OR o.direction IS NULL)),
             (SELECT COALESCE(SUM(o.filled_volume*CASE lower(o.direction) WHEN 'buy' THEN 1 ELSE -1 END),0) FROM execution_order o WHERE o.record_id=s.record_id AND o.path=s.path || '/orders'),
             s.node_kind,s.status,s.outcome,s.outcome_reason,s.reason_code,s.memory
             FROM execution_symbol_result s JOIN executerecord e ON e.id=s.record_id
             LEFT JOIN execution_symbol_sizing z ON z.record_id=s.record_id AND z.path=s.path || '/sizing'
             LEFT JOIN execution_order_collection c ON c.record_id=s.record_id AND c.path=s.path || '/orders'
             WHERE s.record_id=?1 AND (e.is_success!=1 OR e.display_status='PARTIAL' OR e.task_status='TERMINATED') ORDER BY s.ordinal"
        )? })
    }
    pub(crate) fn write(&mut self, record: i64, target: &Connection) -> Result<(), AnalysisError> {
        let mut rows = self.statement.query([record]).map_err(sql_error)?;
        let mut index = 0;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let before: Option<f64> = row.get(1).map_err(sql_error)?;
            let target_quantity: Option<f64> = row.get(2).map_err(sql_error)?;
            let kind: Option<String> = row.get(4).map_err(sql_error)?;
            let expected: Option<i64> = row.get(5).map_err(sql_error)?;
            let actual: i64 = row.get(6).map_err(sql_error)?;
            if kind.as_deref() == Some("array") && expected != Some(actual) {
                return Err(AnalysisError::new(
                    "SOURCE_FACTS_INCOMPLETE",
                    format!("SOURCE_FACTS_INCOMPLETE: record {record}: incomplete orders"),
                ));
            }
            let invalid: i64 = row.get(7).map_err(sql_error)?;
            let filled = if kind.as_deref() == Some("array") && invalid == 0 {
                Some(row.get::<_, f64>(8).map_err(sql_error)?)
            } else {
                None
            };
            let display = crate::executions::symbol_from_row(row)?;
            target
                .prepare_cached(
                    r#"INSERT INTO native_stage.performance_cost_attempt (
                record_id, idx, symbol, planned, filled, reason
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6
            )"#,
                )
                .map_err(stage_error)?
                .execute(params![
                    record,
                    index,
                    row.get::<_, String>(0).map_err(sql_error)?,
                    finite(
                        target_quantity
                            .zip(before)
                            .map(|(target, before)| target - before)
                    ),
                    finite(filled),
                    display["error"]
                        .as_str()
                        .unwrap_or_default()
                        .chars()
                        .take(200)
                        .collect::<String>()
                ])
                .map_err(stage_error)?;
            index += 1;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    fn output(connection: &Connection) -> Vec<Value> {
        connection.prepare("SELECT json_array(record_id,idx,symbol,planned,filled,reason) FROM native_stage.performance_cost_attempt ORDER BY idx").unwrap().query_map([],|row| Ok(serde_json::from_str(&row.get::<_,String>(0)?).unwrap())).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap()
    }
    #[test]
    fn attempted_unknown_empty_partial_and_failed_orders_stay_distinct() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE executerecord(id INTEGER,is_success INTEGER,status TEXT,task_status TEXT); CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,error TEXT); CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,current_quantity REAL,target_quantity REAL); CREATE TABLE execution_order_collection(record_id INTEGER,path TEXT,kind TEXT,item_count INTEGER); CREATE TABLE execution_order(record_id INTEGER,path TEXT,filled_volume REAL,direction TEXT); INSERT INTO executerecord VALUES(1,0,'FAILED',NULL); INSERT INTO execution_symbol_result VALUES(1,'/symbol_results/A','A',0,NULL); INSERT INTO execution_symbol_sizing VALUES(1,'/symbol_results/A/sizing',2,5);").unwrap();
        connection
            .execute_batch(
                "ALTER TABLE executerecord ADD COLUMN display_status TEXT;
            UPDATE executerecord SET display_status=status;
            ALTER TABLE execution_symbol_result ADD COLUMN node_kind TEXT DEFAULT 'object';",
            )
            .unwrap();
        for field in [
            "status",
            "outcome",
            "outcome_reason",
            "reason_code",
            "memory",
        ] {
            connection
                .execute_batch(&format!(
                    "ALTER TABLE execution_symbol_result ADD COLUMN {field} TEXT"
                ))
                .unwrap();
        }
        connection.execute_batch("ATTACH DATABASE '' AS native_stage; CREATE TABLE native_stage.performance_cost_attempt(\"record_id\" INTEGER,\"idx\" INTEGER,\"symbol\" TEXT,\"planned\" FLOAT,\"filled\" FLOAT,\"reason\" TEXT);").unwrap();
        let mut reader = AttemptReader::new(&connection).unwrap();
        reader.write(1, &connection).unwrap();
        assert_eq!(output(&connection)[0][3], 3.0);
        assert!(output(&connection)[0][4].is_null());
        connection.execute_batch("DELETE FROM native_stage.performance_cost_attempt; INSERT INTO execution_order_collection VALUES(1,'/symbol_results/A/orders','array',0);").unwrap();
        reader.write(1, &connection).unwrap();
        assert_eq!(output(&connection)[0][4], 0.0);
        connection
            .execute("UPDATE execution_order_collection SET item_count=1", [])
            .unwrap();
        assert!(reader
            .write(1, &connection)
            .unwrap_err()
            .error
            .contains("incomplete orders"));
        connection.execute_batch("DELETE FROM native_stage.performance_cost_attempt; INSERT INTO execution_order VALUES(1,'/symbol_results/A/orders',2,'sell');").unwrap();
        reader.write(1, &connection).unwrap();
        assert_eq!(output(&connection)[0][4], -2.0);
        connection.execute_batch("DELETE FROM native_stage.performance_cost_attempt; UPDATE executerecord SET is_success=1,status=NULL,display_status='PARTIAL'; UPDATE execution_symbol_result SET outcome='not_reached',error='RuntimeError: private';").unwrap();
        reader.write(1, &connection).unwrap();
        assert_eq!(output(&connection)[0][5], "执行不到位");
        connection.execute_batch("DELETE FROM native_stage.performance_cost_attempt; UPDATE executerecord SET status='PARTIAL',display_status='SUCCEEDED';").unwrap();
        reader.write(1, &connection).unwrap();
        assert!(output(&connection).is_empty());
    }
}
