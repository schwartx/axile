//! 在一个只读快照中发现执行、核验所有权并展开冻结证据。

use std::collections::HashSet;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{DateTime, NaiveDate, NaiveDateTime, Offset, TimeZone};
use chrono_tz::Tz;
use rusqlite::{params, types::Value as SqlValue, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::summary_refs;
use crate::control_audit::{context_kind, legacy_source};
use crate::{asset_ops, audit_ops, business_detail, error::AnalysisError, storage_connection};

const PAGE_SIZE: usize = 500;
const SOURCES: &[(&str, &str)] = &[
    ("execution_intent", "intent"),
    ("executerecord", "record"),
    ("execution_event", "events"),
    ("execution_artifact", "artifacts"),
    ("account_control_event", "controls"),
    ("account_control_counter_delta", "counters"),
    ("account_control_session", "sessions"),
];
const FACTS: &[(&str, &str)] = &[
    ("execution_input", "id"),
    ("execution_input_target", "input_id"),
    ("execution_input_symbol", "input_id"),
    ("execution_input_plan", "input_id"),
    ("execution_termination_cancel_failure", "record_id"),
    ("execution_symbol_collection", "record_id"),
    ("execution_symbol_result", "record_id"),
    ("execution_symbol_sizing", "record_id"),
    ("execution_symbol_conversion", "record_id"),
    ("execution_symbol_tick", "record_id"),
    ("execution_symbol_target", "record_id"),
    ("execution_order_collection", "record_id"),
    ("execution_order", "record_id"),
    ("business_trade_collection", "record_id"),
    ("business_trade", "record_id"),
    ("business_trade_conversion", "record_id"),
    ("execution_extension", "record_id"),
];
const RECORD_PRIVATE: &[&str] = &[
    "facts_complete",
    "result_input_kind",
    "result_input_value",
    "audit_evidence",
    "termination_evidence",
    "termination_kind",
    "termination_cancel_ids_kind",
    "symbol",
    "technical_detail",
    "termination_cancel_count",
];

fn error(code: &str, message: &str) -> AnalysisError {
    AnalysisError::new(code, message)
}
fn invalid(_: impl ToString) -> AnalysisError {
    error("EVIDENCE_INVALID_DATA", "执行业务事实不完整或引用冲突")
}
fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("EVIDENCE_READ", error.to_string())
}
fn too_large() -> AnalysisError {
    error("EVIDENCE_TOO_LARGE", "证据量超过导出上限，请缩小范围")
}
fn too_many_executions() -> AnalysisError {
    error(
        "EVIDENCE_TOO_MANY_EXECUTIONS",
        "执行数量超过导出上限，请按执行清单分批导出",
    )
}
fn add_count(counts: &mut Map<String, Value>, table: &str, rows: u64) {
    let count = counts.entry(table.to_owned()).or_insert(json!(0));
    *count = json!(count.as_u64().unwrap_or(0) + rows);
}
fn text<'a>(row: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    row.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(key))
}
fn integer(row: &Value, key: &str) -> Result<i64, AnalysisError> {
    row.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(key))
}
fn json_argument(values: &Value) -> SqlValue {
    SqlValue::Text(values.to_string())
}
fn in_json(column: &str) -> String {
    format!("{column} IN (SELECT value FROM json_each(?))")
}

struct Budget<'a> {
    rows: u64,
    bytes: u64,
    max_rows: u64,
    max_bytes: u64,
    max_executions: usize,
    started: Instant,
    duration: Duration,
    cancelled: &'a AtomicBool,
}
impl<'a> Budget<'a> {
    fn new(request: &Value, cancelled: &'a AtomicBool) -> Result<Self, AnalysisError> {
        let limits = &request["limits"];
        let seconds = limits["seconds"]
            .as_f64()
            .filter(|value| value.is_finite() && *value > 0.0)
            .ok_or_else(|| error("EVIDENCE_TIMEOUT", "证据导出超时，请缩小范围"))?;
        Ok(Self {
            rows: 0,
            bytes: 0,
            max_rows: limits["rows"].as_u64().ok_or_else(too_large)?,
            max_bytes: limits["bytes"].as_u64().ok_or_else(too_large)?,
            max_executions: limits["executions"].as_u64().ok_or_else(too_large)? as usize,
            started: Instant::now(),
            duration: Duration::try_from_secs_f64(seconds).map_err(invalid)?,
            cancelled,
        })
    }
    fn check(&mut self, rows: u64, bytes: u64) -> Result<(), AnalysisError> {
        self.rows = self.rows.saturating_add(rows);
        self.bytes = self.bytes.saturating_add(bytes);
        self.preflight(0, 0)
    }
    fn preflight(&self, rows: u64, bytes: u64) -> Result<(), AnalysisError> {
        if self.cancelled.load(Ordering::Relaxed) {
            return Err(error("EVIDENCE_CANCELLED", "证据导出已取消"));
        }
        if self.started.elapsed() > self.duration {
            return Err(error("EVIDENCE_TIMEOUT", "证据导出超时，请缩小范围"));
        }
        if self.rows > self.max_rows
            || self.bytes > self.max_bytes
            || rows > self.max_rows.saturating_sub(self.rows)
            || bytes > self.max_bytes.saturating_sub(self.bytes)
        {
            Err(too_large())
        } else {
            Ok(())
        }
    }
    fn assembled(&mut self, value: &Value) -> Result<(), AnalysisError> {
        // Python json.dumps 的默认分隔符在逗号和冒号后各有一个空格。
        fn spaces(value: &Value) -> usize {
            match value {
                Value::Object(map) => {
                    map.len().saturating_sub(1)
                        + map.len()
                        + map.values().map(spaces).sum::<usize>()
                }
                Value::Array(items) => {
                    items.len().saturating_sub(1) + items.iter().map(spaces).sum::<usize>()
                }
                _ => 0,
            }
        }
        self.check(
            0,
            (business_detail::wire(value).map_err(invalid)?.len() + spaces(value)) as u64,
        )
    }
}

fn raw_size(connection: &Connection, table: &str) -> Result<String, AnalysisError> {
    let mut statement = connection
        .prepare(&format!("PRAGMA table_info(\"{table}\")"))
        .map_err(sql)?;
    let names = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    if names.is_empty() {
        return Err(invalid("证据表不存在"));
    }
    Ok(names
        .iter()
        .map(|name| {
            format!(
                "coalesce(length(CAST(\"{}\" AS BLOB)),0)",
                name.replace('"', "\"\"")
            )
        })
        .collect::<Vec<_>>()
        .join("+"))
}

