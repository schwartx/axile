//! Final cost headers from canonical scalar business evidence.

use crate::error::AnalysisError;
use crate::fact_trades::Trade;
use crate::publication::{finite, sql_error, stage_error};
use rusqlite::params;
use rusqlite::{Connection, Statement};

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
            symbols:connection.prepare("SELECT map_key,node_kind,status,error,outcome,outcome_reason,reason_code,memory FROM execution_symbol_result WHERE record_id=?1 ORDER BY ordinal")?,
        })
    }
    pub(crate) fn write(
        &mut self,
        record: i64,
        fills: &[Trade],
        positions: Option<usize>,
        target: &Connection,
    ) -> Result<(), AnalysisError> {
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
            .map_err(sql_error)?;
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
        let time = crate::calendar::timestamp_ms(&created)?;
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
        let summary = crate::execution_summary::write_fees(target, record, fills)?;
        let mut insert = target.prepare_cached(r#"INSERT INTO native_stage.performance_cost_execution (
                record_id, time, success, noop, key, execution_id,
                created_at, is_success, duration_sec, trade_count, symbol_count, position_count,
                positions_present, reason, raw_present, has_symbol_results, raw_status, raw_task_status,
                raw_error, raw_outcome, raw_outcome_reason, raw_reason_code, raw_msg, raw_execution_kind,
                value, cost, loss_bp, coverage, covered,
                count, fee_covered, amount_complete, covered_value, estimated, has_transactions
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15, ?16, ?17, ?18,
                ?19, ?20, ?21, ?22, ?23, ?24,
                ?25, ?26, ?27, ?28, ?29,
                ?30, ?31, ?32, ?33, ?34, ?35
            )"#).map_err(stage_error)?;
        insert
            .execute(params![
                record,
                time,
                success == 1,
                noop,
                record.to_string(),
                execution,
                created,
                success,
                duration.filter(|value| value.is_finite() && *value >= 0.0),
                fills.len(),
                count,
                positions,
                positions.is_some(),
                raw[2]
                    .as_deref()
                    .unwrap_or_default()
                    .chars()
                    .take(300)
                    .collect::<String>(),
                present,
                has_symbol_results,
                raw[0],
                raw[1],
                raw[2],
                raw[3],
                raw[4],
                raw[5],
                raw[6],
                raw[7],
                finite(summary.value),
                finite(summary.cost),
                finite(summary.loss_bp()),
                finite(summary.coverage()),
                summary.covered,
                summary.count,
                summary.fee_count,
                summary.amount_complete(),
                finite(summary.covered_value),
                summary.estimated,
                count > 0,
            ])
            .map_err(stage_error)?;
        let mut rows = self.symbols.query([record]).map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let display = crate::execution_symbol_display::symbol_from_row(row)?;
            let fields =
                crate::execution_symbol_display::SYMBOL_FIELDS.map(|name| display.get(name));
            let present = fields
                .iter()
                .enumerate()
                .fold(0_i64, |mask, (index, value)| {
                    mask | (i64::from(value.is_some()) << index)
                });
            let fields = fields.map(|value| value.and_then(serde_json::Value::as_str));
            target
                .prepare_cached(
                    r#"INSERT INTO native_stage.performance_cost_execution_symbol (
                record_id, symbol, present, status, error, outcome,
                outcome_reason, reason_code, memory_message, memory_reason_code
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, NULL, NULL
            )"#,
                )
                .map_err(stage_error)?
                .execute(params![
                    record,
                    row.get::<_, String>(0).map_err(sql_error)?,
                    present,
                    fields[0],
                    fields[1],
                    fields[2],
                    fields[3],
                    fields[4],
                ])
                .map_err(stage_error)?;
        }
        Ok(())
    }
}
