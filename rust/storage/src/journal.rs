//! Account journal over page-bounded scalar facts, in the caller's read transaction.

use std::collections::HashMap;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use crate::cost_summary::Summary;
#[cfg(test)]
use crate::cost_summary::AGGREGATES;

const HEADERS: &str = "SELECT id,axile_time(created_at) AS occurred_at,0 AS kind FROM executerecord
    WHERE account_id=?1 AND (?2 IS NULL OR axile_time(created_at)>=?2) AND (?3 IS NULL OR axile_time(created_at)<?3)
    UNION ALL SELECT id,axile_time(triggered_at),1 FROM schedule_skip
    WHERE account_id=?1 AND (?2 IS NULL OR axile_time(triggered_at)>=?2) AND (?3 IS NULL OR axile_time(triggered_at)<?3)";
const OWNER_VALID: &str = "((a.record_id=r.id AND a.path=b.path AND a.scope=b.scope
    AND a.ordinal=b.ordinal AND a.map_key IS b.map_key) OR
    (a.input_id IN (r.input_id,r.result_input_id) AND i.account_id=r.account_id
    AND coalesce(i.owner_execution_id,i.execution_id) IS r.execution_id
    AND (i.execution_id IS NULL OR i.execution_id=r.execution_id)
    AND a.path='/after_account_assets' AND a.scope='after_pending' AND b.scope='after'))";

fn database(error: rusqlite::Error) -> String {
    format!("JOURNAL_READ_ERROR: {error}")
}

