//! Account-scoped structural checks before deriving analysis from business facts.

use rusqlite::{Connection, OptionalExtension};

pub(crate) fn validate(connection: &Connection, account: i64) -> Result<(), String> {
    let invalid = connection.query_row(
        "SELECT e.id FROM executerecord e
         WHERE e.account_id=?1 AND e.result_staged=0 AND (
             e.facts_complete IS NOT 1
             OR (e.input_id IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM execution_input i WHERE i.id=e.input_id AND i.account_id=e.account_id))
             OR (e.result_input_id IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM execution_input i WHERE i.id=e.result_input_id AND i.account_id=e.account_id))
             OR EXISTS (SELECT 1 FROM execution_symbol_collection c
                 WHERE c.record_id=e.id AND c.path='/symbol_results' AND c.node_kind='object'
                 AND c.member_count IS NOT (SELECT COUNT(*) FROM execution_symbol_result s WHERE s.record_id=e.id))
             OR EXISTS (SELECT 1 FROM execution_symbol_result s
                 WHERE s.record_id=e.id AND (
                     (s.tick_recorded=1 AND NOT EXISTS (SELECT 1 FROM execution_symbol_tick t
                         WHERE t.record_id=e.id AND t.path=s.path || '/first_tick'))
                     OR (s.sizing_recorded=1 AND NOT EXISTS (SELECT 1 FROM execution_symbol_sizing z
                         WHERE z.record_id=e.id AND z.path=s.path || '/sizing'))))
             OR EXISTS (SELECT 1 FROM business_trade_collection c
                 WHERE c.record_id=e.id AND c.state='array' AND c.member_count IS NOT
                     (SELECT COUNT(*) FROM business_trade t WHERE t.record_id=e.id AND t.collection_path=c.path))
         ) ORDER BY e.id LIMIT 1",
        [account], |row| row.get::<_, i64>(0),
    ).optional().map_err(|error| format!("SOURCE_FACTS_READ: {error}"))?;
    match invalid {
        Some(id) => Err(format!(
            "SOURCE_FACTS_INCOMPLETE: record {id}: incomplete or cross-account business facts"
        )),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn incomplete_and_cross_account_inputs_are_rejected() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,facts_complete INTEGER,input_id TEXT,result_input_id TEXT,result_staged INTEGER DEFAULT 0);
             CREATE TABLE execution_input(id TEXT,account_id INTEGER);
             CREATE TABLE execution_symbol_collection(record_id INTEGER,path TEXT,node_kind TEXT,member_count INTEGER);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,tick_recorded INTEGER,sizing_recorded INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT);
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,state TEXT,member_count INTEGER);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT);
             INSERT INTO executerecord(id,account_id,facts_complete,input_id,result_input_id) VALUES(1,7,0,NULL,NULL);") .unwrap();
        assert!(validate(&connection, 7).unwrap_err().contains("record 1"));
        validate(&connection, 8).unwrap();
        connection.execute_batch("UPDATE executerecord SET facts_complete=1,input_id='input'; INSERT INTO execution_input VALUES('input',8);").unwrap();
        assert!(validate(&connection, 7).is_err());
        connection
            .execute("UPDATE execution_input SET account_id=7", [])
            .unwrap();
        validate(&connection, 7).unwrap();
        connection
            .execute(
                "INSERT INTO business_trade_collection VALUES(1,'/trades','array',1)",
                [],
            )
            .unwrap();
        assert!(validate(&connection, 7).is_err());
        connection
            .execute("INSERT INTO business_trade VALUES(1,'/trades')", [])
            .unwrap();
        validate(&connection, 7).unwrap();
    }
}
