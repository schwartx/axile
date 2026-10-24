//! Emit final frozen cost trades from the publication's source read snapshot.

use rusqlite::Connection;
use serde_json::{json, Value};

pub(crate) const TRADE_COLUMNS: &[&str] = &[
    "id",
    "record_id",
    "symbol",
    "time",
    "day",
    "value",
    "cost",
    "loss_bp",
    "fee",
    "fee_currency",
    "estimated",
    "side",
    "quantity",
    "price",
    "reference",
    "reference_source",
    "payload",
];

pub(crate) fn trades(
    connection: &Connection,
    account: i64,
    emit: &mut crate::publication::RowEmitter<'_>,
) -> Result<(), String> {
    let mut ticks =
        crate::fact_ticks::TickReader::new(connection).map_err(|error| error.to_string())?;
    let mut reader =
        crate::fact_trades::TradeReader::new(connection).map_err(|error| error.to_string())?;
    let mut statement = connection
        .prepare("SELECT id,created_at FROM executerecord WHERE account_id=?1 ORDER BY id")
        .map_err(|error| error.to_string())?;
    let mut rows = statement
        .query([account])
        .map_err(|error| error.to_string())?;
    let mut positions = crate::fact_positions::PositionReader::new(connection)
        .map_err(|error| error.to_string())?;
    let mut transactions = crate::fact_transactions::TransactionReader::new(connection)
        .map_err(|error| error.to_string())?;
    let mut attempts =
        crate::fact_attempts::AttemptReader::new(connection).map_err(|error| error.to_string())?;
    let mut headers =
        crate::fact_headers::HeaderReader::new(connection).map_err(|error| error.to_string())?;
    let mut next_id = 0_i64;
    while let Some(row) = rows.next().map_err(|error| error.to_string())? {
        let record: i64 = row.get(0).map_err(|error| error.to_string())?;
        let created_at: String = row.get(1).map_err(|error| error.to_string())?;
        let prices = ticks.read(record).map_err(|error| error.to_string())?;
        let fills = reader.read(record, &created_at, &prices)?;
        transactions.emit(record, &fills, emit)?;
        attempts.emit(record, emit)?;
        let position_rows = positions.read(record)?;
        headers.emit(record, &fills, position_rows.as_ref().map(Vec::len), emit)?;
        if let Some(positions) = position_rows {
            crate::publication::rows(
                "performance_cost_position",
                &["record_id", "idx", "symbol", "quantity", "direction"],
                positions.into_iter().map(Ok),
                emit,
            )?;
        }
        crate::publication::rows(
            "performance_cost_trade",
            TRADE_COLUMNS,
            fills.into_iter().map(|trade| {
                let id = next_id;
                next_id += 1;
                Ok(vec![
                    json!(id),
                    json!(record),
                    json!(trade.symbol),
                    json!(trade.time),
                    json!(trade.day),
                    json!(trade.value),
                    json!(trade.cost),
                    json!(trade.loss_bp),
                    json!(trade.fee),
                    json!(trade.fee_currency),
                    Value::Bool(trade.estimated),
                    json!(trade.side),
                    json!(trade.quantity),
                    json!(trade.price),
                    json!(trade.reference),
                    json!(trade.reference_source),
                    json!("{}"),
                ])
            }),
            emit,
        )?;
    }
    Ok(())
}