/// Return the final HTTP journal shape without reading execution or input payloads.
pub fn page(
    connection: &Connection,
    account_id: i64,
    skip: i64,
    limit: i64,
    since: Option<&str>,
    until: Option<&str>,
) -> Result<Value, String> {
    if skip < 0 || !(1..=500).contains(&limit) {
        return Err(
            "INVALID_REQUEST: skip must be nonnegative and limit must be 1 through 500".into(),
        );
    }
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(database)?;
    if !exists {
        return Err("ACCOUNT_NOT_FOUND: account does not exist".into());
    }
    let since = since.filter(|value| !value.is_empty())
        .map(crate::calendar::canonical_time).transpose()?;
    let until = until.filter(|value| !value.is_empty())
        .map(crate::calendar::canonical_time).transpose()?;
    let args = params![account_id, since, until, limit, skip];
    let count: i64 = connection
        .query_row(
            &format!("SELECT count(*) FROM ({HEADERS})"),
            params![account_id, since, until],
            |row| row.get(0),
        )
        .map_err(database)?;
    let cte = format!(
        "WITH page AS (SELECT * FROM ({HEADERS})
        ORDER BY occurred_at DESC,id DESC,kind LIMIT ?4 OFFSET ?5),
        selected AS (SELECT id FROM page WHERE kind=0)"
    );

    let mut symbols: HashMap<i64, Map<String, Value>> = HashMap::new();
    let mut statement = connection.prepare(&format!("{cte}
        SELECT s.record_id,s.map_key,s.node_kind,s.status,s.error,s.outcome,s.outcome_reason,s.reason_code,s.memory
        FROM execution_symbol_result s JOIN selected p ON p.id=s.record_id
        ORDER BY s.record_id,s.map_key")).map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let fields = crate::executions::symbol_from_row(row)
        .map_err(|error| format!("JOURNAL_READ_ERROR: {error}"))?;
        symbols
            .entry(row.get(0).map_err(database)?)
            .or_default()
            .insert(row.get(1).map_err(database)?, fields);
    }

    let mut summaries = HashMap::new();
    let mut ticks = crate::fact_ticks::TickReader::new(connection).map_err(database)?;
    let mut trades = crate::fact_trades::TradeReader::new(connection).map_err(database)?;
    let mut statement = connection.prepare(&format!("{cte}
        SELECT r.id,r.created_at,r.facts_complete FROM executerecord r JOIN selected p ON p.id=r.id"))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let id: i64 = row.get(0).map_err(database)?;
        if !row.get::<_, bool>(2).map_err(database)? {
            return Err(format!("SOURCE_FACTS_INCOMPLETE: record {id}"));
        }
        let created: String = row.get(1).map_err(database)?;
        let prices = ticks.read(id).map_err(database)?;
        let fills = trades.read(id, &created, &prices)?;
        let mut summary = Summary::empty();
        for fill in fills {
            summary.push(&fill);
        }
        summaries.insert(id, historical_summary(summary.output()));
    }

    let mut totals = HashMap::new();
    let mut statement = connection
        .prepare(&format!(
            "{cte}
        SELECT b.record_id,a.total_asset,a.observation_valid,{OWNER_VALID} AS owner_valid
        FROM selected p JOIN business_asset_binding b ON b.record_id=p.id
        JOIN business_asset_snapshot a ON a.asset_id=b.asset_id
        JOIN executerecord r ON r.id=b.record_id LEFT JOIN execution_input i ON i.id=a.input_id
        WHERE b.path='/account_assets'"
        ))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        if !row
            .get::<_, Option<bool>>(3)
            .map_err(database)?
            .unwrap_or(false)
        {
            return Err("JOURNAL_READ_ERROR: 资产观测属于其他执行或账户".into());
        }
        let total = if row.get::<_, bool>(2).map_err(database)? {
            json!(row.get::<_, Option<f64>>(1).map_err(database)?)
        } else {
            Value::Null
        };
        totals.insert(row.get::<_, i64>(0).map_err(database)?, total);
    }

    let mut data = Vec::new();
    let mut statement = connection
        .prepare(&format!(
            "{cte}
        SELECT r.id,r.execution_id,r.created_at,r.is_success,r.display_status,r.task_status,
        r.display_error,r.display_outcome,r.display_outcome_reason,r.reason_code,r.execution_kind,
        r.execution_time,r.id,NULL,
        EXISTS(SELECT 1 FROM execution_symbol_collection c WHERE c.record_id=r.id AND c.path='/symbol_results' AND c.node_kind='object'),
        (SELECT count(*) FROM business_trade t JOIN business_trade_collection c
          ON c.record_id=t.record_id AND c.path=t.collection_path
          JOIN execution_symbol_result s ON s.record_id=c.record_id AND s.path=c.parent_path
          WHERE t.record_id=r.id AND c.state='array'),
        p.kind,k.id,k.triggered_at,k.channel,k.calendar_id,k.calendar_day,k.calendar_label,k.reason_code
        FROM page p LEFT JOIN executerecord r ON p.kind=0 AND r.id=p.id
        LEFT JOIN schedule_skip k ON p.kind=1 AND k.id=p.id
        ORDER BY p.occurred_at DESC,p.id DESC,p.kind"
        ))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        if row.get::<_, i64>(16).map_err(database)? == 1 {
            data.push(
                json!({"kind":"schedule_skip","id":row.get::<_, i64>(17).map_err(database)?,
                "occurred_at":row.get::<_, String>(18).map_err(database)?,
                "channel":row.get::<_, String>(19).map_err(database)?,
                "calendar_id":row.get::<_, String>(20).map_err(database)?,
                "calendar_day":row.get::<_, String>(21).map_err(database)?,
                "calendar_label":row.get::<_, String>(22).map_err(database)?,
                "reason_code":row.get::<_, String>(23).map_err(database)?}),
            );
            continue;
        }
        let id: i64 = row.get(0).map_err(database)?;
        let created: String = row.get(2).map_err(database)?;
        let projected = row.get::<_, Option<i64>>(12).map_err(database)?.is_some()
            && row
                .get::<_, Option<String>>(13)
                .map_err(database)?
                .is_none();
        let trade_count: Option<i64> = if projected {
            row.get(15).map_err(database)?
        } else {
            None
        };
        let duration = row
            .get::<_, Option<f64>>(11)
            .map_err(database)?
            .filter(|value| *value >= 0.0);
        let symbol_results = if projected && row.get::<_, bool>(14).map_err(database)? {
            symbols.remove(&id).unwrap_or_default()
        } else {
            Map::new()
        };
        let mut record = json!({"id":id,"execution_id":row.get::<_, Option<String>>(1).map_err(database)?,
            "created_at":created,"is_success":row.get::<_, i64>(3).map_err(database)?,
            "symbol_results":symbol_results,"total_asset":totals.remove(&id).unwrap_or(Value::Null),
            "summary":if trade_count.is_some() { summaries.remove(&id).unwrap_or_else(empty_summary) } else { Value::Null },
            "duration_sec":duration,"trade_count":trade_count});
        for (index, name) in [
            "status",
            "task_status",
            "error",
            "outcome",
            "outcome_reason",
            "reason_code",
            "execution_kind",
        ]
        .iter()
        .enumerate()
        {
            let value = row
                .get::<_, Option<String>>(index + 4)
                .map_err(database)?
                .filter(|value| !value.is_empty());
            record[*name] = json!(value);
        }
        data.push(json!({"kind":"execution","occurred_at":created,"record":record}));
    }
    Ok(json!({"data":data,"count":count}))
}

