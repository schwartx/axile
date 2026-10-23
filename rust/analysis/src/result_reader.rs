//! Reconstruct one published result from typed SQLite columns.
//!
//! Intermediate history never crosses the language boundary. These allocations
//! are the requested public response, not a second copy of persisted JSON.

use rusqlite::{params, Connection, Row};
use serde::Serialize;
use serde_json::{json, Map, Value};

fn sql_error(error: rusqlite::Error) -> String {
    format!("SNAPSHOT_READ: {error}")
}

/// Settings have scalar storage in schema v2; legacy payloads are never read.
pub(crate) fn settings(connection: &Connection, snapshot_id: &str) -> Result<Value, String> {
    let (weight_type, fee_rate): (String, f64) = connection
        .query_row(
            "SELECT backtest_weight_type, backtest_fee_rate FROM performance_snapshot WHERE id=?1",
            [snapshot_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(sql_error)?;
    if !matches!(weight_type.as_str(), "ts" | "cs")
        || !fee_rate.is_finite()
        || !(0.0..1.0).contains(&fee_rate)
    {
        return Err("SNAPSHOT_INVALID: invalid normalized performance settings".into());
    }
    // PerformanceSettings accepts historical `ts`, then normalizes it to `cs`.
    Ok(json!({"backtest_weight_type": "cs", "backtest_fee_rate": fee_rate}))
}

fn rows(
    connection: &Connection,
    sql: &str,
    snapshot_id: &str,
    range: &str,
    mut project: impl FnMut(&Row<'_>) -> rusqlite::Result<Value>,
) -> Result<Vec<Value>, String> {
    let mut statement = connection.prepare(sql).map_err(sql_error)?;
    let mut cursor = statement
        .query(params![snapshot_id, range])
        .map_err(sql_error)?;
    let mut result = Vec::new();
    while let Some(row) = cursor.next().map_err(sql_error)? {
        result.push(project(row).map_err(sql_error)?);
    }
    Ok(result)
}

struct Header {
    performance: Value,
    baseline: Option<String>,
    end: Option<String>,
    event_count: i64,
}

fn header(connection: &Connection, snapshot_id: &str, range: &str) -> Result<Header, String> {
    connection
        .query_row(
            "SELECT backtest_included,engine_version,baseline,end,
                record_count,observation_count,used_record_count,target_used_record_count,
                invalid_asset_count,sizing_fallback_count,calendar_status,calendar_id,
                calendar_label,event_count
         FROM performance_result_range WHERE snapshot_id=?1 AND range_key=?2",
            params![snapshot_id, range],
            |row| {
                let baseline: Option<String> = row.get(2)?;
                let end: Option<String> = row.get(3)?;
                Ok(Header {
                    performance: json!({
                        "range": range,
                        "backtest_included": row.get::<_, bool>(0)?,
                        "engine_version": row.get::<_, String>(1)?,
                        "baseline": baseline,
                        "end": end,
                        "record_count": row.get::<_, i64>(4)?,
                        "observation_count": row.get::<_, i64>(5)?,
                        "used_record_count": row.get::<_, i64>(6)?,
                        "target_used_record_count": row.get::<_, i64>(7)?,
                        "invalid_asset_count": row.get::<_, i64>(8)?,
                        "sizing_fallback_count": row.get::<_, Option<i64>>(9)?,
                        "skips": null,
                        "target_skips": null,
                        "calendar": {
                            "status": row.get::<_, String>(10)?,
                            "calendar_id": row.get::<_, Option<String>>(11)?,
                            "label": row.get::<_, Option<String>>(12)?,
                            "closed_ranges": [],
                            "unavailable_ranges": [],
                        },
                    }),
                    baseline,
                    end,
                    event_count: row.get(13)?,
                })
            },
        )
        .map_err(sql_error)
}

// Field order matches serde_json::Map's sorted wire order, including nulls.
#[derive(Serialize)]
pub(crate) struct Point {
    pub(crate) account_daily_return: Option<f64>,
    pub(crate) account_equity: Option<f64>,
    pub(crate) account_return: Option<f64>,
    pub(crate) date: String,
    pub(crate) difference: Option<f64>,
    pub(crate) execution_id: Option<String>,
    pub(crate) observed_at: String,
    pub(crate) portfolio_daily_return: Option<f64>,
    pub(crate) portfolio_return: Option<f64>,
    pub(crate) record_id: Option<i64>,
    pub(crate) sizing_fallback: bool,
    pub(crate) target_portfolio_daily_return: Option<f64>,
    pub(crate) target_portfolio_return: Option<f64>,
}

impl Point {
    pub(crate) fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            date: row.get(0)?,
            observed_at: row.get(1)?,
            record_id: row.get(2)?,
            execution_id: row.get(3)?,
            account_return: row.get(4)?,
            account_equity: row.get(5)?,
            portfolio_return: row.get(6)?,
            account_daily_return: row.get(7)?,
            portfolio_daily_return: row.get(8)?,
            target_portfolio_return: row.get(9)?,
            target_portfolio_daily_return: row.get(10)?,
            difference: row.get(11)?,
            sizing_fallback: row.get(12)?,
        })
    }
}

