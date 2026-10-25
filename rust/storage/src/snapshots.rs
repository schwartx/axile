//! Published performance results from current structured tables.

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

/// Publication state shared by snapshot, card, view and Python wait readers.
pub fn publication_status(
    snapshot_id: Option<&str>,
    compatible: bool,
    error: Option<&str>,
    active: bool,
    versions_match: bool,
) -> &'static str {
    let has_snapshot = snapshot_id.is_some_and(|id| !id.is_empty());
    if has_snapshot && !compatible {
        "pending"
    } else if error.is_some_and(|error| !error.is_empty()) {
        "failed"
    } else if active || !versions_match {
        if has_snapshot {
            "stale"
        } else if active {
            "pending"
        } else {
            "empty"
        }
    } else if has_snapshot {
        "ready"
    } else {
        "empty"
    }
}

struct Request<'a> {
    account_id: i64,
    range: &'a str,
    logic_version: &'a str,
    engine_version: &'a str,
}

impl<'a> Request<'a> {
    fn parse(value: &'a Value) -> Result<Self, String> {
        let account_id = value
            .get("account_id")
            .and_then(Value::as_i64)
            .filter(|id| *id > 0)
            .ok_or("INVALID_REQUEST: account_id must be a positive integer")?;
        let range = value.get("range").and_then(Value::as_str).unwrap_or("all");
        if !matches!(range, "all" | "30" | "90") {
            return Err("INVALID_REQUEST: range must be all, 30 or 90".into());
        }
        let version = |field| {
            value
                .get(field)
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| format!("INVALID_REQUEST: {field} must be a nonempty string"))
        };
        Ok(Self {
            account_id,
            range,
            logic_version: version("logic_version")?,
            engine_version: version("engine_version")?,
        })
    }
}

struct Snapshot {
    source_version: i64,
    published_version: i64,
    requested: bool,
    running_version: Option<i64>,
    failures: i64,
    retry_at: f64,
    error: Option<String>,
    id: Option<String>,
    version: Option<i64>,
    logic_version: Option<String>,
    engine_version: Option<String>,
    computed_at: Option<String>,
    data_until: Option<String>,
    result_schema_version: i64,
    settings: Value,
    selected: Value,
}

impl Snapshot {
    fn compatible(&self, request: &Request<'_>) -> bool {
        self.has_snapshot()
            && self.logic_version.as_deref() == Some(request.logic_version)
            && self.engine_version.as_deref() == Some(request.engine_version)
    }

    fn has_snapshot(&self) -> bool {
        self.id.as_deref().is_some_and(|id| !id.is_empty())
    }

    fn has_error(&self) -> bool {
        self.error.as_deref().is_some_and(|error| !error.is_empty())
    }

