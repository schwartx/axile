//! Final cost headers from canonical scalar business evidence.

use crate::fact_trades::Trade;
use rusqlite::{Connection, Statement};
use serde_json::{json, Value};

const COLUMNS: &[&str] = &[
    "record_id",
    "time",
    "success",
    "noop",
    "key",
    "execution_id",
    "created_at",
    "is_success",
    "duration_sec",
    "trade_count",
    "symbol_count",
    "position_count",
    "positions_present",
    "reason",
    "raw_present",
    "has_symbol_results",
    "raw_status",
    "raw_task_status",
    "raw_error",
    "raw_outcome",
    "raw_outcome_reason",
    "raw_reason_code",
    "raw_msg",
    "raw_execution_kind",
    "payload",
];

pub(crate) struct HeaderReader<'a> {
    statement: Statement<'a>,
    symbols: Statement<'a>,
}

impl<'a> HeaderReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self {
            statement:connection.prepare("SELECT e.execution_id,e.created_at,e.is_success,e.execution_time,e.display_status,e.task_status,e.display_error,e.display_outcome,e.display_outcome_reason,e.reason_code,e.msg,e.execution_kind,
             EXISTS(SELECT 1 FROM execution_symbol_collection c WHERE c.record_id=e.id AND c.path='/symbol_results' AND c.node_kind='object'),
             EXISTS(SELECT 1 FROM execution_order o WHERE o.record_id=e.id AND o.path LIKE '/symbol_results/%' AND o.filled_volume>0),
             EXISTS(SELECT 1 FROM execution_symbol_result s WHERE s.record_id=e.id),
             EXISTS(SELECT 1 FROM execution_symbol_result s WHERE s.record_id=e.id AND s.status IS NOT 'NOOP')
             FROM executerecord e WHERE e.id=?1")?,
            symbols:connection.prepare("SELECT map_key,status,error,outcome,outcome_reason,reason_code, CASE WHEN json_type(memory,'$.message')='text' THEN json_extract(memory,'$.message') END, CASE WHEN json_type(memory,'$.symbol_decision_reason_code')='text' THEN json_extract(memory,'$.symbol_decision_reason_code') END FROM execution_symbol_result WHERE record_id=?1 ORDER BY ordinal")?,
        })
    }
    pub(crate) fn emit(
        &mut self,
        record: i64,
        fills: &[Trade],
        positions: Option<usize>,
        emit: &mut crate::publication::RowEmitter<'_>,
    ) -> Result<(), String> {
        let row = self
            .statement
            .query_row([record], |row| {
                let created: String = row.get(1)?;
                let success: i64 = row.get(2)?;
                let status: Option<String> = row.get(4)?;
                let duration: Option<f64> = row.get(3)?;
                let has_fills: bool = row.get(13)?;
                let has_symbols: bool = row.get(14)?;
                let non_noop: bool = row.get(15)?;
                let raw = (4..12)
                    .map(|index| row.get::<_, Option<String>>(index))
                    .collect::<rusqlite::Result<Vec<_>>>()?;
                Ok((
                    created,
                    success,
                    status,
                    duration,
                    has_fills,
                    has_symbols,
                    non_noop,
                    raw,
                    row.get::<_, bool>(12)?,
                    row.get::<_, Option<String>>(0)?,
                ))
            })
            .map_err(|error| error.to_string())?;
        let (
            created,
            success,
            status,
            duration,
            has_fills,
            has_symbols,
            non_noop,
            raw,
            has_symbol_results,
            execution,
        ) = row;
        let time = crate::calendar::local_time(&created)?
            .and_utc()
            .timestamp_micros()
            - 28_800_000_000;
        let noop = success == 1
            && fills.is_empty()
            && !has_fills
            && (status.as_deref() == Some("NOOP") || has_symbols && !non_noop);
        let count = fills
            .iter()
            .filter(|trade| trade.quantity.is_some())
            .map(|trade| trade.symbol.as_str())
            .collect::<std::collections::BTreeSet<_>>()
            .len();
        let mut present = 0_i64;
        for (index, value) in raw.iter().enumerate() {
            if value.is_some() || index == 3 {
                present |= 1 << index;
            }
        }
        let mut values = vec![
            json!(record),
            json!(time as f64 / 1_000_000.0 * 1000.0),
            json!(success == 1),
            json!(noop),
            json!(record.to_string()),
            json!(execution),
            json!(created),
            json!(success),
            json!(duration.filter(|value| value.is_finite() && *value >= 0.0)),
            json!(fills.len()),
            json!(count),
            json!(positions),
            json!(positions.is_some()),
            json!(raw[2]
                .as_deref()
                .unwrap_or_default()
                .chars()
                .take(300)
                .collect::<String>()),
            json!(present),
            json!(has_symbol_results),
        ];
        values.extend(raw.into_iter().map(|value| json!(value)));
        values.push(json!("{}"));
        values.extend(crate::execution_summary::values(record, fills, emit)?);
        let columns: Vec<_> = COLUMNS.iter().copied().chain([
            "value","cost","loss_bp","coverage","covered","count","fee_covered",
            "amount_complete","covered_value","estimated","has_transactions"
        ]).collect();
        crate::publication::rows("performance_cost_execution", &columns, [Ok(values)], emit)?;
        let mut rows = self
            .symbols
            .query([record])
            .map_err(|error| error.to_string())?;
        while let Some(row) = rows.next().map_err(|error| error.to_string())? {
            let mut present = 0_i64;
            let mut values = vec![
                json!(record),
                json!(row.get::<_, String>(0).map_err(|error| error.to_string())?),
                Value::Null,
            ];
            for index in 1..=5 {
                let value: Option<String> = row.get(index).map_err(|error| error.to_string())?;
                if value.is_some() {
                    present |= 1 << (index - 1);
                }
                values.push(json!(value));
            }
            values[2] = json!(present);
            values.push(json!(row
                .get::<_, Option<String>>(6)
                .map_err(|error| error.to_string())?));
            values.push(json!(row
                .get::<_, Option<String>>(7)
                .map_err(|error| error.to_string())?));
            crate::publication::rows(
                "performance_cost_execution_symbol",
                &[
                    "record_id",
                    "symbol",
                    "present",
                    "status",
                    "error",
                    "outcome",
                    "outcome_reason",
                    "reason_code",
                    "memory_message",
                    "memory_reason_code",
                ],
                [Ok(values)],
                emit,
            )?;
        }
        Ok(())
    }
}