pub(crate) fn typed_points(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
) -> Result<Vec<Point>, String> {
    let mut statement = connection
        .prepare(
            "SELECT date,observed_at,record_id,execution_id,account_return,account_equity,
                portfolio_return,account_daily_return,portfolio_daily_return,
                target_portfolio_return,target_portfolio_daily_return,difference,sizing_fallback
         FROM performance_result_point WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
        )
        .map_err(sql_error)?;
    let mut cursor = statement
        .query(params![snapshot_id, range])
        .map_err(sql_error)?;
    let mut result = Vec::new();
    while let Some(row) = cursor.next().map_err(sql_error)? {
        result.push(Point::from_row(row).map_err(sql_error)?);
    }
    Ok(result)
}

fn points(connection: &Connection, snapshot_id: &str, range: &str) -> Result<Vec<Value>, String> {
    typed_points(connection, snapshot_id, range)?
        .into_iter()
        .map(|point| serde_json::to_value(point).map_err(|error| format!("SNAPSHOT_READ: {error}")))
        .collect()
}

fn attach_skips(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
    performance: &mut Value,
) -> Result<(), String> {
    let mut statement = connection
        .prepare(
            "SELECT kind,count,missing_target,missing_ticks,first_time,last_time
         FROM performance_result_skip WHERE snapshot_id=?1 AND range_key=?2",
        )
        .map_err(sql_error)?;
    let mut cursor = statement
        .query(params![snapshot_id, range])
        .map_err(sql_error)?;
    while let Some(row) = cursor.next().map_err(sql_error)? {
        let kind: String = row.get(0).map_err(sql_error)?;
        let field = match kind.as_str() {
            "portfolio" => "skips",
            "target" => "target_skips",
            _ => return Err("SNAPSHOT_INVALID: unknown skip kind".into()),
        };
        performance[field] = json!({
            "count": row.get::<_, i64>(1).map_err(sql_error)?,
            "missing_target": row.get::<_, i64>(2).map_err(sql_error)?,
            "missing_ticks": row.get::<_, i64>(3).map_err(sql_error)?,
            "first_time": row.get::<_, Option<String>>(4).map_err(sql_error)?,
            "last_time": row.get::<_, Option<String>>(5).map_err(sql_error)?,
        });
    }
    Ok(())
}

fn attach_calendar(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
    performance: &mut Value,
) -> Result<(), String> {
    let mut statement = connection
        .prepare(
            "SELECT kind,start,end FROM performance_result_calendar_range
         WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
        )
        .map_err(sql_error)?;
    let mut cursor = statement
        .query(params![snapshot_id, range])
        .map_err(sql_error)?;
    while let Some(row) = cursor.next().map_err(sql_error)? {
        let kind: String = row.get(0).map_err(sql_error)?;
        let field = match kind.as_str() {
            "closed" => "closed_ranges",
            "unavailable" => "unavailable_ranges",
            _ => return Err("SNAPSHOT_INVALID: unknown calendar range kind".into()),
        };
        performance["calendar"][field]
            .as_array_mut()
            .expect("calendar array")
            .push(json!({
                "start": row.get::<_, String>(1).map_err(sql_error)?,
                "end": row.get::<_, String>(2).map_err(sql_error)?,
            }));
    }
    Ok(())
}

