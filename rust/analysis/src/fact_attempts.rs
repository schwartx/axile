//! Frozen attempted quantities from canonical sizing and order collections.

use rusqlite::{Connection, Statement};
use serde_json::json;

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
    pub(crate) fn emit(
        &mut self,
        record: i64,
        emit: &mut crate::publication::RowEmitter<'_>,
    ) -> Result<(), String> {
        let mut rows = self
            .statement
            .query([record])
            .map_err(|error| error.to_string())?;
        let mut index = 0;
        while let Some(row) = rows.next().map_err(|error| error.to_string())? {
            let before: Option<f64> = row.get(1).map_err(|error| error.to_string())?;
            let target: Option<f64> = row.get(2).map_err(|error| error.to_string())?;
            let kind: Option<String> = row.get(4).map_err(|error| error.to_string())?;
            let expected: Option<i64> = row.get(5).map_err(|error| error.to_string())?;
            let actual: i64 = row.get(6).map_err(|error| error.to_string())?;
            if kind.as_deref() == Some("array") && expected != Some(actual) {
                return Err(format!(
                    "SOURCE_FACTS_INCOMPLETE: record {record}: incomplete orders"
                ));
            }
            let invalid: i64 = row.get(7).map_err(|error| error.to_string())?;
            let filled = if kind.as_deref() == Some("array") && invalid == 0 {
                Some(row.get::<_, f64>(8).map_err(|error| error.to_string())?)
            } else {
                None
            };
            let display = crate::executions::symbol_from_row(row).map_err(|error| error.to_string())?;
            crate::publication::rows(
                "performance_cost_attempt",
                &["record_id", "idx", "symbol", "planned", "filled", "reason"],
                [Ok(vec![
                    json!(record),
                    json!(index),
                    json!(row.get::<_, String>(0).map_err(|error| error.to_string())?),
                    json!(target.zip(before).map(|(target, before)| target - before)),
                    json!(filled),
                    json!(display["error"].as_str()
                        .unwrap_or_default()
                        .chars()
                        .take(200)
                        .collect::<String>()),
                ])],
                emit,
            )?;
            index += 1;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn absent_empty_and_incomplete_orders_are_distinct() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE executerecord(id INTEGER,is_success INTEGER,status TEXT,task_status TEXT); CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,error TEXT); CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,current_quantity REAL,target_quantity REAL); CREATE TABLE execution_order_collection(record_id INTEGER,path TEXT,kind TEXT,item_count INTEGER); CREATE TABLE execution_order(record_id INTEGER,path TEXT,filled_volume REAL,direction TEXT); INSERT INTO executerecord VALUES(1,0,'FAILED',NULL); INSERT INTO execution_symbol_result VALUES(1,'/symbol_results/A','A',0,NULL); INSERT INTO execution_symbol_sizing VALUES(1,'/symbol_results/A/sizing',2,5);").unwrap();
        connection.execute_batch("ALTER TABLE executerecord ADD COLUMN display_status TEXT;
            UPDATE executerecord SET display_status=status;
            ALTER TABLE execution_symbol_result ADD COLUMN node_kind TEXT DEFAULT 'object';").unwrap();
        for field in ["status", "outcome", "outcome_reason", "reason_code", "memory"] {
            connection.execute_batch(&format!("ALTER TABLE execution_symbol_result ADD COLUMN {field} TEXT")).unwrap();
        }
        let mut reader = AttemptReader::new(&connection).unwrap();
        let mut output = Vec::new();
        reader
            .emit(1, &mut |_, _, rows| {
                output.extend_from_slice(rows);
                Ok(())
            })
            .unwrap();
        assert_eq!(output[0][3], 3.0);
        assert!(output[0][4].is_null());
        connection.execute("INSERT INTO execution_order_collection VALUES(1,'/symbol_results/A/orders','array',0)",[]).unwrap();
        output.clear();
        reader
            .emit(1, &mut |_, _, rows| {
                output.extend_from_slice(rows);
                Ok(())
            })
            .unwrap();
        assert_eq!(output[0][4], 0.0);
        connection
            .execute("UPDATE execution_order_collection SET item_count=1", [])
            .unwrap();
        assert!(reader
            .emit(1, &mut |_, _, _| Ok(()))
            .unwrap_err()
            .contains("incomplete orders"));
        connection
            .execute(
                "INSERT INTO execution_order VALUES(1,'/symbol_results/A/orders',2,'sell')",
                [],
            )
            .unwrap();
        output.clear();
        reader
            .emit(1, &mut |_, _, rows| {
                output.extend_from_slice(rows);
                Ok(())
            })
            .unwrap();
        assert_eq!(output[0][4], -2.0);
        connection.execute_batch("UPDATE executerecord SET is_success=1,status=NULL,display_status='PARTIAL';
            UPDATE execution_symbol_result SET outcome='not_reached',error='RuntimeError: private';").unwrap();
        output.clear();
        reader.emit(1, &mut |_, _, rows| { output.extend_from_slice(rows); Ok(()) }).unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0][5], "执行不到位");
        connection.execute_batch("UPDATE executerecord SET status='PARTIAL',display_status='SUCCEEDED';").unwrap();
        output.clear();
        reader.emit(1, &mut |_, _, rows| { output.extend_from_slice(rows); Ok(()) }).unwrap();
        assert!(output.is_empty());

    }
}