fn decode_source(row: &rusqlite::Row<'_>, table: &str) -> Result<Value, AnalysisError> {
    if table != "executerecord" {
        return audit_ops::decode_row(row, table).map_err(invalid);
    }
    let mut decoded = business_detail::decode(row, table).map_err(invalid)?;
    for field in RECORD_PRIVATE {
        decoded.shift_remove(*field);
    }
    Ok(Value::Object(decoded))
}

fn pages(
    connection: &Connection,
    table: &str,
    predicate: &str,
    arguments: &[SqlValue],
    budget: &mut Budget,
) -> Result<Vec<Value>, AnalysisError> {
    let size = raw_size(connection, table)?;
    let mut result = Vec::new();
    let mut last_id = 0;
    loop {
        budget.check(0, 0)?;
        let mut arguments = arguments.to_vec();
        arguments.push(SqlValue::Integer(last_id));
        let mut statement = connection.prepare(&format!(
            "SELECT id,{size} FROM {table} WHERE ({predicate}) AND id>? ORDER BY id LIMIT {PAGE_SIZE}"
        )).map_err(sql)?;
        let page = statement
            .query_map(rusqlite::params_from_iter(&arguments), |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, u64>(1)?))
            })
            .map_err(sql)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sql)?;
        if page.is_empty() {
            break;
        }
        budget.preflight(page.len() as u64, page.iter().map(|(_, size)| size).sum())?;
        last_id = page.last().expect("nonempty").0;
        let ids = json!(page.iter().map(|(id, _)| id).collect::<Vec<_>>());
        let mut statement = connection
            .prepare(&format!(
                "SELECT * FROM {table} WHERE {} ORDER BY id",
                in_json("id")
            ))
            .map_err(sql)?;
        let mut cursor = statement.query([json_argument(&ids)]).map_err(sql)?;
        while let Some(row) = cursor.next().map_err(sql)? {
            let value = decode_source(row, table)?;
            budget.check(
                1,
                if table == "account_control_event" {
                    1
                } else {
                    0
                },
            )?;
            budget.assembled(&value)?;
            result.push(value);
        }
        if page.len() < PAGE_SIZE {
            break;
        }
    }
    Ok(result)
}

fn preflight(
    connection: &Connection,
    table: &str,
    predicate: &str,
    arguments: &[SqlValue],
    budget: &mut Budget,
    counts: &mut Map<String, Value>,
) -> Result<(), AnalysisError> {
    budget.check(0, 0)?;
    let size = raw_size(connection, table)?;
    let (rows, bytes): (u64, u64) = connection
        .query_row(
            &format!("SELECT count(*),coalesce(sum({size}),0) FROM {table} WHERE {predicate}"),
            rusqlite::params_from_iter(arguments),
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(sql)?;
    budget.check(rows, bytes)?;
    add_count(counts, table, rows);
    Ok(())
}

struct Window<'a> {
    request: &'a Value,
    start: f64,
    end: f64,
    zone: Option<Tz>,
}
impl<'a> Window<'a> {
    fn new(request: &'a Value) -> Result<Self, AnalysisError> {
        let zone = request
            .get("legacy_timezone")
            .and_then(Value::as_str)
            .map(str::parse::<Tz>)
            .transpose()
            .map_err(invalid)?;
        Ok(Self {
            request,
            start: request["start_ms"]
                .as_f64()
                .ok_or_else(|| invalid("start"))?,
            end: request["end_ms"].as_f64().ok_or_else(|| invalid("end"))?,
            zone,
        })
    }
    fn time(&self, value: &Value) -> Option<f64> {
        let raw = value.as_str()?;
        let normalized = raw.replacen(' ', "T", 1);
        if let Ok(parsed) = DateTime::parse_from_rfc3339(&normalized) {
            return Some(parsed.timestamp_micros() as f64 / 1000.0);
        }
        let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H"]
            .iter()
            .find_map(|pattern| NaiveDateTime::parse_from_str(&normalized, pattern).ok())
            .or_else(|| {
                NaiveDate::parse_from_str(&normalized, "%Y-%m-%d")
                    .ok()?
                    .and_hms_opt(0, 0, 0)
            })?;
        let offset = if let Some(zone) = self.zone {
            // fold=0 采用较早一侧;跳时缺口沿用跳时前的 offset。
            let offset = zone
                .offset_from_local_datetime(&naive)
                .earliest()
                .or_else(|| {
                    (1..=24).find_map(|hours| {
                        zone.offset_from_local_datetime(&(naive - chrono::Duration::hours(hours)))
                            .earliest()
                    })
                })?;
            offset.fix().local_minus_utc() as i64
        } else {
            let key = naive.format("%Y-%m-%dT%H:%M:%S").to_string();
            self.request["local_offsets"]
                .as_array()?
                .iter()
                .rev()
                .find(|row| {
                    row[0]
                        .as_str()
                        .is_some_and(|boundary| boundary <= key.as_str())
                })?[1]
                .as_i64()?
        };
        Some(naive.and_utc().timestamp_micros() as f64 / 1000.0 - offset as f64 * 1000.0)
    }
    fn contains(&self, value: &Value) -> Option<bool> {
        self.time(value)
            .map(|time| self.start <= time && time < self.end)
    }
    fn coarse(&self, column: &str) -> Result<(String, Vec<SqlValue>), AnalysisError> {
        Ok((
            format!("(({column}>=? AND {column}<?) OR julianday({column}) IS NULL)"),
            vec![
                SqlValue::Text(text(self.request, "coarse_start")?.to_owned()),
                SqlValue::Text(text(self.request, "coarse_end")?.to_owned()),
            ],
        ))
    }
}

fn append_unique(list: &mut Value, value: Value) {
    let items = list.as_array_mut().expect("context list");
    if !items.contains(&value) {
        items.push(value);
    }
}

