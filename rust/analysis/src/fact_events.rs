//! Execution event wording from canonical terminal and symbol facts.

use rusqlite::{Connection, Statement};

pub(crate) struct EventReader<'a> {
    symbols: Statement<'a>,
    root_orders: Statement<'a>,
    malformed: Statement<'a>,
}

impl<'a> EventReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self {
            malformed: connection.prepare("SELECT EXISTS(SELECT 1 FROM execution_symbol_collection WHERE record_id=?1 AND path='/symbol_results' AND node_kind!='object') OR EXISTS(SELECT 1 FROM execution_order_collection WHERE record_id=?1 AND kind='invalid') OR EXISTS(SELECT 1 FROM business_trade_collection WHERE record_id=?1 AND state!='array' AND malformed_value IS NOT NULL AND malformed_value!='null')")?,
            symbols: connection.prepare(
                "SELECT s.node_kind,s.status,s.reason_code,
                 EXISTS(SELECT 1 FROM execution_order o WHERE o.record_id=s.record_id AND o.path=s.path || '/orders'),
                 EXISTS(SELECT 1 FROM business_trade t WHERE t.record_id=s.record_id AND t.collection_path=s.path || '/trades')
                 FROM execution_symbol_result s WHERE s.record_id=?1 ORDER BY s.ordinal",
            )?,
            root_orders: connection.prepare(
                "SELECT EXISTS(SELECT 1 FROM execution_order WHERE record_id=?1 AND path='/orders')
                    OR EXISTS(SELECT 1 FROM business_trade WHERE record_id=?1 AND collection_path='/trades')",
            )?,
        })
    }

    pub(crate) fn label(
        &mut self,
        id: i64,
        status: Option<&str>,
        task: Option<&str>,
        reason: Option<&str>,
    ) -> rusqlite::Result<&'static str> {
        if task == Some("TERMINATED") {
            return Ok("执行已终止");
        }
        let ordinary = match status {
            Some("SUCCEEDED") => "调仓完成",
            Some("NOOP") => "无需调仓",
            Some("PARTIAL") => "执行未全部完成",
            Some("FAILED") => "执行失败",
            Some("BLOCKED") => "未执行",
            _ => "执行状态未知",
        };
        if reason != Some("COMMON.SESSION.CLOSED") || !matches!(status, Some("BLOCKED" | "PARTIAL"))
        {
            return Ok(ordinary);
        }
        if self
            .malformed
            .query_row([id], |row| row.get::<_, bool>(0))?
        {
            return Ok(ordinary);
        }
        if status == Some("BLOCKED")
            && self
                .root_orders
                .query_row([id], |row| row.get::<_, bool>(0))?
        {
            return Ok(ordinary);
        }
        let mut rows = self.symbols.query([id])?;
        let mut count = 0;
        let mut open = false;
        let mut closed = false;
        while let Some(row) = rows.next()? {
            count += 1;
            let kind: String = row.get(0)?;
            let status: Option<String> = row.get(1)?;
            let code: Option<String> = row.get(2)?;
            if kind != "object" {
                return Ok(ordinary);
            }
            if matches!(status.as_deref(), Some("SUCCEEDED" | "NOOP")) {
                open = true;
            } else if status.as_deref() == Some("BLOCKED")
                && code.as_deref() == Some("COMMON.SESSION.CLOSED")
                && !row.get::<_, bool>(3)?
                && !row.get::<_, bool>(4)?
            {
                closed = true;
            } else {
                return Ok(ordinary);
            }
        }
        Ok(
            if status == Some("BLOCKED") && (count == 0 || closed && !open) {
                "休市跳过"
            } else if status == Some("PARTIAL") && closed && open {
                "部分品种休市 · 执行未全部完成"
            } else {
                ordinary
            },
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn terminal_and_closed_labels_keep_order_evidence() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,ordinal INTEGER,node_kind TEXT,status TEXT,reason_code TEXT); CREATE TABLE execution_order(record_id INTEGER,path TEXT); CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT); CREATE TABLE execution_symbol_collection(record_id INTEGER,path TEXT,node_kind TEXT); CREATE TABLE execution_order_collection(record_id INTEGER,kind TEXT); CREATE TABLE business_trade_collection(record_id INTEGER,state TEXT,malformed_value TEXT);").unwrap();
        let mut reader = EventReader::new(&connection).unwrap();
        assert_eq!(
            reader
                .label(1, Some("BLOCKED"), None, Some("COMMON.SESSION.CLOSED"))
                .unwrap(),
            "休市跳过"
        );
        connection
            .execute(
                "INSERT INTO execution_symbol_collection VALUES(2,'/symbol_results','value')",
                [],
            )
            .unwrap();
        assert_eq!(
            reader
                .label(2, Some("BLOCKED"), None, Some("COMMON.SESSION.CLOSED"))
                .unwrap(),
            "未执行"
        );
        connection
            .execute(
                "INSERT INTO business_trade_collection VALUES(3,'malformed','{\"bad\":true}')",
                [],
            )
            .unwrap();
        assert_eq!(
            reader
                .label(3, Some("BLOCKED"), None, Some("COMMON.SESSION.CLOSED"))
                .unwrap(),
            "未执行"
        );
        connection
            .execute("INSERT INTO execution_order VALUES(1,'/orders')", [])
            .unwrap();
        assert_eq!(
            reader
                .label(1, Some("BLOCKED"), None, Some("COMMON.SESSION.CLOSED"))
                .unwrap(),
            "未执行"
        );
        assert_eq!(
            reader
                .label(
                    1,
                    Some("BLOCKED"),
                    Some("TERMINATED"),
                    Some("COMMON.SESSION.CLOSED")
                )
                .unwrap(),
            "执行已终止"
        );
    }
}
