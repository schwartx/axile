//! Frozen position preview: absent/degraded evidence is never flat.

use rusqlite::{Connection, OptionalExtension, Statement};
use serde_json::{json, Value};

pub(crate) struct PositionReader<'a> {
    root: Statement<'a>,
    positions: Statement<'a>,
}

impl<'a> PositionReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self {
            root: connection.prepare("SELECT a.asset_id,a.positions_kind,a.positions_count,a.source FROM business_asset_binding b JOIN business_asset_snapshot a ON a.asset_id=b.asset_id WHERE b.record_id=?1 AND b.path='/account_assets'")?,
            positions: connection.prepare("SELECT symbol,volume,direction FROM business_asset_position WHERE asset_id=?1 ORDER BY ordinal")?,
        })
    }

    pub(crate) fn read(&mut self, record: i64) -> Result<Option<Vec<Vec<Value>>>, String> {
        let root = self
            .root
            .query_row([record], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<usize>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            })
            .optional()
            .map_err(|error| error.to_string())?;
        let Some((asset, kind, count, source)) = root else {
            return Ok(None);
        };
        if kind != "array" || matches!(source.as_deref(), Some("assumed" | "error" | "unavailable"))
        {
            return Ok(None);
        }
        let mut rows = self
            .positions
            .query([asset])
            .map_err(|error| error.to_string())?;
        let mut seen = 0;
        let mut output = Vec::new();
        let mut valid = true;
        while let Some(row) = rows.next().map_err(|error| error.to_string())? {
            seen += 1;
            let quantity: Option<f64> = row.get(1).map_err(|error| error.to_string())?;
            let Some(quantity) = quantity.filter(|value| value.is_finite()) else {
                valid = false;
                continue;
            };
            if quantity == 0.0 {
                continue;
            }
            output.push(vec![
                json!(record),
                json!(output.len()),
                json!(row
                    .get::<_, Option<String>>(0)
                    .map_err(|error| error.to_string())?
                    .unwrap_or_default()),
                json!(quantity),
                json!(row
                    .get::<_, Option<String>>(2)
                    .map_err(|error| error.to_string())?
                    .unwrap_or_default()),
            ]);
        }
        if count != Some(seen) {
            return Err(format!(
                "SOURCE_FACTS_INCOMPLETE: record {record}: incomplete positions"
            ));
        }
        Ok(valid.then_some(output))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn flat_unknown_degraded_and_truncated_positions_stay_distinct() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE business_asset_binding(record_id INTEGER,path TEXT,asset_id TEXT); CREATE TABLE business_asset_snapshot(asset_id TEXT,positions_kind TEXT,positions_count INTEGER,source TEXT); CREATE TABLE business_asset_position(asset_id TEXT,ordinal INTEGER,symbol TEXT,volume REAL,direction TEXT); INSERT INTO business_asset_binding VALUES(1,'/account_assets','a'); INSERT INTO business_asset_snapshot VALUES('a','array',0,'real');").unwrap();
        let mut reader = PositionReader::new(&connection).unwrap();
        assert_eq!(reader.read(1).unwrap(), Some(vec![]));
        assert_eq!(reader.read(2).unwrap(), None);
        connection
            .execute(
                "UPDATE business_asset_snapshot SET source='unavailable'",
                [],
            )
            .unwrap();
        assert_eq!(reader.read(1).unwrap(), None);
        connection
            .execute(
                "UPDATE business_asset_snapshot SET source='real',positions_count=1",
                [],
            )
            .unwrap();
        assert!(reader.read(1).is_err());
        connection
            .execute(
                "INSERT INTO business_asset_position VALUES('a',0,'A',NULL,NULL)",
                [],
            )
            .unwrap();
        assert_eq!(reader.read(1).unwrap(), None);
    }
}