struct Snapshot {
    account: Value,
    executions: Map<String, Value>,
    operations: Map<String, Value>,
    counts: Map<String, Value>,
}
impl Snapshot {
    fn new(account: Value) -> Self {
        Self {
            account,
            executions: Map::new(),
            operations: Map::new(),
            counts: SOURCES
                .iter()
                .map(|(table, _)| ((*table).to_owned(), json!(0)))
                .collect(),
        }
    }
    fn select(
        &mut self,
        key: &str,
        execution_id: Value,
        reason: &str,
        kind: &str,
        source: &str,
        budget: &Budget,
    ) -> Result<(), AnalysisError> {
        let kind = context_kind(kind, key);
        let operation = kind == "account_operation" && !self.executions.contains_key(key);
        if !operation && !self.executions.contains_key(key) {
            if self.executions.len() >= budget.max_executions {
                return Err(too_many_executions());
            }
            if let Some(item) = self.operations.shift_remove(key) {
                self.executions.insert(key.to_owned(), item);
            }
        }
        let selected = if operation {
            &mut self.operations
        } else {
            &mut self.executions
        };
        let item = selected.entry(key.to_owned()).or_insert_with(|| {
            json!({"key":key,"execution_id":execution_id,
                "selection_reasons":[],"input_id":null,"input":null,"asset_observations":[],
                "intent":null,"record":null,"events":[],"artifacts":[],"controls":[],
                "counters":[],"sessions":[],"context_kind":kind,"sources":[]})
        });
        if item["execution_id"] != execution_id {
            return Err(error(
                "EVIDENCE_IDENTIFIER_CONFLICT",
                "历史记录与执行标识冲突",
            ));
        }
        if !operation && kind == "execution" {
            item["context_kind"] = json!(kind);
        }
        append_unique(&mut item["selection_reasons"], json!(reason));
        append_unique(&mut item["sources"], json!(source));
        Ok(())
    }
    fn select_control(
        &mut self,
        row: &Value,
        reason: &str,
        budget: &Budget,
    ) -> Result<(), AnalysisError> {
        let key = text(row, "execution_id")?;
        let kind = row["context_kind"].as_str().unwrap_or("unknown");
        let mut source = row["source"].as_str().unwrap_or("unknown");
        if source == "unknown" && kind == "unknown" {
            source = legacy_source(key).unwrap_or(source);
        }
        self.select(key, json!(key), reason, kind, source, budget)
    }
    fn item_mut(&mut self, key: &str) -> Option<&mut Value> {
        self.executions
            .get_mut(key)
            .or_else(|| self.operations.get_mut(key))
    }
    fn classify(&mut self, budget: &Budget) -> Result<(), AnalysisError> {
        let keys = self
            .executions
            .keys()
            .chain(self.operations.keys())
            .cloned()
            .collect::<Vec<_>>();
        for key in keys {
            let item = self.item_mut(&key).expect("known context");
            let business = !item["intent"].is_null()
                || !item["record"].is_null()
                || !item["input_id"].is_null()
                || !item["events"].as_array().expect("events").is_empty()
                || !item["artifacts"].as_array().expect("artifacts").is_empty();
            let rows = ["controls", "counters", "sessions"]
                .iter()
                .flat_map(|field| item[*field].as_array().expect("control rows").iter());
            let kind = rows
                .clone()
                .map(|row| context_kind(row["context_kind"].as_str().unwrap_or("unknown"), &key))
                .reduce(|left, right| if left == right { left } else { "unknown" })
                .unwrap_or("unknown");
            let sources = rows
                .map(|row| row.get("source").cloned().unwrap_or(json!("unknown")))
                .collect::<Vec<_>>();
            let operation = !business && kind == "account_operation";
            item["context_kind"] = json!(if business { "execution" } else { kind });
            for source in sources {
                append_unique(&mut item["sources"], source);
            }
            if operation {
                if let Some(item) = self.executions.shift_remove(&key) {
                    self.operations.insert(key, item);
                }
            } else if let Some(item) = self.operations.shift_remove(&key) {
                self.executions.insert(key, item);
            }
        }
        if self.executions.len() > budget.max_executions {
            return Err(too_many_executions());
        }
        Ok(())
    }
}

fn owner(connection: &Connection, execution_id: &str) -> Result<i64, AnalysisError> {
    let mut owners = HashSet::new();
    for (table, _) in SOURCES
        .iter()
        .filter(|(table, _)| *table != "execution_artifact")
    {
        let mut statement = connection
            .prepare(&format!(
                "SELECT DISTINCT account_id FROM {table} WHERE execution_id=?1 LIMIT 2"
            ))
            .map_err(sql)?;
        for account_id in statement
            .query_map([execution_id], |row| row.get::<_, i64>(0))
            .map_err(sql)?
        {
            owners.insert(account_id.map_err(sql)?);
        }
    }
    if owners.is_empty() {
        return Err(error("EVIDENCE_NOT_FOUND", "执行不存在或无法确定所属账户"));
    }
    if owners.len() != 1 {
        return Err(error("EVIDENCE_OWNER_CONFLICT", "执行材料的账户归属冲突"));
    }
    Ok(*owners.iter().next().expect("one owner"))
}