fn daily_costs(connection: &Connection, snapshot_id: &str, range: &str) -> Result<Value, String> {
    // Keep the writer's presence mask: old summaries do not acquire optional
    // coveredValue/estimated keys merely because their SQL columns exist.
    let fields = [
        "value",
        "cost",
        "lossBp",
        "coverage",
        "covered",
        "count",
        "amountComplete",
        "feeCovered",
        "coveredValue",
        "estimated",
    ];
    let mut statement = connection
        .prepare(
            "SELECT day,present,value,cost,loss_bp,coverage,covered,count,
                amount_complete,fee_covered,covered_value,estimated
         FROM performance_result_daily_cost WHERE snapshot_id=?1 AND range_key=?2 ORDER BY day",
        )
        .map_err(sql_error)?;
    let mut cursor = statement
        .query(params![snapshot_id, range])
        .map_err(sql_error)?;
    let mut daily = Map::new();
    while let Some(row) = cursor.next().map_err(sql_error)? {
        let day: String = row.get(0).map_err(sql_error)?;
        let present: i64 = row.get(1).map_err(sql_error)?;
        let mut summary = Map::new();
        for (index, field) in fields.iter().enumerate() {
            if present & (1 << index) == 0 {
                continue;
            }
            let value = match index {
                4 | 5 | 7 | 9 => json!(row.get::<_, Option<i64>>(index + 2).map_err(sql_error)?),
                6 => json!(row.get::<_, Option<bool>>(index + 2).map_err(sql_error)?),
                _ => json!(row.get::<_, Option<f64>>(index + 2).map_err(sql_error)?),
            };
            summary.insert((*field).into(), value);
        }
        summary.insert("fees".into(), json!({}));
        daily.insert(day, Value::Object(summary));
    }
    drop(cursor);
    drop(statement);
    let mut statement = connection
        .prepare(
            "SELECT day,currency,value FROM performance_result_daily_fee
         WHERE snapshot_id=?1 AND range_key=?2 ORDER BY day,currency",
        )
        .map_err(sql_error)?;
    let mut cursor = statement
        .query(params![snapshot_id, range])
        .map_err(sql_error)?;
    while let Some(row) = cursor.next().map_err(sql_error)? {
        let day: String = row.get(0).map_err(sql_error)?;
        let currency: String = row.get(1).map_err(sql_error)?;
        let value: f64 = row.get(2).map_err(sql_error)?;
        let summary = daily
            .get_mut(&day)
            .ok_or("SNAPSHOT_INVALID: daily fee has no cost summary")?;
        summary["fees"][currency] = json!(value);
    }
    Ok(Value::Object(daily))
}

/// Match historical timestamps: naive values mean Shanghai and facts use ms.
fn timestamp(value: &str) -> Option<f64> {
    // Python's datetime.timestamp() rounds to floating seconds before * 1000;
    // retain that order so inclusive bounds match stored fractional-ms facts.
    crate::snapshots::timestamp(value).map(|micros| micros as f64 / 1_000_000.0 * 1000.0)
}

/// Return a selected range wrapper without reading serialized legacy payloads.
pub(crate) fn read(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
    performance_only: bool,
) -> Result<Value, String> {
    read_mode(connection, snapshot_id, range, performance_only, None)
}

pub(crate) fn read_typed(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
    performance_only: bool,
) -> Result<(Value, Vec<Point>), String> {
    let mut points = Vec::new();
    let value = read_mode(
        connection,
        snapshot_id,
        range,
        performance_only,
        Some(&mut points),
    )?;
    Ok((value, points))
}

