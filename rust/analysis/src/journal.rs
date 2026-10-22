//! Account journal over page-bounded scalar facts, in the caller's read transaction.

use std::collections::HashMap;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use crate::costs::{Summary, AGGREGATES};

const HEADERS: &str = "SELECT id,created_at AS occurred_at,0 AS kind FROM executerecord
    WHERE account_id=?1 AND (?2 IS NULL OR created_at>=?2) AND (?3 IS NULL OR created_at<?3)
    UNION ALL SELECT id,triggered_at,1 FROM schedule_skip
    WHERE account_id=?1 AND (?2 IS NULL OR triggered_at>=?2) AND (?3 IS NULL OR triggered_at<?3)";
const SYMBOL_FIELDS: [&str; 5] = [
    "status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
];
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
    let since = since.filter(|value| !value.is_empty());
    let until = until.filter(|value| !value.is_empty());
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
        SELECT s.record_id,s.symbol,s.present,s.status,s.error,s.outcome,s.outcome_reason,s.reason_code
        FROM analysis_source_execution_symbol s JOIN selected p ON p.id=s.record_id
        JOIN analysis_source_execution e ON e.record_id=s.record_id
        WHERE e.projection_error IS NULL AND e.has_symbol_results
        ORDER BY s.record_id,s.symbol")).map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let present: i64 = row.get(2).map_err(database)?;
        let mut fields = Map::new();
        for (bit, name) in SYMBOL_FIELDS.iter().enumerate() {
            if present & (1 << bit) != 0 {
                fields.insert(
                    (*name).into(),
                    json!(row.get::<_, Option<String>>(bit + 3).map_err(database)?),
                );
            }
        }
        symbols
            .entry(row.get(0).map_err(database)?)
            .or_default()
            .insert(row.get(1).map_err(database)?, Value::Object(fields));
    }

    let mut fees: HashMap<i64, (Map<String, Value>, i64)> = HashMap::new();
    let mut statement = connection
        .prepare(&format!(
            "{cte}
        SELECT t.record_id,t.fee_currency,SUM(t.fee),COUNT(*) FROM analysis_source_trade t
        JOIN selected p ON p.id=t.record_id
        JOIN analysis_source_execution e ON e.record_id=t.record_id
        WHERE e.projection_error IS NULL AND e.trade_count IS NOT NULL AND t.fee IS NOT NULL AND t.fee_currency IS NOT NULL AND t.fee_currency!=''
        GROUP BY t.record_id,t.fee_currency"
        ))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let entry = fees.entry(row.get(0).map_err(database)?).or_default();
        entry.0.insert(
            row.get(1).map_err(database)?,
            json!(row.get::<_, f64>(2).map_err(database)?),
        );
        entry.1 += row.get::<_, i64>(3).map_err(database)?;
    }
    // Python summarize excludes costs on unvalued fills; the analysis aggregate does not.
    let mut summaries = HashMap::new();
    let mut statement = connection
        .prepare(&format!(
            "{cte},
        trades AS (SELECT t.record_id,t.value,
            CASE WHEN t.value IS NOT NULL THEN t.cost END AS cost,t.loss_bp,t.estimated
            FROM analysis_source_trade t JOIN selected p ON p.id=t.record_id
            JOIN analysis_source_execution e ON e.record_id=t.record_id
            WHERE e.projection_error IS NULL AND e.trade_count IS NOT NULL)
        SELECT record_id,{AGGREGATES} FROM trades GROUP BY record_id"
        ))
        .map_err(database)?;
    let mut rows = statement.query(args).map_err(database)?;
    while let Some(row) = rows.next().map_err(database)? {
        let id: i64 = row.get(0).map_err(database)?;
        let summary = Summary::read(row, 1).map_err(database)?;
        let (currencies, covered) = fees.remove(&id).unwrap_or_default();
        summaries.insert(id, historical_summary(summary.output(currencies, covered)));
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
        r.execution_time,s.record_id,s.projection_error,s.has_symbol_results,s.trade_count,
        p.kind,k.id,k.triggered_at,k.channel,k.calendar_id,k.calendar_day,k.calendar_label,k.reason_code
        FROM page p LEFT JOIN executerecord r ON p.kind=0 AND r.id=p.id
        LEFT JOIN schedule_skip k ON p.kind=1 AND k.id=p.id
        LEFT JOIN analysis_source_execution s ON s.record_id=r.id
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

fn historical_summary(mut summary: Value) -> Value {
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

    #[test]
    fn union_bounds_ties_and_pagination() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT);
            CREATE TABLE schedule_skip(id INTEGER,account_id INTEGER,triggered_at TEXT);
            INSERT INTO executerecord VALUES(1,1,'a'),(2,1,'b'),(9,2,'z');
            INSERT INTO schedule_skip VALUES(2,1,'b'),(3,1,'c');",
        )
        .unwrap();
        let sql = format!("SELECT kind,id FROM ({HEADERS}) ORDER BY occurred_at DESC,id DESC,kind LIMIT ?4 OFFSET ?5");
        let mut stmt = conn.prepare(&sql).unwrap();
        let result = stmt
            .query_map(params![1, "b", "c", 10, 0], |row| {
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
        let summary = conn.query_row(&format!("WITH trades AS
            (SELECT value,CASE WHEN value IS NOT NULL THEN cost END AS cost,loss_bp,estimated FROM trade)
            SELECT {AGGREGATES} FROM trades"), [], |row| Summary::read(row,0)).unwrap();
        let result =
            historical_summary(summary.output(Map::from_iter([("USD".into(), json!(3))]), 1));
        assert_eq!(result["value"], 150.0);
        assert_eq!(result["cost"], 2.0);
        assert_eq!(result["lossBp"], 200.0);
        assert!(result["coverage"].is_null());
        assert_eq!(result["covered"], 1);
        assert_eq!(result["count"], 3);
        assert_eq!(result["fees"]["USD"], 3);
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