fn empty_summary() -> Value {
    json!({"value":null,"cost":null,"lossBp":null,"coverage":null,"covered":0,
        "count":0,"amountComplete":true,"fees":{},"feeCovered":0})
}

pub(crate) fn historical_summary(mut summary: Value) -> Value {
    let object = summary.as_object_mut().expect("summary object");
    if object["coveredValue"].as_f64().unwrap_or(0.0) <= 0.0 {
        object.insert("lossBp".into(), Value::Null);
    }
    if object["value"].as_f64().unwrap_or(0.0) <= 0.0 {
        object.insert("coverage".into(), Value::Null);
    }
    object.remove("coveredValue");
    object.remove("estimated");
    summary
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let conn = crate::execution_summary::tests::fixture();
        conn.execute_batch("CREATE TABLE account(id INTEGER); INSERT INTO account VALUES(2);
            CREATE TABLE schedule_skip(id INTEGER,account_id INTEGER,triggered_at TEXT,channel TEXT,calendar_id TEXT,calendar_day TEXT,calendar_label TEXT,reason_code TEXT);
            CREATE TABLE execution_input(id TEXT,account_id INTEGER,owner_execution_id TEXT,execution_id TEXT);
            CREATE TABLE business_asset_binding(record_id INTEGER,asset_id TEXT,path TEXT,scope TEXT,ordinal INTEGER,map_key TEXT);
            CREATE TABLE business_asset_snapshot(asset_id TEXT,record_id INTEGER,input_id TEXT,path TEXT,scope TEXT,ordinal INTEGER,map_key TEXT,total_asset REAL,observation_valid INTEGER);
            CREATE TABLE execution_symbol_collection(record_id INTEGER,path TEXT,node_kind TEXT);
            INSERT INTO execution_symbol_collection VALUES(1,'/symbol_results','object');").unwrap();
        for column in [
            "execution_id TEXT",
            "input_id TEXT",
            "result_input_id TEXT",
            "facts_complete INTEGER DEFAULT 1",
            "is_success INTEGER DEFAULT 1",
            "display_status TEXT",
            "task_status TEXT",
            "display_error TEXT",
            "display_outcome TEXT",
            "display_outcome_reason TEXT",
            "reason_code TEXT",
            "execution_kind TEXT",
            "execution_time REAL",
        ] {
            conn.execute_batch(&format!("ALTER TABLE executerecord ADD COLUMN {column}"))
                .unwrap();
        }
        for name in [
            "status",
            "error",
            "outcome",
            "outcome_reason",
            "reason_code",
            "memory",
        ] {
            conn.execute_batch(&format!(
                "ALTER TABLE execution_symbol_result ADD COLUMN {name} TEXT"
            ))
            .unwrap();
        }
        conn.execute_batch("ALTER TABLE execution_symbol_result ADD COLUMN node_kind TEXT NOT NULL DEFAULT 'object'").unwrap();
        conn
    }

    #[test]
    fn canonical_page_without_projection_tables() {
        let conn = fixture();
        let result = page(&conn, 2, 0, 10, None, None).unwrap();
        assert_eq!(result["count"], 2);
        assert_eq!(result["data"][0]["record"]["summary"]["count"], 0);
        assert_eq!(result["data"][1]["record"]["trade_count"], 2);
        assert_eq!(result["data"][1]["record"]["summary"]["fees"]["USD"], -0.2);
        assert_eq!(
            result["data"][1]["record"]["symbol_results"],
            json!({"A":{"outcome":null}})
        );
        assert_eq!(
            page(&conn, 2, 1, 1, None, None).unwrap()["data"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn timezones_are_normalized_before_filtering_ordering_and_pagination() {
        let conn = fixture();
        conn.execute_batch("UPDATE executerecord SET created_at='2026-01-31T16:30:00Z' WHERE id=1;
            UPDATE executerecord SET created_at='2026-02-01 00:20:00' WHERE id=2;
            INSERT INTO schedule_skip VALUES(20,2,'2026-01-31T16:25:00Z','ctp','calendar','2026-02-01','test','BUSY');").unwrap();
        let since = Some("2026-02-01T00:00:00+08:00");
        let until = Some("2026-02-02T00:00:00+08:00");
        let result = page(&conn, 2, 0, 1, since, until).unwrap();
        assert_eq!(result["count"], 3);
        assert_eq!(result["data"][0]["record"]["id"], 1);
        assert_eq!(page(&conn, 2, 1, 1, since, until).unwrap()["data"][0]["id"], 20);
        assert_eq!(page(&conn, 2, 2, 1, since, until).unwrap()["data"][0]["record"]["id"], 2);
        assert_eq!(page(&conn, 2, 0, 10, since, Some("2026-02-01 00:30:00")).unwrap()["count"], 2);
    }

    #[test]
    fn symbol_display_matches_execution_projection_without_memory_or_fact_updates() {
        let conn = fixture();
        conn.execute_batch("UPDATE execution_symbol_result SET status='NOOP',memory='null' WHERE record_id=1;
            INSERT INTO execution_symbol_result(record_id,path,map_key,ordinal,status,memory,node_kind) VALUES
            (1,'/symbol_results/empty','empty',1,NULL,'null','object'),
            (1,'/symbol_results/failed','failed',2,'FAILED','{\"message\":\"RuntimeError: private\",\"private\":true}','object'),
            (1,'/symbol_results/malformed','malformed',3,NULL,NULL,'value');").unwrap();
        let result = page(&conn, 2, 0, 10, None, None).unwrap();
        assert_eq!(
            result["data"][1]["record"]["symbol_results"],
            json!({
                "A":{"status":"NOOP","outcome":"completed"},
                "empty":{"outcome":null},
                "failed":{"status":"FAILED","outcome":"error","error":"执行失败，具体原因见执行证据"},
                "malformed":{"outcome":null}
            })
        );
        assert_eq!(conn.query_row("SELECT count(*) FROM execution_symbol_result WHERE record_id=1 AND outcome IS NULL AND error IS NULL AND reason_code IS NULL", [], |row| row.get::<_,i64>(0)).unwrap(),4);
        conn.execute_batch(
            "UPDATE execution_symbol_result SET memory='bad-json' WHERE map_key='A'",
        )
        .unwrap();
        assert!(page(&conn, 2, 0, 10, None, None)
            .unwrap_err()
            .starts_with("JOURNAL_READ_ERROR:"));
    }

    #[test]
    fn union_bounds_ties_and_pagination() {
        let conn = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&conn).unwrap();
        conn.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT);
            CREATE TABLE schedule_skip(id INTEGER,account_id INTEGER,triggered_at TEXT);
            INSERT INTO executerecord VALUES(1,1,'2026-01-01'),(2,1,'2026-01-02'),(9,2,'2026-01-04');
            INSERT INTO schedule_skip VALUES(2,1,'2026-01-02'),(3,1,'2026-01-03');",
        )
        .unwrap();
        let sql = format!("SELECT kind,id FROM ({HEADERS}) ORDER BY occurred_at DESC,id DESC,kind LIMIT ?4 OFFSET ?5");
        let mut stmt = conn.prepare(&sql).unwrap();
        let result = stmt
            .query_map(params![1, "2026-01-02", "2026-01-03", 10, 0], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(result, vec![(0, 2), (1, 2)]);
        let result = stmt
            .query_map(
                params![1, Option::<String>::None, Option::<String>::None, 1, 1],
                |row| row.get::<_, i64>(0),
            )
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(result, vec![0]);
    }

    #[test]
    fn owner_validation_is_null_safe_and_fail_closed() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE r(id INTEGER,input_id TEXT,result_input_id TEXT,account_id INTEGER,execution_id TEXT);
            CREATE TABLE a(record_id INTEGER,input_id TEXT,path TEXT,scope TEXT,ordinal INTEGER,map_key TEXT);
            CREATE TABLE b(path TEXT,scope TEXT,ordinal INTEGER,map_key TEXT);
            CREATE TABLE i(account_id INTEGER,owner_execution_id TEXT,execution_id TEXT);
            INSERT INTO r VALUES(1,'input','result',7,'exec');
            INSERT INTO a VALUES(1,NULL,'/account_assets','after',0,NULL);
            INSERT INTO b VALUES('/account_assets','after',0,NULL);
            INSERT INTO i VALUES(7,'exec',NULL);").unwrap();
        let valid = || {
            conn.query_row(&format!("SELECT {OWNER_VALID} FROM r,a,b,i"), [], |row| {
                row.get::<_, Option<bool>>(0)
            })
            .unwrap()
            .unwrap_or(false)
        };
        assert!(valid());
        conn.execute_batch("UPDATE a SET record_id=2;").unwrap();
        assert!(!valid());
        conn.execute_batch("UPDATE a SET record_id=NULL,input_id='input',path='/after_account_assets',scope='after_pending';").unwrap();
        assert!(valid());
        conn.execute_batch("UPDATE i SET account_id=8;").unwrap();
        assert!(!valid());
        conn.execute_batch("UPDATE i SET account_id=7,execution_id='other';")
            .unwrap();
        assert!(!valid());
    }

    #[test]
    fn unvalued_costs_do_not_contaminate_shared_summary() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE trade(value REAL,cost REAL,loss_bp REAL,estimated INTEGER);
            INSERT INTO trade VALUES(100,2,200,0),(NULL,999,999,0),(50,NULL,NULL,0);",
        )
        .unwrap();
        let mut summary = conn.query_row(&format!("WITH trades AS
            (SELECT value,CASE WHEN value IS NOT NULL THEN cost END AS cost,loss_bp,estimated FROM trade)
            SELECT {AGGREGATES} FROM trades"), [], |row| Summary::read(row,0)).unwrap();
        summary.fees.insert("USD".into(), (3.0, 1));
        summary.fee_count = 1;
        let result = historical_summary(summary.output());
        assert_eq!(result["value"], 150.0);
        assert_eq!(result["cost"], 2.0);
        assert_eq!(result["lossBp"], 200.0);
        assert!(result["coverage"].is_null());
        assert_eq!(result["covered"], 1);
        assert_eq!(result["count"], 3);
        assert_eq!(result["fees"]["USD"], 3.0);
        assert_eq!(result["feeCovered"], 1);
    }

    #[test]
    fn historical_summary_preserves_fees_and_nonpositive_rules() {
        let result = historical_summary(json!({"value":-10,"coveredValue":-5,"lossBp":2,
            "coverage":0.5,"estimated":1,"fees":{"USD":3},"feeCovered":1}));
        assert!(result["lossBp"].is_null());
        assert!(result["coverage"].is_null());
        assert_eq!(result["fees"], json!({"USD":3}));
        assert!(result.get("estimated").is_none());
        assert_eq!(empty_summary()["amountComplete"], true);
    }
}