fn read_mode(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
    performance_only: bool,
    typed: Option<&mut Vec<Point>>,
) -> Result<Value, String> {
    let Header {
        mut performance,
        baseline,
        end,
        event_count,
    } = header(connection, snapshot_id, range)?;
    performance["settings"] = settings(connection, snapshot_id)?;
    performance["points"] = match typed {
        Some(output) => {
            *output = typed_points(connection, snapshot_id, range)?;
            json!([])
        }
        None => Value::Array(points(connection, snapshot_id, range)?),
    };
    attach_skips(connection, snapshot_id, range, &mut performance)?;
    attach_calendar(connection, snapshot_id, range, &mut performance)?;
    performance["sizing_fallback_ranges"] = Value::Array(rows(
        connection,
        "SELECT start,end,count FROM performance_result_fallback
         WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
        snapshot_id,
        range,
        |row| {
            Ok(
                json!({"start": row.get::<_, String>(0)?, "end": row.get::<_, String>(1)?,
            "count": row.get::<_, i64>(2)?}),
            )
        },
    )?);
    performance["bindings"] = Value::Array(rows(
        connection,
        "SELECT time,portfolio_id FROM performance_result_binding
         WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
        snapshot_id,
        range,
        |row| {
            Ok(
                json!({"time": row.get::<_, String>(0)?, "portfolio_id": row.get::<_, Option<i64>>(1)?}),
            )
        },
    )?);
    let executions = match (baseline.as_deref(), end.as_deref()) {
        (Some(start), Some(end)) => {
            let start = timestamp(start).ok_or("SNAPSHOT_INVALID: invalid baseline timestamp")?;
            let end = timestamp(end).ok_or("SNAPSHOT_INVALID: invalid end timestamp")?;
            crate::costs::snapshot_executions(connection, snapshot_id, start, end)?
        }
        _ => Vec::new(),
    };
    performance["executions"] = Value::Array(executions);
    let mut result = json!({});
    result["performance"] = performance;
    if !performance_only {
        result["daily_costs"] = daily_costs(connection, snapshot_id, range)?;
        result["events"] = Value::Array(rows(
            connection,
            "SELECT time,tag,text,execution_id FROM performance_result_event
             WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx LIMIT 100",
            snapshot_id,
            range,
            |row| {
                Ok(
                    json!({"time": row.get::<_, String>(0)?, "tag": row.get::<_, String>(1)?,
                "text": row.get::<_, String>(2)?, "executionId": row.get::<_, Option<String>>(3)?}),
                )
            },
        )?);
        result["event_count"] = json!(event_count);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE performance_snapshot (id TEXT PRIMARY KEY, backtest_weight_type TEXT,
                backtest_fee_rate REAL, settings TEXT, ranges TEXT);
             CREATE TABLE performance_result_range (snapshot_id TEXT,range_key TEXT,
                backtest_included INTEGER,engine_version TEXT,baseline TEXT,end TEXT,
                record_count INTEGER,observation_count INTEGER,used_record_count INTEGER,
                target_used_record_count INTEGER,invalid_asset_count INTEGER,sizing_fallback_count INTEGER,
                calendar_status TEXT,calendar_id TEXT,calendar_label TEXT,event_count INTEGER);
             CREATE TABLE performance_result_point (snapshot_id TEXT,range_key TEXT,idx INTEGER,
                date TEXT,observed_at TEXT,record_id INTEGER,execution_id TEXT,account_return REAL,
                account_equity REAL,portfolio_return REAL,account_daily_return REAL,
                portfolio_daily_return REAL,target_portfolio_return REAL,
                target_portfolio_daily_return REAL,difference REAL,sizing_fallback INTEGER);
             CREATE TABLE performance_result_skip (snapshot_id TEXT,range_key TEXT,kind TEXT,
                count INTEGER,missing_target INTEGER,missing_ticks INTEGER,first_time TEXT,last_time TEXT);
             CREATE TABLE performance_result_fallback (snapshot_id TEXT,range_key TEXT,idx INTEGER,
                start TEXT,end TEXT,count INTEGER);
             CREATE TABLE performance_result_binding (snapshot_id TEXT,range_key TEXT,idx INTEGER,
                time TEXT,portfolio_id INTEGER);
             CREATE TABLE performance_result_calendar_range (snapshot_id TEXT,range_key TEXT,
                kind TEXT,idx INTEGER,start TEXT,end TEXT);
             CREATE TABLE performance_result_event (snapshot_id TEXT,range_key TEXT,idx INTEGER,
                time TEXT,tag TEXT,text TEXT,execution_id TEXT);
             CREATE TABLE performance_result_daily_cost (snapshot_id TEXT,range_key TEXT,day TEXT,
                present INTEGER,value REAL,cost REAL,loss_bp REAL,coverage REAL,covered INTEGER,
                count INTEGER,amount_complete INTEGER,fee_covered INTEGER,covered_value REAL,estimated INTEGER);
             CREATE TABLE performance_result_daily_fee (snapshot_id TEXT,range_key TEXT,day TEXT,
                currency TEXT,value REAL);
             INSERT INTO performance_snapshot VALUES ('s','ts',0.0002,'poison settings','poison ranges');
             INSERT INTO performance_result_range VALUES ('s','all',1,'ledger',NULL,NULL,0,0,0,0,0,NULL,
                'partial',NULL,'上海',201);
             INSERT INTO performance_result_point VALUES ('s','all',2,'2026-10-02','later',NULL,NULL,
                NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,0);
             INSERT INTO performance_result_point VALUES ('s','all',1,'2026-10-01','first',1,'e',
                0.1,10,0.2,0.1,0.2,NULL,NULL,-0.1,1);
             INSERT INTO performance_result_skip VALUES ('s','all','target',3,2,1,NULL,'last');
             INSERT INTO performance_result_fallback VALUES ('s','all',0,'begin','end',2);
             INSERT INTO performance_result_binding VALUES ('s','all',0,'at',NULL);
             INSERT INTO performance_result_calendar_range VALUES ('s','all','closed',0,'a','b');
             INSERT INTO performance_result_calendar_range VALUES ('s','all','unavailable',0,'c','d');
             INSERT INTO performance_result_event VALUES ('s','all',0,'now','trade','done',NULL);
             INSERT INTO performance_result_daily_cost VALUES ('s','all','2026-10-01',255,
                10,1,1000,0.5,1,2,0,1,5,1);
             INSERT INTO performance_result_daily_cost VALUES ('s','all','2026-10-02',1023,
                NULL,NULL,NULL,NULL,0,0,1,0,0,0);
             INSERT INTO performance_result_daily_fee VALUES ('s','all','2026-10-01','USDT',0.02);"
        ).unwrap();
        connection
    }

    #[test]
    fn typed_results_preserve_defaults_nulls_presence_masks_and_order() {
        let connection = database();
        let result = read(&connection, "s", "all", false).unwrap();
        let performance = &result["performance"];
        assert_eq!(performance["settings"]["backtest_weight_type"], "cs");
        assert_eq!(performance["points"][0]["record_id"], 1);
        assert_eq!(performance["points"][0]["sizing_fallback"], true);
        assert_eq!(performance["points"][1]["sizing_fallback"], false);
        assert!(performance["points"][1]["account_return"].is_null());
        assert!(performance["points"][1]["record_id"].is_null());
        assert!(performance["sizing_fallback_count"].is_null());
        assert!(performance["skips"].is_null());
        assert_eq!(performance["target_skips"]["count"], 3);
        assert_eq!(performance["executions"], json!([]));
        assert_eq!(performance["bindings"][0]["portfolio_id"], Value::Null);
        assert_eq!(
            performance["calendar"]["closed_ranges"],
            json!([{"start":"a","end":"b"}])
        );
        assert_eq!(
            performance["calendar"]["unavailable_ranges"],
            json!([{"start":"c","end":"d"}])
        );
        assert_eq!(result["event_count"], 201);
        assert!(result["events"][0]["executionId"].is_null());
        let daily = &result["daily_costs"]["2026-10-01"];
        assert_eq!(daily["amountComplete"], false);
        assert_eq!(daily["covered"], 1);
        assert_eq!(daily["fees"]["USDT"], 0.02);
        assert!(daily.get("coveredValue").is_none());
        assert!(daily.get("estimated").is_none());
        let empty = &result["daily_costs"]["2026-10-02"];
        assert!(empty.get("value").unwrap().is_null());
        assert_eq!(empty["amountComplete"], true);
        assert_eq!(empty["coveredValue"], 0.0);
        assert_eq!(empty["estimated"], 0);
        assert_eq!(empty["fees"], json!({}));
    }

    #[test]
    fn summary_points_read_multiple_snapshots_and_selected_range() {
        // 从 Python 私有 reader 用例迁移，保留缺失快照、缺失范围及空请求契约。
        let connection = database();
        connection.execute_batch(
            "UPDATE performance_snapshot SET id='snapshot-a';
             INSERT INTO performance_snapshot VALUES
                ('snapshot-b','cs',0.0002,'poison settings','poison ranges');
             DELETE FROM performance_result_point;
             INSERT INTO performance_result_point VALUES
                ('snapshot-a','all',1,'2026-01-02','2026-01-02T09:00:00',NULL,NULL,
                 0.03,103,-0.02,0.03,-0.02,NULL,NULL,0.05,1),
                ('snapshot-a','all',0,'2026-01-01','2026-01-01T09:00:00',7,'execution-7',
                 0,100,0,NULL,NULL,0,NULL,0,0);
             INSERT INTO performance_result_point
                SELECT snapshot_id,'90',0,date,observed_at,record_id,execution_id,
                    account_return,account_equity,portfolio_return,account_daily_return,
                    portfolio_daily_return,target_portfolio_return,target_portfolio_daily_return,
                    difference,sizing_fallback
                FROM performance_result_point WHERE snapshot_id='snapshot-a' AND range_key='all' AND idx=1;"
        ).unwrap();
        let expected = json!([
            {
                "date": "2026-01-01", "observed_at": "2026-01-01T09:00:00",
                "record_id": 7, "execution_id": "execution-7",
                "account_return": 0.0, "account_equity": 100.0, "portfolio_return": 0.0,
                "account_daily_return": null, "portfolio_daily_return": null,
                "target_portfolio_return": 0.0, "target_portfolio_daily_return": null,
                "sizing_fallback": false, "difference": 0.0,
            },
            {
                "date": "2026-01-02", "observed_at": "2026-01-02T09:00:00",
                "record_id": null, "execution_id": null,
                "account_return": 0.03, "account_equity": 103.0, "portfolio_return": -0.02,
                "account_daily_return": 0.03, "portfolio_daily_return": -0.02,
                "target_portfolio_return": null, "target_portfolio_daily_return": null,
                "sizing_fallback": true, "difference": 0.05,
            }
        ]);
        let read = |ids: &[&str], range: &str| {
            Value::Object(
                ids.iter()
                    .map(|id| {
                        (
                            (*id).to_owned(),
                            Value::Array(points(&connection, id, range).unwrap()),
                        )
                    })
                    .collect(),
            )
        };
        assert_eq!(
            read(&["snapshot-a", "snapshot-b", "missing"], "all"),
            json!({"snapshot-a": expected, "snapshot-b": [], "missing": []})
        );
        assert_eq!(
            read(&["snapshot-a", "snapshot-b"], "90"),
            json!({"snapshot-a": [expected[1]], "snapshot-b": []})
        );
        assert_eq!(read(&[], "all"), json!({}));
    }

    #[test]
    fn performance_only_does_not_require_cost_or_event_tables() {
        let connection = database();
        connection
            .execute_batch(
                "DROP TABLE performance_result_daily_cost;
            DROP TABLE performance_result_daily_fee; DROP TABLE performance_result_event;",
            )
            .unwrap();
        let result = read(&connection, "s", "all", true).unwrap();
        assert!(result["performance"].is_object());
        assert_eq!(result.as_object().unwrap().len(), 1);
    }

    #[test]
    fn typed_wire_points_preserve_exact_bytes_and_close_sqlite_before_output() {
        let connection = database();
        connection.execute(
            "UPDATE performance_result_point SET date=?1,observed_at=?2,account_return=?3 WHERE idx=1",
            params!["上海\"\\\n", "time\tvalue", -0.0_f64],
        ).unwrap();
        let expected = read(&connection, "s", "all", false).unwrap()["performance"].take();
        let (mut value, points) = read_typed(&connection, "s", "all", false).unwrap();
        let wire = crate::wire_result::WireResult {
            value: value["performance"].take(),
            points: Some(points),
            performance_only: true,
        };
        drop(connection);
        assert_eq!(
            serde_json::to_vec(&wire).unwrap(),
            serde_json::to_vec(&expected).unwrap()
        );
        let mut empty = wire;
        empty.points.as_mut().unwrap().clear();
        let parsed: Value = serde_json::from_slice(&serde_json::to_vec(&empty).unwrap()).unwrap();
        assert_eq!(parsed["points"], json!([]));
    }

    #[test]
    fn naive_and_explicit_shanghai_timestamps_match_in_milliseconds() {
        let expected = timestamp("2026-10-01T00:00:00.123456+08:00").unwrap();
        assert_eq!(timestamp("2026-10-01T00:00:00.123456"), Some(expected));
        assert_eq!(timestamp("2026-09-30T16:00:00.123456Z"), Some(expected));
        assert_eq!(
            timestamp("2026-10-01"),
            timestamp("2026-10-01T00:00:00+08:00")
        );
        assert!(timestamp("invalid").is_none());
        // The extra rounding to seconds is observable at inclusive end bounds.
        assert_eq!(
            timestamp("2026-10-01T00:00:00.000003+08:00"),
            Some(1_790_784_000_000.003_2)
        );
        assert_eq!(
            timestamp("2026-10-01T00:00:00.000003"),
            Some(1_790_784_000_000.003_2)
        );
    }
}