    fn status(&self, request: &Request<'_>) -> &'static str {
        publication_status(
            self.id.as_deref(),
            self.compatible(request),
            self.error.as_deref(),
            self.requested || self.running_version.is_some(),
            self.published_version == self.source_version,
        )
    }
}

fn read(
    connection: &Connection,
    request: &Request<'_>,
    performance_only: bool,
) -> Result<Option<Snapshot>, String> {
    // Read the marker first. The typed path never selects legacy ranges,
    // settings or payload columns, including when those contain poisoned JSON.
    let mut snapshot = connection
        .query_row(
            "SELECT a.source_version, a.published_version, a.requested,
                    a.running_version, a.failures, a.retry_at, a.error,
                    s.id, s.source_version, s.logic_version, s.engine_version,
                    s.computed_at, s.data_until, s.result_schema_version
             FROM account_analysis a
             LEFT JOIN performance_snapshot s ON a.current_snapshot = s.id
             WHERE a.account_id = ?1",
            params![request.account_id],
            |row| {
                Ok(Snapshot {
                    source_version: row.get(0)?,
                    published_version: row.get(1)?,
                    requested: row.get(2)?,
                    running_version: row.get(3)?,
                    failures: row.get(4)?,
                    retry_at: row.get(5)?,
                    error: row.get(6)?,
                    id: row.get(7)?,
                    version: row.get(8)?,
                    logic_version: row.get(9)?,
                    engine_version: row.get(10)?,
                    computed_at: row.get(11)?,
                    data_until: row.get(12)?,
                    result_schema_version: row.get::<_, Option<i64>>(13)?.unwrap_or(0),
                    settings: Value::Null,
                    selected: Value::Null,
                })
            },
        )
        .optional()
        .map_err(|error| format!("SNAPSHOT_READ: {error}"))?;
    if performance_only {
        // The caller already owns a SQLite read snapshot. Reject before reading
        // any result rows or legacy JSON, in that same view as the ready check.
        let status = snapshot.as_ref().map_or("empty", |row| row.status(request));
        if status != "ready" {
            return Err(format!("SNAPSHOT_NOT_READY: {status}"));
        }
    }
    if let Some(row) = snapshot.as_mut().filter(|row| row.compatible(request)) {
        if row.result_schema_version != 2 {
            return Err(format!(
                "SNAPSHOT_SCHEMA_UNSUPPORTED: result schema {}",
                row.result_schema_version
            ));
        }
        let id = row.id.as_deref().expect("compatible snapshots have an id");
        row.selected = crate::result_reader::read(connection, id, request.range, performance_only)?;
        row.settings = row.selected["performance"]["settings"].clone();
    }
    Ok(snapshot)
}

/// Return the existing PerformanceSnapshot envelope, including stale results.
pub fn query(connection: &Connection, value: &Value) -> Result<Value, String> {
    let request = Request::parse(value)?;
    let mut row = read(connection, &request, false)?;
    let status = row.as_ref().map_or("empty", |row| row.status(&request));
    let mut selected = row
        .as_mut()
        .filter(|row| row.compatible(&request))
        .map(|row| row.selected.take())
        .unwrap_or(Value::Null);
    let snapshot = row.as_ref().filter(|row| row.compatible(&request));
    if snapshot.is_some() {
        if !selected.is_object() {
            return Err("SNAPSHOT_INVALID: requested range is missing".into());
        }
    }
    let mut response = json!({
        "status": status,
        "source_version": row.as_ref().map_or(0, |row| row.source_version),
        "snapshot_id": snapshot.and_then(|row| row.id.as_deref()),
        "snapshot_version": snapshot.and_then(|row| row.version),
        "logic_version": snapshot.and_then(|row| row.logic_version.as_deref()).unwrap_or(request.logic_version),
        "engine_version": snapshot.and_then(|row| row.engine_version.as_deref()).unwrap_or(request.engine_version),
        "computed_at": snapshot.and_then(|row| row.computed_at.as_deref()),
        "data_until": snapshot.and_then(|row| row.data_until.as_deref()),
        "settings": snapshot.map(|row| &row.settings),
        "error": row.as_ref().and_then(|row| row.error.as_deref()),
        "retry_at": row.as_ref().filter(|row| row.has_error() && row.failures <= 3).map(|row| row.retry_at),
        "event_count": selected.get("event_count").and_then(Value::as_u64).unwrap_or(0),
    });
    // Move the potentially large arrays into the envelope. json! on these
    // values would serialize by reference and create a second complete tree.
    response["result"] = selected
        .get_mut("performance")
        .map(Value::take)
        .unwrap_or(Value::Null);
    response["daily_costs"] = selected
        .get_mut("daily_costs")
        .map(Value::take)
        .unwrap_or_else(|| json!({}));
    response["events"] = selected
        .get_mut("events")
        .map(Value::take)
        .unwrap_or_else(|| json!([]));
    Ok(response)
}

/// Return a ready AccountPerformance directly, without encoding its envelope.
pub fn performance_result(connection: &Connection, value: &Value) -> Result<Value, String> {
    let request = Request::parse(value)?;
    let row = read(connection, &request, true)?;
    let mut row = row.expect("ready status requires a snapshot row");
    let mut result = row
        .selected
        .get_mut("performance")
        .map(Value::take)
        .unwrap_or(Value::Null);
    if !result.is_object() {
        return Err("SNAPSHOT_INVALID: requested performance range is missing".into());
    }
    if value.get("include_backtest").and_then(Value::as_bool) == Some(false) {
        result["backtest_included"] = json!(false);
        result["skips"] = Value::Null;
        result["used_record_count"] = json!(0);
        if let Some(points) = result.get_mut("points").and_then(Value::as_array_mut) {
            for point in points {
                if let Some(point) = point.as_object_mut() {
                    for field in ["portfolio_return", "portfolio_daily_return", "difference"] {
                        point.insert(field.into(), Value::Null);
                    }
                }
            }
        }
    }
    Ok(result)
}

pub(crate) fn timestamp(value: &str) -> Option<i64> {
    crate::calendar::timestamp_micros(value).ok()
}

/// 批量账户卡片摘要:状态 + 全区间曲线末点,只读取当前结构化结果。
pub fn summaries(connection: &Connection, request: &Value) -> Result<Value, String> {
    let account_ids: Vec<i64> = request
        .get("account_ids")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_i64).collect())
        .ok_or("INVALID_REQUEST: account_ids must be an array of integers")?;
    let expected_logic = request
        .get("logic_version")
        .and_then(Value::as_str)
        .ok_or("INVALID_REQUEST: logic_version must be a nonempty string")?;
    let expected_engine = request
        .get("engine_version")
        .and_then(Value::as_str)
        .ok_or("INVALID_REQUEST: engine_version must be a nonempty string")?;
    summaries_for_accounts(connection, &account_ids, expected_logic, expected_engine)
}