fn discover(
    connection: &Connection,
    snapshot: &mut Snapshot,
    window: &Window<'_>,
    budget: &mut Budget,
) -> Result<(), AnalysisError> {
    let account = SqlValue::Integer(integer(&snapshot.account, "id")?);
    let start = SqlValue::Text(text(window.request, "coarse_start")?.to_owned());
    let end = SqlValue::Text(text(window.request, "coarse_end")?.to_owned());
    let intents = pages(
        connection,
        "execution_intent",
        "account_id=? AND (created_at<? OR julianday(created_at) IS NULL) AND
         (finished_at IS NULL OR finished_at>=? OR julianday(finished_at) IS NULL)",
        &[account.clone(), end, start],
        budget,
    )?;
    for row in intents {
        let start = window.time(&row["created_at"]);
        let finish = window.time(&row["finished_at"]);
        let reason = if start.is_none() || (!row["finished_at"].is_null() && finish.is_none()) {
            "intent_time_unknown"
        } else if start.is_some_and(|start| start < window.end)
            && finish.map_or(true, |finish| finish >= window.start)
        {
            "intent_overlaps_window"
        } else {
            continue;
        };
        snapshot.select(
            text(&row, "execution_id")?,
            row["execution_id"].clone(),
            reason,
            "execution",
            "unknown",
            budget,
        )?;
    }
    for (table, column, reason) in [
        ("executerecord", "created_at", "record_created_in_window"),
        (
            "execution_event",
            "ts_local_created",
            "execution_event_in_window",
        ),
    ] {
        let (predicate, mut args) = window.coarse(column)?;
        args.insert(0, account.clone());
        for row in pages(
            connection,
            table,
            &format!("account_id=? AND {predicate}"),
            &args,
            budget,
        )? {
            let inside = window.contains(&row[column]);
            if inside == Some(false) {
                continue;
            }
            let key = row["execution_id"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or(format!("legacy-record-{}", integer(&row, "id")?));
            snapshot.select(
                &key,
                row["execution_id"].clone(),
                if inside == Some(true) {
                    reason
                } else {
                    "record_time_unknown"
                },
                "execution",
                "unknown",
                budget,
            )?;
            if table == "executerecord" && row["execution_id"].is_null() {
                snapshot.item_mut(&key).expect("selected legacy record")["record"] = row;
            }
        }
    }
    for row in pages(
        connection,
        "account_control_event",
        "account_id=? AND occurred_at_ms>=? AND occurred_at_ms<?",
        &[
            account.clone(),
            SqlValue::Real(window.start),
            SqlValue::Real(window.end),
        ],
        budget,
    )? {
        snapshot.select_control(&row, "control_event_in_window", budget)?;
    }
    for row in pages(
        connection,
        "account_control_session",
        "account_id=? AND started_at_ms<? AND (finished_at_ms IS NULL OR finished_at_ms>=?)",
        &[
            account.clone(),
            SqlValue::Real(window.end),
            SqlValue::Real(window.start),
        ],
        budget,
    )? {
        snapshot.select_control(&row, "control_session_overlaps_window", budget)?;
    }
    let (predicate, mut args) = window.coarse("bucket_start")?;
    args.insert(0, account);
    for row in pages(
        connection,
        "account_control_counter_delta",
        &format!("account_id=? AND {predicate}"),
        &args,
        budget,
    )? {
        let start = window.time(&row["bucket_start"]);
        let duration = if row["bucket_type"] == "day" {
            86_400_000.0
        } else {
            60_000.0
        };
        if start.map_or(true, |start| {
            start < window.end && start + duration > window.start
        }) {
            snapshot.select_control(&row, "counter_bucket_context_time_assumed", budget)?;
        }
    }
    Ok(())
}

fn load_sources(
    connection: &Connection,
    snapshot: &mut Snapshot,
    budget: &mut Budget,
) -> Result<(), AnalysisError> {
    let account_id = integer(&snapshot.account, "id")?;
    let ids = snapshot
        .executions
        .values()
        .chain(snapshot.operations.values())
        .filter_map(|item| item["execution_id"].as_str().map(str::to_owned))
        .collect::<Vec<_>>();
    for batch in ids.chunks(PAGE_SIZE) {
        let selected = json_argument(&json!(batch));
        for (table, _) in SOURCES
            .iter()
            .filter(|(table, _)| *table != "execution_artifact")
        {
            let conflict: bool = connection
                .query_row(
                    &format!(
                        "SELECT EXISTS(SELECT 1 FROM {table} WHERE {} AND account_id!=?)",
                        in_json("execution_id")
                    ),
                    params![selected, account_id],
                    |row| row.get(0),
                )
                .map_err(sql)?;
            if conflict {
                return Err(error("EVIDENCE_OWNER_CONFLICT", "执行材料的账户归属冲突"));
            }
        }
        for (table, field) in SOURCES {
            let mut predicate = in_json("execution_id");
            let mut args = vec![selected.clone()];
            if *table != "execution_artifact" {
                predicate.push_str(" AND account_id=?");
                args.push(SqlValue::Integer(account_id));
            }
            let rows = pages(connection, table, &predicate, &args, budget)?;
            add_count(&mut snapshot.counts, table, rows.len() as u64);
            for row in rows {
                let item = snapshot
                    .item_mut(text(&row, "execution_id")?)
                    .ok_or_else(|| invalid("context"))?;
                if *field == "intent" || *field == "record" {
                    item[*field] = row;
                } else {
                    item[*field].as_array_mut().expect("source list").push(row);
                }
            }
        }
    }
    snapshot.classify(budget)
}

fn references(item: &Value) -> Result<Vec<&Map<String, Value>>, AnalysisError> {
    let artifacts = item["artifacts"]
        .as_array()
        .ok_or_else(|| invalid("artifacts"))?;
    let events = item["events"].as_array().ok_or_else(|| invalid("events"))?;
    artifacts
        .iter()
        .map(|artifact| &artifact["content"])
        .chain(events.iter().filter_map(|event| {
            event["details"]
                .get("decision")
                .filter(|value| value.is_object())
        }))
        .filter_map(|content| {
            summary_refs::parse_artifact_reference(content)
                .map_err(invalid)
                .transpose()
        })
        .collect()
}

fn preflight_facts(
    connection: &Connection,
    snapshot: &mut Snapshot,
    budget: &mut Budget,
    record_ids: &[i64],
    input_ids: &HashSet<String>,
    asset_ids: &HashSet<String>,
) -> Result<(), AnalysisError> {
    let records = json_argument(&json!(record_ids));
    let inputs = json_argument(&json!(input_ids));
    let assets = json_argument(&json!(asset_ids));
    // 所有权检查只读取标量身份,先于任何引用资产的大列读取。
    preflight(
        connection,
        "business_asset_binding",
        &in_json("record_id"),
        &[records.clone()],
        budget,
        &mut snapshot.counts,
    )?;
    for id in record_ids {
        asset_ops::validate_result_asset_ownership(connection, *id).map_err(invalid)?;
    }
    for (table, key) in FACTS {
        let args = if *key == "record_id" {
            &records
        } else {
            &inputs
        };
        if (*key == "record_id" && record_ids.is_empty())
            || (*key != "record_id" && input_ids.is_empty())
        {
            continue;
        }
        preflight(
            connection,
            table,
            &in_json(key),
            &[args.clone()],
            budget,
            &mut snapshot.counts,
        )?;
    }
    let asset_scope = format!(
        "{} OR asset_id IN (SELECT asset_id FROM business_asset_binding WHERE {}) OR {} OR {}",
        in_json("record_id"),
        in_json("record_id"),
        in_json("input_id"),
        in_json("asset_id")
    );
    let args = [records.clone(), records.clone(), inputs, assets.clone()];
    preflight(
        connection,
        "business_asset_snapshot",
        &asset_scope,
        &args,
        budget,
        &mut snapshot.counts,
    )?;
    preflight(
        connection,
        "business_asset_position",
        &format!("asset_id IN (SELECT asset_id FROM business_asset_snapshot WHERE {asset_scope})"),
        &args,
        budget,
        &mut snapshot.counts,
    )?;
    preflight(
        connection,
        "business_asset_binding",
        &format!("{} AND NOT ({})", in_json("asset_id"), in_json("record_id")),
        &[assets, records.clone()],
        budget,
        &mut snapshot.counts,
    )?;
    Ok(())
}

fn hydrate(
    connection: &Connection,
    snapshot: &mut Snapshot,
    budget: &mut Budget,
) -> Result<(), AnalysisError> {
    let account_id = integer(&snapshot.account, "id")?;
    let mut record_ids = Vec::new();
    let mut input_ids = HashSet::new();
    let mut asset_ids = HashSet::new();
    for item in snapshot
        .executions
        .values_mut()
        .chain(snapshot.operations.values_mut())
    {
        if !item["record"].is_null() {
            record_ids.push(integer(&item["record"], "id")?);
            if integer(&item["record"], "account_id")? != account_id {
                return Err(invalid("owner"));
            }
            item["input_id"] = item["record"]["input_id"].clone();
            for key in ["input_id", "result_input_id"] {
                if let Some(id) = item["record"][key].as_str() {
                    input_ids.insert(id.to_owned());
                }
            }
        } else if let Some(execution_id) = item["execution_id"].as_str() {
            let found=connection.query_row("SELECT id,account_id,owner_execution_id FROM execution_input WHERE execution_id=?1",
                [execution_id],|row|Ok((row.get::<_,String>(0)?,row.get::<_,i64>(1)?,row.get::<_,Option<String>>(2)?)))
                .optional().map_err(sql)?;
            if let Some((id, owner, execution)) = found {
                if owner != account_id
                    || execution.as_deref().unwrap_or(execution_id) != execution_id
                {
                    return Err(invalid("input owner"));
                }
                item["input_id"] = json!(id);
                input_ids.insert(id);
            }
        }
        for reference in references(item)? {
            if let Some(id) = reference.get("input_id").and_then(Value::as_str) {
                input_ids.insert(id.to_owned());
            }
            if let Some(id) = reference.get("asset_id").and_then(Value::as_str) {
                asset_ids.insert(id.to_owned());
            }
        }
    }
    preflight_facts(
        connection,
        snapshot,
        budget,
        &record_ids,
        &input_ids,
        &asset_ids,
    )?;
    for item in snapshot
        .executions
        .values_mut()
        .chain(snapshot.operations.values_mut())
    {
        if !item["record"].is_null() {
            let mut detail =
                business_detail::read(connection, account_id, integer(&item["record"], "id")?)
                    .map_err(invalid)?;
            for key in [
                "id",
                "account_id",
                "execution_id",
                "created_at",
                "is_success",
            ] {
                detail[key] = item["record"][key].clone();
            }
            item["input"] = detail["input"].clone();
            item["record"] = detail;
            budget.assembled(&item["record"])?;
        } else if let Some(id) = item["input_id"].as_str() {
            item["input"] = summary_refs::read_owned_input(
                connection,
                id,
                &item["execution_id"],
                Some(account_id),
            )
            .map_err(invalid)?;
            budget.assembled(&item["input"])?;
        }
        if let Some(id) = item["input_id"].as_str() {
            let mut observations =
                asset_ops::read_input_asset_observations(connection, id).map_err(invalid)?;
            for row in observations
                .as_array_mut()
                .ok_or_else(|| invalid("asset observations"))?
            {
                let path = row
                    .as_object_mut()
                    .ok_or_else(|| invalid("asset observation"))?
                    .shift_remove("path")
                    .ok_or_else(|| invalid("path"))?;
                row["input_id"] = json!(id);
                row["fact_path"] = path;
            }
            item["asset_observations"] = observations;
            budget.assembled(&item["asset_observations"])?;
        }
        for artifact in item["artifacts"]
            .as_array_mut()
            .ok_or_else(|| invalid("artifacts"))?
        {
            if summary_refs::parse_artifact_reference(&artifact["content"])
                .map_err(invalid)?
                .is_some()
            {
                artifact["content"] =
                    summary_refs::resolve_artifact_content(connection, artifact, Some(account_id))
                        .map_err(invalid)?;
                budget.assembled(&artifact["content"])?;
            }
        }
        for event in item["events"]
            .as_array_mut()
            .ok_or_else(|| invalid("events"))?
        {
            let resolved = summary_refs::resolve_event_details(connection, event, Some(account_id))
                .map_err(invalid)?;
            if summary_refs::parse_artifact_reference(
                event["details"].get("decision").unwrap_or(&Value::Null),
            )
            .map_err(invalid)?
            .is_some()
            {
                budget.assembled(&resolved)?;
            }
            event["details"] = resolved;
        }
    }
    snapshot.classify(budget)
}

fn collect(
    connection: &Connection,
    request: &Value,
    budget: &mut Budget,
) -> Result<Snapshot, AnalysisError> {
    let execution_id = request.get("execution_id").filter(|value| !value.is_null());
    let explicit = request
        .get("execution_ids")
        .filter(|value| !value.is_null());
    let window = request.get("window").filter(|value| !value.is_null());
    let mut account_id = request.get("account_id").and_then(Value::as_i64);
    if let Some(ids) = explicit {
        let ids = ids
            .as_array()
            .ok_or_else(|| error("EVIDENCE_INVALID_SELECTION", "执行清单须为不重复的非空标识"))?;
        if execution_id.is_some() || window.is_some() || account_id.is_none() {
            return Err(error(
                "EVIDENCE_INVALID_SELECTION",
                "执行清单须绑定账户且不能与其他选择混用",
            ));
        }
        if ids.is_empty() || ids.len() > budget.max_executions {
            return Err(error(
                "EVIDENCE_TOO_MANY_EXECUTIONS",
                "执行清单超过导出上限",
            ));
        }
        let keys = ids.iter().filter_map(Value::as_str).collect::<HashSet<_>>();
        if keys.len() != ids.len() || keys.iter().any(|id| id.trim().is_empty()) {
            return Err(error(
                "EVIDENCE_INVALID_SELECTION",
                "执行清单须为不重复的非空标识",
            ));
        }
    }
    if let Some(id) = execution_id {
        account_id = Some(owner(
            connection,
            id.as_str().ok_or_else(|| invalid("execution id"))?,
        )?);
    }
    let account=connection.query_row("SELECT id,name,trade_channel FROM account WHERE id=?1",[account_id],|row|
        Ok(json!({"id":row.get::<_,i64>(0)?,"name":row.get::<_,String>(1)?,"channel":row.get::<_,String>(2)?})))
        .optional().map_err(sql)?.ok_or_else(||error("EVIDENCE_ACCOUNT_NOT_FOUND","账户不存在"))?;
    let mut snapshot = Snapshot::new(account);
    if let Some(id) = execution_id {
        snapshot.select(
            id.as_str().expect("checked"),
            id.clone(),
            "explicit_execution",
            "execution",
            "unknown",
            budget,
        )?;
    } else if let Some(ids) = explicit.and_then(Value::as_array) {
        for id in ids {
            budget.check(0, 0)?;
            let key = id.as_str().expect("checked");
            if Some(owner(connection, key)?) != account_id {
                return Err(error("EVIDENCE_NOT_FOUND", "执行不存在或不属于所选账户"));
            }
            snapshot.select(
                key,
                id.clone(),
                "explicit_execution_ids",
                "execution",
                "unknown",
                budget,
            )?;
        }
    } else {
        discover(
            connection,
            &mut snapshot,
            &Window::new(
                window.ok_or_else(|| error("EVIDENCE_INVALID_SELECTION", "缺少导出选择范围"))?,
            )?,
            budget,
        )?;
    }
    load_sources(connection, &mut snapshot, budget)?;
    let legacy_count = snapshot
        .executions
        .values()
        .filter(|item| item["execution_id"].is_null())
        .count();
    add_count(&mut snapshot.counts, "executerecord", legacy_count as u64);
    hydrate(connection, &mut snapshot, budget)?;
    budget.check(0, 0)?;
    Ok(snapshot)
}

fn install_progress(connection: &Connection, budget: &Budget, cancelled: &Arc<AtomicBool>) {
    let cancelled = Arc::clone(cancelled);
    let started = budget.started;
    let duration = budget.duration;
    connection.progress_handler(
        10_000,
        Some(move || cancelled.load(Ordering::Relaxed) || started.elapsed() > duration),
    );
}

pub fn collect_evidence_with_control(
    path: &str,
    request: &Value,
    cancelled: &Arc<AtomicBool>,
) -> Result<Value, AnalysisError> {
    let mut budget = Budget::new(request, cancelled.as_ref())?;
    budget.check(0, 0)?;
    let connection = storage_connection::open_reader(Path::new(path))?;
    install_progress(&connection, &budget, cancelled);
    connection.execute_batch("BEGIN").map_err(sql)?;
    let result = collect(&connection, request, &mut budget);
    connection.progress_handler(0, None::<fn() -> bool>);
    let control = budget.check(0, 0);
    let rollback = connection.execute_batch("ROLLBACK").map_err(sql);
    control?;
    let snapshot = result?;
    rollback?;
    Ok(
        json!({"account":snapshot.account,"executions":snapshot.executions,
        "account_operations":snapshot.operations,"source_counts":snapshot.counts,
        "rows":budget.rows,"bytes":budget.bytes}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    fn test_budget() -> Budget<'static> {
        static CANCELLED: AtomicBool = AtomicBool::new(false);
        Budget::new(
            &json!({"limits":{"rows":10000,"bytes":1000000,"executions":1,"seconds":5}}),
            &CANCELLED,
        )
        .unwrap()
    }
    #[test]
    fn running_export_observes_shared_cancellation_between_pages() {
        let cancelled = AtomicBool::new(false);
        let mut budget = Budget::new(
            &json!({"limits":{"rows":10000,"bytes":1000000,"executions":1,"seconds":5}}),
            &cancelled,
        )
        .unwrap();
        budget.check(1, 1).unwrap();
        cancelled.store(true, Ordering::Relaxed);
        assert_eq!(budget.check(0, 0).unwrap_err().code, "EVIDENCE_CANCELLED");
        assert_eq!(
            budget.preflight(0, 0).unwrap_err().code,
            "EVIDENCE_CANCELLED"
        );
    }
    #[test]
    fn cancellation_interrupts_long_sql_without_python_callbacks() {
        let connection = Connection::open_in_memory().unwrap();
        let cancelled = Arc::new(AtomicBool::new(false));
        let budget = Budget::new(
            &json!({"limits":{"rows":10000,"bytes":1000000,"executions":1,"seconds":5}}),
            cancelled.as_ref(),
        )
        .unwrap();
        install_progress(&connection, &budget, &cancelled);
        let signal = Arc::clone(&cancelled);
        let thread = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(10));
            signal.store(true, Ordering::Relaxed);
        });
        let result = connection.query_row(
            "WITH RECURSIVE n(v) AS(SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<100000000)
            SELECT sum(v) FROM n",
            [],
            |row| row.get::<_, i64>(0),
        );
        thread.join().unwrap();
        assert!(
            matches!(result,Err(rusqlite::Error::SqliteFailure(code,_)) if code.code==rusqlite::ErrorCode::OperationInterrupted)
        );
        assert_eq!(
            budget.preflight(0, 0).unwrap_err().code,
            "EVIDENCE_CANCELLED"
        );
    }
    #[test]
    fn raw_budget_rejects_oversized_invalid_json_before_decoding() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE execution_event(id INTEGER PRIMARY KEY,details JSON);
            INSERT INTO execution_event VALUES(1,'invalid-json-invalid-json');",
            )
            .unwrap();
        let mut budget = test_budget();
        budget.max_bytes = 8;
        assert_eq!(
            pages(&connection, "execution_event", "1", &[], &mut budget)
                .unwrap_err()
                .code,
            "EVIDENCE_TOO_LARGE"
        );
    }
    #[test]
    fn assembled_views_cannot_bypass_shared_byte_budget() {
        let mut budget = test_budget();
        budget.max_bytes = 5;
        assert_eq!(
            budget
                .assembled(&json!({"large": [1, 2]}))
                .unwrap_err()
                .code,
            "EVIDENCE_TOO_LARGE"
        );
        let mut budget = test_budget();
        budget.max_rows = 0;
        assert_eq!(budget.check(1, 0).unwrap_err().code, "EVIDENCE_TOO_LARGE");
    }
    #[test]
    fn ownership_considers_every_source_and_never_guesses_from_artifacts() {
        let connection = Connection::open_in_memory().unwrap();
        for (table, _) in SOURCES {
            connection
                .execute_batch(&format!(
                    "CREATE TABLE {table}(account_id INTEGER,execution_id TEXT)"
                ))
                .unwrap();
        }
        connection
            .execute(
                "INSERT INTO execution_artifact VALUES(7,'artifact-only')",
                [],
            )
            .unwrap();
        assert_eq!(
            owner(&connection, "artifact-only").unwrap_err().code,
            "EVIDENCE_NOT_FOUND"
        );
        connection
            .execute("INSERT INTO execution_intent VALUES(7,'execution')", [])
            .unwrap();
        assert_eq!(owner(&connection, "execution").unwrap(), 7);
        connection
            .execute(
                "INSERT INTO account_control_session VALUES(8,'execution')",
                [],
            )
            .unwrap();
        assert_eq!(
            owner(&connection, "execution").unwrap_err().code,
            "EVIDENCE_OWNER_CONFLICT"
        );
    }
    fn control_schema() -> Connection {
        control_schema_in(Connection::open_in_memory().unwrap())
    }
    fn control_schema_in(connection: Connection) -> Connection {
        connection.execute_batch("CREATE TABLE account(id INTEGER,name TEXT,trade_channel TEXT);
            INSERT INTO account VALUES(7,'account','ctp');
            CREATE TABLE execution_intent(id INTEGER,account_id INTEGER,execution_id TEXT,
                created_at TEXT,finished_at TEXT,payload JSON);
            CREATE TABLE executerecord(id INTEGER,account_id INTEGER,execution_id TEXT,created_at TEXT);
            CREATE TABLE execution_event(id INTEGER,account_id INTEGER,execution_id TEXT,ts_local_created TEXT,details JSON);
            CREATE TABLE execution_artifact(id INTEGER,execution_id TEXT,artifact_type TEXT,content JSON);
            CREATE TABLE account_control_event(id INTEGER,account_id INTEGER,execution_id TEXT,
                context_kind TEXT,source TEXT,occurred_at_ms INTEGER,metadata JSON,counted BOOLEAN,decision TEXT);
            CREATE TABLE account_control_session(id INTEGER,account_id INTEGER,execution_id TEXT,
                context_kind TEXT,source TEXT,started_at_ms INTEGER,finished_at_ms INTEGER);
            CREATE TABLE account_control_counter_delta(id INTEGER,account_id INTEGER,execution_id TEXT,
                context_kind TEXT,source TEXT,bucket_start TEXT,bucket_type TEXT);
            CREATE TABLE execution_input(id TEXT,account_id INTEGER,execution_id TEXT,owner_execution_id TEXT,root_kind TEXT,root_value JSON);
            CREATE TABLE execution_input_target(input_id TEXT);
            CREATE TABLE execution_input_symbol(input_id TEXT);
            CREATE TABLE execution_input_plan(input_id TEXT);
            CREATE TABLE business_asset_binding(record_id INTEGER,asset_id TEXT);
            CREATE TABLE business_asset_snapshot(asset_id TEXT,record_id INTEGER,input_id TEXT);
            CREATE TABLE business_asset_position(asset_id TEXT);").unwrap();
        connection
    }
    #[test]
    fn complete_control_only_bundle_is_read_only_and_preserves_operation_context() {
        let connection = control_schema();
        connection.execute_batch("INSERT INTO account_control_event VALUES(1,7,'orders-00000000000000000000000000000000',
            'unknown','unknown',1,'{}',1,'ALLOWED'); BEGIN;").unwrap();
        let mut budget = test_budget();
        let request = json!({"execution_id":"orders-00000000000000000000000000000000"});
        let snapshot = collect(&connection, &request, &mut budget).unwrap();
        assert!(snapshot.executions.is_empty());
        let item = &snapshot.operations["orders-00000000000000000000000000000000"];
        assert_eq!(item["controls"][0]["decision"], "allowed");
        assert_eq!(item["controls"][0]["metadata"], json!({}));
        assert_eq!(item["context_kind"], "account_operation");
        assert!(item["record"].is_null());
        assert!(item["input"].is_null());
        assert_eq!(snapshot.counts["account_control_event"], 1);
        connection.execute_batch("ROLLBACK").unwrap();
        let changes: i64 = connection
            .query_row("SELECT total_changes()", [], |row| row.get(0))
            .unwrap();
        assert_eq!(changes, 2);
    }
    #[test]
    fn concurrent_commit_between_owner_and_sources_keeps_one_read_snapshot() {
        let path = std::env::temp_dir().join(format!(
            "axile-evidence-read-snapshot-{}.sqlite",
            std::process::id()
        ));
        let writer = control_schema_in(Connection::open(&path).unwrap());
        let mode: String = writer
            .query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
        writer
            .execute_batch(
                r#"INSERT INTO account_control_event VALUES(1,7,'e1',
            'execution','algorithm',1,'{}',1,'ALLOWED');
            INSERT INTO execution_event VALUES(1,7,'e1','t','{"generation":"before"}');"#,
            )
            .unwrap();
        let reader = storage_connection::open_reader(&path).unwrap();
        reader.execute_batch("BEGIN").unwrap();
        assert_eq!(owner(&reader, "e1").unwrap(), 7);

        // owner 已固定快照；后续来源查询不能混入另一连接刚提交的材料。
        writer.execute_batch(r#"BEGIN IMMEDIATE;
            UPDATE account SET name='after' WHERE id=7;
            INSERT INTO execution_event VALUES(2,7,'e1','t','{"generation":"after"}');
            INSERT INTO execution_artifact VALUES(1,'e1','MARKET_SNAPSHOT','{"generation":"after"}');
            COMMIT;"#).unwrap();
        assert_eq!(
            writer
                .query_row("SELECT count(*) FROM execution_event", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            2
        );
        let request = json!({"execution_id":"e1"});
        let mut budget = test_budget();
        let snapshot = collect(&reader, &request, &mut budget).unwrap();
        assert_eq!(snapshot.account["name"], "account");
        assert_eq!(
            snapshot.executions["e1"]["events"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            snapshot.executions["e1"]["events"][0]["details"]["generation"],
            "before"
        );
        assert!(snapshot.executions["e1"]["artifacts"]
            .as_array()
            .unwrap()
            .is_empty());
        assert_eq!(snapshot.counts["execution_event"], 1);
        assert_eq!(snapshot.counts["execution_artifact"], 0);
        reader.execute_batch("ROLLBACK").unwrap();

        reader.execute_batch("BEGIN").unwrap();
        let mut budget = test_budget();
        let latest = collect(&reader, &request, &mut budget).unwrap();
        assert_eq!(latest.account["name"], "after");
        assert_eq!(
            latest.executions["e1"]["events"].as_array().unwrap().len(),
            2
        );
        assert_eq!(
            latest.executions["e1"]["artifacts"][0]["content"]["generation"],
            "after"
        );
        assert_eq!(latest.counts["execution_event"], 2);
        assert_eq!(latest.counts["execution_artifact"], 1);
        reader.execute_batch("ROLLBACK").unwrap();
        drop(reader);
        drop(writer);
        std::fs::remove_file(path).unwrap();
    }
    #[test]
    fn frozen_variant_reference_uses_durable_owner_and_rejects_another_execution() {
        let connection = control_schema();
        connection
            .execute_batch(
                "INSERT INTO account_control_event VALUES(1,7,'e1',
            'execution','algorithm',1,'{}',1,'ALLOWED');
            INSERT INTO execution_input VALUES('variant',7,NULL,'e1','scalar','\"frozen\"');",
            )
            .unwrap();
        let content =
            json!({"business_facts_ref":{"version":1,"view":"input","input_id":"variant"}});
        connection
            .execute(
                "INSERT INTO execution_artifact VALUES(1,'e1','STANDARD_INPUT',?1)",
                [content.to_string()],
            )
            .unwrap();
        let request = json!({"execution_id":"e1"});
        let mut budget = test_budget();
        let snapshot = collect(&connection, &request, &mut budget).unwrap();
        assert_eq!(
            snapshot.executions["e1"]["artifacts"][0]["content"]["input"],
            "frozen"
        );
        assert_eq!(
            snapshot.executions["e1"]["artifacts"][0]["content"]["business_facts_ref"],
            content["business_facts_ref"]
        );
        connection
            .execute(
                "UPDATE execution_input SET owner_execution_id='other' WHERE id='variant'",
                [],
            )
            .unwrap();
        let mut budget = test_budget();
        let result = collect(&connection, &request, &mut budget);
        assert!(matches!(result,Err(error) if error.code=="EVIDENCE_INVALID_DATA"));
    }
    #[test]
    fn page_cursor_reads_every_id_across_the_page_boundary() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE execution_event(id INTEGER PRIMARY KEY,details JSON);
            WITH RECURSIVE ids(n) AS(SELECT 1 UNION ALL SELECT n+1 FROM ids WHERE n<1001)
            INSERT INTO execution_event SELECT n*2,'{}' FROM ids;",
            )
            .unwrap();
        let mut budget = test_budget();
        let rows = pages(&connection, "execution_event", "1", &[], &mut budget).unwrap();
        assert_eq!(rows.len(), 1001);
        assert_eq!(rows[0]["id"], 2);
        assert_eq!(rows[1000]["id"], 2002);
        assert_eq!(budget.rows, 1001);
    }
    #[test]
    fn operation_contexts_do_not_consume_execution_limit_and_business_evidence_wins() {
        let mut snapshot = Snapshot::new(json!({"id":1}));
        let budget = test_budget();
        for ordinal in 0..20 {
            let key = format!("orders-{ordinal:032x}");
            snapshot
                .select_control(
                    &json!({"execution_id":key,"context_kind":"unknown","source":"unknown"}),
                    "control",
                    &budget,
                )
                .unwrap();
            snapshot.item_mut(&key).unwrap()["controls"] =
                json!([{"context_kind":"unknown","source":"unknown"}]);
        }
        let expected_keys = snapshot.operations.keys().cloned().collect::<Vec<_>>();
        let first = "orders-00000000000000000000000000000000";
        snapshot.item_mut(first).unwrap()["controls"] = json!([
            {"context_kind":"account_operation","source":"current_orders"},
            {"context_kind":"account_operation","source":"manual_execution"}
        ]);
        snapshot.item_mut(first).unwrap()["counters"] = json!([
            {"context_kind":"account_operation","source":"current_orders"},
            {"context_kind":"account_operation","source":"instrument_catalog"}
        ]);
        snapshot.item_mut(first).unwrap()["sessions"] = json!([
            {"context_kind":"account_operation","source":"manual_execution"}
        ]);
        snapshot.classify(&budget).unwrap();
        assert_eq!(snapshot.operations.keys().cloned().collect::<Vec<_>>(), expected_keys);
        assert_eq!(snapshot.operations[first]["sources"], json!(["current_orders","manual_execution","instrument_catalog"]));
        assert_eq!(snapshot.operations.len(), 20);
        assert!(snapshot.executions.is_empty());
        let key = "orders-00000000000000000000000000000000";
        snapshot.item_mut(key).unwrap()["events"] = json!([{}]);
        snapshot.classify(&budget).unwrap();
        assert_eq!(snapshot.executions.len(), 1);
        assert_eq!(snapshot.operations.len(), 19);
        assert_eq!(snapshot.executions[key]["context_kind"], "execution");
        let classified = (snapshot.executions.clone(), snapshot.operations.clone());
        snapshot.classify(&budget).unwrap();
        assert_eq!((snapshot.executions.clone(), snapshot.operations.clone()), classified);
        snapshot.item_mut("orders-00000000000000000000000000000001").unwrap()["events"] = json!([{}]);
        assert_eq!(snapshot.classify(&budget).unwrap_err().code, "EVIDENCE_TOO_MANY_EXECUTIONS");
        assert!(legacy_source("orders-strategy").is_none());
    }
    #[test]
    fn exact_window_respects_offsets_fold_and_unknown_dates() {
        let request = json!({"start_ms":0.0,"end_ms":1.0,"legacy_timezone":"America/New_York"});
        let window = Window::new(&request).unwrap();
        assert_eq!(
            window.time(&json!("2026-11-01T01:30:00")),
            window.time(&json!("2026-11-01T01:30:00-04:00"))
        );
        assert_eq!(
            window.time(&json!("2026-03-08T02:30:00")),
            window.time(&json!("2026-03-08T02:30:00-05:00"))
        );
        assert_eq!(window.time(&json!("bad-time")), None);
    }
}
