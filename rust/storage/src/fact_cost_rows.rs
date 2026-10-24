//! Emit final frozen cost trades from the publication's source read snapshot.

use crate::error::AnalysisError;
use crate::publication::{finite, sql_error, stage_error};
use rusqlite::params;
use rusqlite::Connection;

pub(crate) fn trades(
    connection: &Connection,
    account: i64,
    target: &Connection,
) -> Result<(), AnalysisError> {
    let mut ticks = crate::fact_ticks::TickReader::new(connection).map_err(sql_error)?;
    let mut reader = crate::fact_trades::TradeReader::new(connection).map_err(sql_error)?;
    let mut statement = connection
        .prepare("SELECT id,created_at FROM executerecord WHERE account_id=?1 ORDER BY id")
        .map_err(sql_error)?;
    let mut rows = statement.query([account]).map_err(sql_error)?;
    let mut positions =
        crate::fact_positions::PositionReader::new(connection).map_err(sql_error)?;
    let mut transactions =
        crate::fact_transactions::TransactionReader::new(connection).map_err(sql_error)?;
    let mut attempts = crate::fact_attempts::AttemptReader::new(connection).map_err(sql_error)?;
    let mut headers = crate::fact_headers::HeaderReader::new(connection).map_err(sql_error)?;
    let mut next_id = 0_i64;
    while let Some(row) = rows.next().map_err(sql_error)? {
        let record: i64 = row.get(0).map_err(sql_error)?;
        let created_at: String = row.get(1).map_err(sql_error)?;
        let prices = ticks.read(record).map_err(sql_error)?;
        let fills = reader.read(record, &created_at, &prices)?;
        transactions.write(record, &fills, target)?;
        attempts.write(record, target)?;
        let position_rows = positions.read(record)?;
        headers.write(record, &fills, position_rows.as_ref().map(Vec::len), target)?;
        if let Some(positions) = position_rows {
            let mut insert = target.prepare_cached(
                "INSERT INTO native_stage.performance_cost_position(record_id,idx,symbol,quantity,direction) VALUES(?1,?2,?3,?4,?5)"
            ).map_err(stage_error)?;
            for (index, (symbol, quantity, direction)) in positions.into_iter().enumerate() {
                insert
                    .execute(params![record, index, symbol, quantity, direction])
                    .map_err(stage_error)?;
            }
        }
        let mut insert = target
            .prepare_cached(
                r#"INSERT INTO native_stage.performance_cost_trade (
                id, record_id, symbol, time, day, value,
                cost, loss_bp, fee, fee_currency, estimated, side,
                quantity, price, reference, reference_source, payload
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15, ?16, '{}'
            )"#,
            )
            .map_err(stage_error)?;
        for trade in fills {
            insert
                .execute(params![
                    next_id,
                    record,
                    trade.symbol,
                    trade.time,
                    trade.day,
                    finite(trade.value),
                    finite(trade.cost),
                    finite(trade.loss_bp),
                    finite(trade.fee),
                    trade.fee_currency,
                    trade.estimated,
                    trade.side,
                    finite(trade.quantity),
                    finite(trade.price),
                    finite(trade.reference),
                    trade.reference_source,
                ])
                .map_err(stage_error)?;
            next_id += 1;
        }
    }
    Ok(())
}
