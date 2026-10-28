//! Frozen attempted quantities from canonical sizing and order collections.

use crate::analysis::sql as sql_error;
use crate::error::AnalysisError;
fn finite(v: Option<f64>) -> Option<f64> {
    v.filter(|v| v.is_finite())
}
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
    pub(crate) fn read(&mut self, record: i64) -> Result<Vec<serde_json::Value>, AnalysisError> {
        let mut rows = self.statement.query([record]).map_err(sql_error)?;
        let mut output = Vec::new();
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
            output.push(serde_json::json!({"symbol":row.get::<_,String>(0).map_err(sql_error)?,"planned":finite(target_quantity.zip(before).map(|(t,b)|t-b)),"filled":finite(filled),"reason":display.get("error").and_then(serde_json::Value::as_str).unwrap_or_default().chars().take(200).collect::<String>()}));
        }
        Ok(output)
    }
}
