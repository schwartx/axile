//! Read only pricing columns, reusing a prepared statement for each execution.

use std::collections::BTreeMap;

use rusqlite::{Connection, Statement};

use crate::fact_prices::{cost_reference, replay_reference};

#[derive(Debug, PartialEq)]
pub(crate) struct Prices {
    pub cost: Option<f64>,
    pub cost_source: Option<&'static str>,
    pub replay: Option<f64>,
}

pub(crate) struct TickReader<'a> {
    statement: Statement<'a>,
    root_statement: Statement<'a>,
}

impl<'a> TickReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self {
            statement: connection.prepare(
                "SELECT s.map_key,t.bid_price,t.ask_price,t.last_price,t.book_valid,t.path
                 FROM execution_symbol_result s
                 LEFT JOIN execution_symbol_tick t ON t.record_id=s.record_id
                     AND t.path=s.path || '/first_tick'
                 WHERE s.record_id=?1 AND s.path LIKE '/symbol_results/%'
                 ORDER BY s.ordinal",
            )?,
            root_statement: connection.prepare(
                "SELECT map_key,bid_price,ask_price,last_price,book_valid
                 FROM execution_symbol_tick WHERE record_id=?1 AND path LIKE '/first_ticks/%'
                 ORDER BY ordinal",
            )?,
        })
    }

    pub(crate) fn read(&mut self, record_id: i64) -> rusqlite::Result<BTreeMap<String, Prices>> {
        let mut values: BTreeMap<String, Prices> = self
            .root_statement
            .query_map([record_id], |row| {
                Ok((
                    row.get(0)?,
                    Prices {
                        cost: None,
                        cost_source: None,
                        replay: replay_reference(
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                            row.get(4)?,
                        ),
                    },
                ))
            })?
            .collect::<rusqlite::Result<_>>()?;
        let children: BTreeMap<String, Prices> = self
            .statement
            .query_map([record_id], |row| {
                let bid = row.get(1)?;
                let ask = row.get(2)?;
                let last = row.get(3)?;
                let (cost, cost_source) = cost_reference(bid, ask, last);
                let symbol: String = row.get(0)?;
                let replay = if row.get::<_, Option<String>>(5)?.is_some() {
                    replay_reference(bid, ask, last, row.get(4)?)
                } else {
                    values.get(&symbol).and_then(|prices| prices.replay)
                };
                Ok((
                    symbol,
                    Prices {
                        cost,
                        cost_source,
                        replay,
                    },
                ))
            })?
            .collect::<rusqlite::Result<_>>()?;
        values.extend(children);
        Ok(values)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepared_reader_keeps_execution_and_symbol_boundaries() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER,map_key TEXT,ordinal INTEGER);
             INSERT INTO execution_symbol_result VALUES (1,'/symbol_results/A','A',0),(2,'/symbol_results/A','A',0);
             INSERT INTO execution_symbol_tick VALUES (1,'/symbol_results/A/first_tick',99,101,103,1,'A',0),(2,'/symbol_results/A/first_tick',10,12,15,0,'A',0);"
        ).unwrap();
        let mut reader = TickReader::new(&connection).unwrap();
        assert_eq!(
            reader.read(1).unwrap()["A"],
            Prices {
                cost: Some(100.0),
                cost_source: Some("mid"),
                replay: Some(103.0)
            }
        );
        assert_eq!(
            reader.read(2).unwrap()["A"],
            Prices {
                cost: Some(11.0),
                cost_source: Some("mid"),
                replay: None
            }
        );
        assert!(reader.read(3).unwrap().is_empty());
    }
}