pub(crate) fn summaries_for_accounts(
    connection: &Connection,
    account_ids: &[i64],
    expected_logic: &str,
    expected_engine: &str,
) -> Result<Value, String> {
    if account_ids.is_empty() {
        return Ok(json!({}));
    }
    let placeholders = vec!["?"; account_ids.len()].join(",");
    let sql = format!(
        "SELECT s.account_id,s.source_version,s.published_version,s.requested,s.running_version,s.error,\
p.id,p.computed_at,p.logic_version,p.engine_version,p.result_schema_version \
FROM account_analysis s LEFT JOIN performance_snapshot p ON s.current_snapshot=p.id \
WHERE s.account_id IN ({placeholders})"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|error| format!("SNAPSHOT_READ: {error}"))?;
    let mut summaries = serde_json::Map::new();
    let mut typed_points: std::collections::HashMap<String, Vec<crate::result_reader::Point>> =
        std::collections::HashMap::new();
    // Only compatible published schema2 snapshots need points; one query for all cards.
    let mut points_statement = connection.prepare(&format!(
        "SELECT q.date,q.observed_at,q.record_id,q.execution_id,q.account_return,q.account_equity,
        q.portfolio_return,q.account_daily_return,q.portfolio_daily_return,
        q.target_portfolio_return,q.target_portfolio_daily_return,q.difference,q.sizing_fallback,q.snapshot_id
        FROM performance_result_point q WHERE q.range_key='all' AND q.snapshot_id IN (
        SELECT p.id FROM account_analysis s JOIN performance_snapshot p ON s.current_snapshot=p.id
        WHERE s.account_id IN ({placeholders}) AND p.result_schema_version=2
        AND p.logic_version=? AND p.engine_version=?) ORDER BY q.snapshot_id,q.idx"
    )).map_err(|error| format!("SNAPSHOT_READ: {error}"))?;
    let mut parameters: Vec<rusqlite::types::Value> =
        account_ids.iter().copied().map(Into::into).collect();
    parameters.push(expected_logic.to_owned().into());
    parameters.push(expected_engine.to_owned().into());
    let mut point_rows = points_statement
        .query(rusqlite::params_from_iter(parameters))
        .map_err(|error| format!("SNAPSHOT_READ: {error}"))?;
    while let Some(row) = point_rows
        .next()
        .map_err(|error| format!("SNAPSHOT_READ: {error}"))?
    {
        let id: String = row
            .get(13)
            .map_err(|error| format!("SNAPSHOT_READ: {error}"))?;
        typed_points.entry(id).or_default().push(
            crate::result_reader::Point::from_row(row)
                .map_err(|error| format!("SNAPSHOT_READ: {error}"))?,
        );
    }
    let mut cursor = statement
        .query(rusqlite::params_from_iter(account_ids.iter()))
        .map_err(|error| format!("SNAPSHOT_READ: {error}"))?;
    let read_error = |error: rusqlite::Error| format!("SNAPSHOT_READ: {error}");
    while let Some(row) = cursor.next().map_err(read_error)? {
        let account_id: i64 = row.get(0).map_err(read_error)?;
        let source_version: i64 = row.get(1).map_err(read_error)?;
        let published_version: i64 = row.get(2).map_err(read_error)?;
        let requested: bool = row.get(3).map_err(read_error)?;
        let running_version: Option<i64> = row.get(4).map_err(read_error)?;
        let error: Option<String> = row.get(5).map_err(read_error)?;
        let snapshot_id: Option<String> = row.get(6).map_err(read_error)?;
        let computed_at: Option<String> = row.get(7).map_err(read_error)?;
        let logic: Option<String> = row.get(8).map_err(read_error)?;
        let engine: Option<String> = row.get(9).map_err(read_error)?;
        let schema_version: Option<i64> = row.get(10).map_err(read_error)?;
        let has_snapshot = snapshot_id.as_deref().is_some_and(|id| !id.is_empty());
        let incompatible = has_snapshot
            && (logic.as_deref() != Some(expected_logic)
                || engine.as_deref() != Some(expected_engine));
        let status = publication_status(
            snapshot_id.as_deref(),
            !incompatible,
            error.as_deref(),
            requested || running_version.is_some(),
            published_version == source_version,
        );
        let points = if incompatible || !has_snapshot {
            Value::Array(Vec::new())
        } else {
            if schema_version != Some(2) {
                return Err("SNAPSHOT_SCHEMA_UNSUPPORTED: unsupported result schema".into());
            }
            let points = typed_points
                .get(snapshot_id.as_deref().expect("checked"))
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            serde_json::to_value(points).map_err(|error| format!("SUMMARY_ENCODE: {error}"))?
        };
        let last = points.as_array().and_then(|items| items.last());
        let pick = |field: &str| {
            last.and_then(|point| point.get(field).cloned())
                .unwrap_or(Value::Null)
        };
        summaries.insert(
            account_id.to_string(),
            json!({
                "snapshot_id": if incompatible { Value::Null } else { snapshot_id.clone().map(Value::String).unwrap_or(Value::Null) },
                "status": status,
                "computed_at": if incompatible { Value::Null } else { computed_at.map(Value::String).unwrap_or(Value::Null) },
                "observed_at": pick("observed_at"),
                "account_equity": pick("account_equity"),
                "account_daily_return": pick("account_daily_return"),
                "points": points,
            }),
        );
    }
    Ok(Value::Object(summaries))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        connection
            .execute_batch(
                "CREATE TABLE account_analysis (
                     account_id INTEGER PRIMARY KEY, source_version INTEGER NOT NULL DEFAULT 1,
                     published_version INTEGER NOT NULL DEFAULT 0, requested INTEGER NOT NULL DEFAULT 0,
                     current_snapshot TEXT, running_version INTEGER, failures INTEGER NOT NULL DEFAULT 0,
                     retry_at REAL NOT NULL DEFAULT 0, error TEXT);
                 CREATE TABLE performance_snapshot (
                     id TEXT PRIMARY KEY, account_id INTEGER, source_version INTEGER,
                     logic_version TEXT, engine_version TEXT, computed_at TEXT, data_until TEXT,
                     settings TEXT, ranges TEXT, result_schema_version INTEGER NOT NULL DEFAULT 2,backtest_weight_type TEXT DEFAULT 'cs',backtest_fee_rate REAL DEFAULT 0.0002);
                 ",
            ).unwrap();
        let schema = include_str!("migrations/structured/analysis.sql").replace(
            "sizing_fallback BOOLEAN NOT NULL",
            "sizing_fallback BOOLEAN NOT NULL DEFAULT 0",
        );
        connection
            .execute_batch(
                &schema[schema
                    .find("CREATE TABLE performance_cost_execution")
                    .unwrap()..],
            )
            .unwrap();
        connection
    }

    fn request(range: &str) -> Value {
        json!({"account_id": 1, "range": range, "logic_version": "18", "engine_version": "equity-ledger-1"})
    }

    fn publish(connection: &Connection) {
        connection.execute_batch("INSERT INTO performance_snapshot(id,account_id,source_version,logic_version,engine_version,computed_at)
            VALUES('s1',1,1,'18','equity-ledger-1','2026-10-02');
            INSERT INTO performance_cost_execution(snapshot_id,record_id,time,success,noop,execution_id,created_at,is_success,symbol_count,trade_count,position_count,value,cost,loss_bp,coverage,covered,count,fee_covered,amount_complete,covered_value,estimated,has_transactions)
            VALUES('s1',2,1790812800000,1,0,'e2','2026-10-01T00:00:00',1,0,0,0,NULL,NULL,NULL,NULL,0,0,0,1,0,0,0),
                  ('s1',3,1790870400000,1,0,'e3','2026-10-01T16:00:00Z',1,0,0,0,NULL,NULL,NULL,NULL,0,0,0,1,0,0,0);").unwrap();
        for range in ["30", "90", "all"] {
            connection.execute("INSERT INTO performance_result_range(snapshot_id,range_key,backtest_included,engine_version,baseline,end,record_count,observation_count,used_record_count,target_used_record_count,invalid_asset_count,calendar_status,event_count)
                VALUES('s1',?1,1,'equity-ledger-1','2026-10-01T00:00:00+08:00','2026-10-02T00:00:00+08:00',0,0,9,0,0,'not_required',0)",[range]).unwrap();
            connection.execute("INSERT INTO performance_result_point(snapshot_id,range_key,idx,date,observed_at,account_return,portfolio_return,portfolio_daily_return,difference,sizing_fallback)
                VALUES('s1',?1,0,'2026-10-01','2026-10-01T00:00:00',0.03,0.02,0.01,0.01,0)",[range]).unwrap();
        }
        connection.execute("INSERT INTO account_analysis (account_id, published_version, current_snapshot) VALUES (1, 1, 's1')", []).unwrap();
    }

    #[test]
    fn publication_status_matches_all_readers() {
        for (id, compatible, error, requested, running, versions_match, expected) in [
            (None, true, None, false, None, true, "empty"),
            (None, true, None, false, None, false, "empty"),
            (None, true, None, true, None, false, "pending"),
            (None, true, None, false, Some(0), true, "pending"),
            (None, true, Some("failed"), true, Some(1), false, "failed"),
            (Some(""), false, Some(""), false, None, true, "empty"),
            (Some(""), false, None, true, None, true, "pending"),
            (Some("s1"), true, Some(""), false, None, true, "ready"),
            (Some("s1"), true, None, false, None, false, "stale"),
            (Some("s1"), true, None, true, Some(1), true, "stale"),
            (
                Some("s1"),
                true,
                Some("failed"),
                true,
                Some(1),
                false,
                "failed",
            ),
            (
                Some("s1"),
                false,
                Some("failed"),
                true,
                Some(1),
                false,
                "pending",
            ),
            (Some("s1"), false, None, false, None, true, "pending"),
        ] {
            let connection = database();
            publish(&connection);
            connection.execute_batch(
                "CREATE TABLE account (id INTEGER PRIMARY KEY);
                 INSERT INTO account VALUES (1);
                 ALTER TABLE performance_snapshot ADD COLUMN read_view_schema_version INTEGER DEFAULT 0;"
            ).unwrap();
            connection
                .execute(
                    "UPDATE performance_snapshot SET id=?1, logic_version=?2",
                    params![id.unwrap_or("s1"), if compatible { "18" } else { "old" }],
                )
                .unwrap();
            connection
                .execute(
                    "UPDATE account_analysis SET current_snapshot=?1,error=?2,requested=?3,
                 running_version=?4,published_version=?5",
                    params![
                        id,
                        error,
                        requested,
                        running,
                        if versions_match { 1 } else { 0 }
                    ],
                )
                .unwrap();
            assert_eq!(
                publication_status(
                    id,
                    compatible,
                    error,
                    requested || running.is_some(),
                    versions_match
                ),
                expected
            );
            assert_eq!(
                query(&connection, &request("all")).unwrap()["status"],
                expected
            );
            assert_eq!(
                crate::views::status(&connection, 1, "18", "equity-ledger-1").unwrap()["status"],
                expected
            );
            assert_eq!(summaries(&connection, &json!({"account_ids": [1], "logic_version": "18", "engine_version": "equity-ledger-1"})).unwrap()["1"]["status"], expected);
        }
    }

    #[test]
    fn empty_pending_and_running_states_match_existing_contract() {
        let connection = database();
        let empty = query(&connection, &request("all")).unwrap();
        assert_eq!(empty["status"], "empty");
        assert_eq!(empty["source_version"], 0);
        assert!(empty["result"].is_null());
        assert_eq!(empty["daily_costs"], json!({}));
        connection
            .execute("INSERT INTO account_analysis (account_id) VALUES (1)", [])
            .unwrap();
        assert_eq!(
            query(&connection, &request("all")).unwrap()["status"],
            "empty"
        );
        connection
            .execute("UPDATE account_analysis SET requested = 1", [])
            .unwrap();
        assert_eq!(
            query(&connection, &request("all")).unwrap()["status"],
            "pending"
        );
        connection
            .execute(
                "UPDATE account_analysis SET requested = 0, running_version = 1",
                [],
            )
            .unwrap();
        assert_eq!(
            query(&connection, &request("all")).unwrap()["status"],
            "pending"
        );
    }

    #[test]
    fn stale_and_failed_states_keep_published_result_and_retry_metadata() {
        let connection = database();
        publish(&connection);
        assert_eq!(
            query(&connection, &request("all")).unwrap()["status"],
            "ready"
        );
        connection
            .execute("UPDATE account_analysis SET source_version = 2", [])
            .unwrap();
        let stale = query(&connection, &request("all")).unwrap();
        assert_eq!(stale["status"], "stale");
        assert_eq!(stale["snapshot_version"], 1);
        assert_eq!(stale["source_version"], 2);
        assert!(stale["result"].is_object());
        connection
            .execute(
                "UPDATE account_analysis SET error = 'failed', failures = 3, retry_at = 123",
                [],
            )
            .unwrap();
        let failed = query(&connection, &request("all")).unwrap();
        assert_eq!(failed["status"], "failed");
        assert_eq!(failed["retry_at"], 123.0);
        connection
            .execute("UPDATE account_analysis SET failures = 4", [])
            .unwrap();
        assert!(query(&connection, &request("all")).unwrap()["retry_at"].is_null());
    }

    #[test]
    fn incompatible_versions_hide_old_result_even_when_failed() {
        let connection = database();
        publish(&connection);
        connection
            .execute(
                "UPDATE account_analysis SET error = 'old error', failures = 1",
                [],
            )
            .unwrap();
        connection
            .execute(
                "UPDATE performance_snapshot SET logic_version = '17', ranges = 'not JSON'",
                [],
            )
            .unwrap();
        let snapshot = query(&connection, &request("all")).unwrap();
        assert_eq!(snapshot["status"], "pending");
        assert!(snapshot["snapshot_id"].is_null());
        assert!(snapshot["result"].is_null());
        assert_eq!(snapshot["logic_version"], "18");
        assert_eq!(snapshot["error"], "old error");
    }

    #[test]
    fn ready_result_can_omit_backtest_without_changing_account_returns() {
        let connection = database();
        publish(&connection);
        let mut input = request("30");
        input["include_backtest"] = json!(false);
        let result = performance_result(&connection, &input).unwrap();
        assert_eq!(result["backtest_included"], false);
        assert_eq!(result["used_record_count"], 0);
        assert!(result["skips"].is_null());
        assert!(result["points"][0]["portfolio_return"].is_null());
        assert_eq!(result["points"][0]["account_return"], 0.03);
        assert_eq!(result["executions"].as_array().unwrap().len(), 2);
        connection
            .execute("UPDATE account_analysis SET requested = 1", [])
            .unwrap();
        assert_eq!(
            performance_result(&connection, &input).unwrap_err(),
            "SNAPSHOT_NOT_READY: stale"
        );
    }

    #[test]
    fn unready_performance_never_reads_legacy_or_typed_results() {
        for schema in [0, 2, 99] {
            let connection = database();
            publish(&connection);
            // Typed tables deliberately do not exist. Legacy JSON is poisoned.
            connection
                .execute(
                    "UPDATE performance_snapshot SET result_schema_version = ?1,
                         settings = 'invalid JSON', ranges = 'invalid JSON'",
                    params![schema],
                )
                .unwrap();
            for (mutation, status) in [
                ("source_version = 2", "stale"),
                ("published_version = 0", "stale"),
                ("requested = 1", "stale"),
                ("running_version = 1", "stale"),
                ("error = 'failed'", "failed"),
                ("current_snapshot = NULL, requested = 1", "pending"),
                ("current_snapshot = NULL", "empty"),
            ] {
                connection
                    .execute_batch(&format!(
                        "UPDATE account_analysis SET source_version = 1,
                             published_version = 1, requested = 0,
                             running_version = NULL, error = NULL, current_snapshot = 's1';
                         UPDATE account_analysis SET {mutation};"
                    ))
                    .unwrap();
                let expected = format!("SNAPSHOT_NOT_READY: {status}");
                assert_eq!(
                    performance_result(&connection, &request("all")).unwrap_err(),
                    expected
                );
            }
        }
    }

    #[test]
    fn ready_performance_still_reports_corrupt_results() {
        let connection = database();
        publish(&connection);
        connection
            .execute("UPDATE performance_result_point SET date = x'ff'", [])
            .unwrap();
        assert!(performance_result(&connection, &request("all"))
            .unwrap_err()
            .starts_with("SNAPSHOT_READ:"));
    }

    #[test]
    fn performance_readiness_and_result_share_one_sqlite_view() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "axile-performance-read-{}-{nonce}.sqlite",
            std::process::id()
        ));
        let writer = database();
        // Copy the small fixture to a file without needing another test dependency.
        writer
            .execute("VACUUM INTO ?1", params![path.to_str().unwrap()])
            .unwrap();
        drop(writer);
        let writer = Connection::open(&path).unwrap();
        writer.execute_batch("PRAGMA journal_mode = WAL;").unwrap();
        publish(&writer);
        let reader = Connection::open(&path).unwrap();
        reader.execute_batch("BEGIN DEFERRED;").unwrap();
        let source: i64 = reader
            .query_row("SELECT source_version FROM account_analysis", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(source, 1);
        writer
            .execute_batch(
                "UPDATE account_analysis SET source_version = 2;
                 UPDATE performance_result_point SET date = x'ff';",
            )
            .unwrap();
        assert_eq!(
            performance_result(&reader, &request("30")).unwrap()["points"][0]["account_return"],
            0.03
        );
        reader.execute_batch("ROLLBACK;").unwrap();
        assert_eq!(
            performance_result(&reader, &request("30")).unwrap_err(),
            "SNAPSHOT_NOT_READY: stale"
        );
        drop(reader);
        drop(writer);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn malformed_requests_are_rejected_before_reading() {
        let connection = Connection::open_in_memory().unwrap();
        assert!(query(&connection, &json!({"account_id": 0}))
            .unwrap_err()
            .starts_with("INVALID_REQUEST:"));
        let mut input = request("invalid");
        assert!(query(&connection, &input)
            .unwrap_err()
            .starts_with("INVALID_REQUEST:"));
        input["range"] = json!("all");
        input["logic_version"] = json!(null);
        assert!(query(&connection, &input)
            .unwrap_err()
            .starts_with("INVALID_REQUEST:"));
    }

    #[test]
    fn summaries_preserve_request_validation_before_empty_accounts() {
        let connection = database();
        for ids in [json!([]), json!([null, true, "1", 1.5])] {
            let mut request = json!({"account_ids": ids});
            assert_eq!(
                summaries(&connection, &request).unwrap_err(),
                "INVALID_REQUEST: logic_version must be a nonempty string"
            );
            request["logic_version"] = json!("");
            assert_eq!(
                summaries(&connection, &request).unwrap_err(),
                "INVALID_REQUEST: engine_version must be a nonempty string"
            );
            request["engine_version"] = json!("");
            assert_eq!(summaries(&connection, &request).unwrap(), json!({}));
        }
        assert_eq!(
            summaries(&connection, &json!({"account_ids": 1})).unwrap_err(),
            "INVALID_REQUEST: account_ids must be an array of integers"
        );
    }

    #[test]
    fn summaries_batch_typed_points_in_index_order() {
        let connection = database();
        connection.execute_batch("INSERT INTO account_analysis
            (account_id,current_snapshot,published_version) VALUES (1,'a',1),(2,'b',1),(3,'c',1),(4,'d',1);
            INSERT INTO performance_snapshot (id,account_id,logic_version,engine_version,result_schema_version)
            VALUES ('a',1,'18','e',2),('b',2,'18','e',2),('c',3,'18','e',2),('d',4,'old','e',2);
            INSERT INTO performance_result_point
            (snapshot_id,range_key,idx,date,observed_at,account_equity) VALUES
            ('a','all',2,'later','t2',20),('b','all',0,'other','tb',99),
            ('a','all',1,'earlier','t1',10),('a','month',3,'excluded','x',999),
            ('d','all',0,'incompatible','x',999);").unwrap();
        let result = summaries(
            &connection,
            &json!({"account_ids":[1,2,3,4],
            "logic_version":"18","engine_version":"e"}),
        )
        .unwrap();
        assert_eq!(
            result,
            summaries_for_accounts(&connection, &[1, 2, 3, 4], "18", "e").unwrap()
        );
        assert_eq!(result, summaries(&connection, &json!({"account_ids":[1,null,"2",2,true,3,4,1.5], "logic_version":"18", "engine_version":"e"})).unwrap());
        assert_eq!(result["1"]["points"][0]["account_equity"], 10.0);
        assert_eq!(result["1"]["account_equity"], 20.0);
        assert_eq!(result["2"]["account_equity"], 99.0);
        assert_eq!(result["3"]["points"], json!([]));
        assert_eq!(result["3"]["account_equity"], Value::Null);
        assert_eq!(result["4"]["points"], json!([]));
        assert_eq!(result["4"]["status"], "pending");
    }

    #[test]
    fn summaries_match_card_status_and_last_point() {
        let connection = database();
        connection
            .execute_batch(
                "INSERT INTO account_analysis (account_id, source_version, published_version, requested, current_snapshot)
                 VALUES (1,3,3,0,'s1'),(2,3,1,0,'s2'),(3,3,3,0,'s3'),(4,3,3,0,NULL),(5,3,3,0,'s5');
                 INSERT INTO performance_snapshot (id,account_id,source_version,logic_version,engine_version,
                  computed_at,settings,ranges,result_schema_version) VALUES
                 ('s1',1,3,'18','equity-ledger-1','2026-10-02T00:00:00','{}',
                  '{\"all\":{\"performance\":{\"points\":[{\"observed_at\":\"t1\",\"account_equity\":100.0,\"account_daily_return\":0.01}]}}}',2),
                 ('s2',2,1,'18','equity-ledger-1','2026-10-02T00:00:00','{}','{}',2),
                 ('s3',3,3,'17','equity-ledger-1','2026-10-02T00:00:00','{}','{}',2),
                 ('s5',5,3,'18','old-engine','2026-10-02T00:00:00','{}','{}',2);",
            )
            .unwrap();
        connection.execute("INSERT INTO performance_result_point(snapshot_id,range_key,idx,date,observed_at,account_equity,account_daily_return,sizing_fallback) VALUES('s1','all',0,'day','t1',100,0.01,0)",[]).unwrap();
        let result = summaries(
            &connection,
            &json!({"account_ids": [1, 2, 3, 4, 5, 6], "logic_version": "18", "engine_version": "equity-ledger-1"}),
        )
        .unwrap();
        assert_eq!(result["1"]["status"], "ready");
        assert_eq!(result["1"]["snapshot_id"], "s1");
        assert_eq!(result["1"]["account_equity"], 100.0);
        assert_eq!(result["1"]["account_daily_return"], 0.01);
        assert_eq!(result["1"]["points"].as_array().unwrap().len(), 1);
        assert_eq!(result["2"]["status"], "stale");
        assert_eq!(result["3"]["status"], "pending");
        assert_eq!(result["3"]["snapshot_id"], Value::Null);
        assert_eq!(result["4"]["status"], "empty");
        assert_eq!(result["5"]["status"], "pending");
        assert!(result.get("6").is_none());
        assert_eq!(
            result,
            summaries_for_accounts(&connection, &[1, 2, 3, 4, 5, 6], "18", "equity-ledger-1")
                .unwrap()
        );
    }
}
