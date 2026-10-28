use crate::{
    browser_fact_schema::TABLES,
    local::{self, sql, Result, Schema},
};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

pub struct BrowserStore {
    pub(crate) db: Connection,
    pub(crate) account: i64,
    pub(crate) build: String,
    pub(crate) schemas: &'static [Schema],
}
impl BrowserStore {
    pub fn new(prefix: String, account: i64, build: String) -> Result<Self> {
        if account <= 0 || prefix.is_empty() {
            return Err("Invalid browser database identity".into());
        }
        let db = Connection::open(format!("{prefix}-incremental.sqlite3")).map_err(sql)?;
        db.execute_batch(
            "PRAGMA journal_mode=DELETE;PRAGMA synchronous=FULL;PRAGMA cache_size=-4096",
        )
        .map_err(sql)?;
        let version: i64 = db
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .map_err(sql)?;
        if version == 0 {
            local::transaction(&db, || {
                db.execute_batch("CREATE TABLE facts(record_id INTEGER PRIMARY KEY,seq TEXT NOT NULL,time TEXT,asset REAL,eligible INTEGER,body TEXT NOT NULL,normal TEXT);CREATE INDEX facts_time ON facts(time,record_id);CREATE TABLE results(kind TEXT NOT NULL,range_key TEXT NOT NULL,key TEXT NOT NULL,version INTEGER NOT NULL,record_id INTEGER NOT NULL,time TEXT NOT NULL,body TEXT,PRIMARY KEY(kind,range_key,key,version));CREATE INDEX results_record ON results(record_id,kind,version);CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL);PRAGMA user_version=1").map_err(sql)
            })?;
        } else if version != 1 && version != 2 {
            return Err("SCHEMA_INCOMPATIBLE: 浏览器分析数据库版本不兼容".into());
        }
        if version < 2 {
            local::transaction(&db, || {
                db.execute_batch("CREATE INDEX results_version ON results(version);CREATE INDEX results_trade_day ON results(json_extract(body,'$.day'),record_id) WHERE kind='performance_cost_trade';CREATE INDEX results_event_time ON results(kind,time DESC,range_key,key,version) WHERE kind='source_event' AND body IS NOT NULL;PRAGMA user_version=2").map_err(sql)
            })?;
        }
        let schemas = local::result_schemas()?;
        Ok(Self {
            db,
            account,
            build,
            schemas,
        })
    }
    pub fn metadata(&self, remote: &Value) -> Result<Value> {
        let p = local::get(&self.db, "published")?;
        let job = local::get(&self.db, "job")?;
        let compatible = !p.is_null()
            && p["build"].as_str() == Some(&self.build)
            && (remote.is_null() || p["source_id"] == remote["source_id"]);
        let ready = compatible
            && job.is_null()
            && (remote.is_null()
                || p["cursor"] == remote["cursor"]
                    && p["settings"] == remote["settings"]
                    && p["trade_channel"] == remote["trade_channel"]);
        Ok(
            json!({"schema_version":1,"status":if ready{"ready"}else if compatible{"stale"}else{"pending"},"source_version":remote["source_version"].as_i64().or(p["source_version"].as_i64()).unwrap_or(0),"snapshot_id":if compatible{p["snapshot_id"].clone()}else{Value::Null},"snapshot_version":if compatible{p["source_version"].clone()}else{Value::Null},"logic_version":self.build,"engine_version":"equity-ledger-1","computed_at":if compatible{p["computed_at"].clone()}else{Value::Null},"data_until":if compatible{p["data_until"].clone()}else{Value::Null},"read_view_schema_version":if compatible{1}else{0},"error":null,"retry_at":null}),
        )
    }
    pub fn begin(&mut self, remote: Value) -> Result<Value> {
        if remote["schema_version"] != 2
            || remote["account_id"].as_i64() != Some(self.account)
            || remote["source_id"].as_str().is_none()
            || remote["cursor"]
                .as_str()
                .and_then(|s| s.parse::<i64>().ok())
                .is_none()
            || !remote["settings"]["backtest_fee_rate"]
                .as_f64()
                .is_some_and(|f| f.is_finite() && (0.0..1.0).contains(&f))
        {
            return Err("SCHEMA_INCOMPATIBLE: 分析事实格式或账户设置无效".into());
        }
        local::transaction(&self.db, || {
            let source = local::get(&self.db, "source")?;
            let downloaded = local::get(&self.db, "downloaded")?;
            let previous_job = self.job()?;
            if !source.is_null()
                && (source != remote["source_id"]
                    || downloaded
                        .as_str()
                        .and_then(|v| v.parse::<i64>().ok())
                        .unwrap_or(0)
                        .max(
                            previous_job["remote"]["cursor"]
                                .as_str()
                                .and_then(|v| v.parse().ok())
                                .unwrap_or(0),
                        )
                        > remote["cursor"]
                            .as_str()
                            .unwrap()
                            .parse::<i64>()
                            .map_err(sql)?)
            {
                self.db
                    .execute_batch("DELETE FROM facts;DELETE FROM results;DELETE FROM state")
                    .map_err(sql)?;
            }
            local::set(&self.db, "source", &remote["source_id"])?;
            let mut job = local::get(&self.db, "job")?;
            if job.is_null() {
                let p = local::get(&self.db, "published")?;
                let generation = p["generation"].as_i64().unwrap_or(0) + 1;
                let downloaded = local::get(&self.db, "downloaded")?;
                job = json!({"remote":remote,"generation":generation,"cursor":{"seq":downloaded.as_str().unwrap_or("0"),"table":TABLES.len(),"after":null},"phase":"sync","dirty":null,"cost_days":[],"renormalize":!p.is_null() && (p["build"].as_str()!=Some(&self.build) || p["trade_channel"]!=remote["trade_channel"]),"reset":p["build"].as_str()!=Some(&self.build) || p["settings"]!=remote["settings"] || p["trade_channel"]!=remote["trade_channel"]});
                local::set(&self.db, "job", &job)?;
            }
            Ok(job)
        })
    }
    pub fn job(&self) -> Result<Value> {
        local::get(&self.db, "job")
    }
    pub fn page(&mut self, page: Value) -> Result<Value> {
        let mut job = self.job()?;
        if job["phase"] != "sync"
            || page["schema_version"] != 2
            || page["source_id"] != job["remote"]["source_id"]
        {
            return Err("SOURCE_CHANGED: 同步数据源不匹配".into());
        }
        validate_page(&page, &job)?;
        let version = job["generation"].as_i64().unwrap();
        self.adapt(version, &job["remote"])?;
        local::transaction(&self.db, || {
            let mut pending = local::get(&self.db, "pending")?;
            for part in page["parts"].as_array().ok_or("Invalid fact page")? {
                let record = part["record_id"]
                    .as_str()
                    .and_then(|s| s.parse::<i64>().ok())
                    .filter(|id| *id >= 0)
                    .ok_or("Invalid record identity")?;
                if pending["seq"] != part["seq"] || pending["record_id"] != part["record_id"] {
                    pending = json!({"seq":part["seq"],"record_id":part["record_id"],"rows":{}});
                }
                if let Some(table) = part["table"].as_str() {
                    let schema = TABLES
                        .iter()
                        .find(|t| t.name == table)
                        .ok_or("Invalid fact table")?;
                    let rows = part["rows"].as_array().ok_or("Invalid fact rows")?;
                    if rows
                        .iter()
                        .any(|r| r.as_array().is_none_or(|r| r.len() != schema.names().len()))
                    {
                        return Err("Invalid fact columns".into());
                    }
                    let auxiliary = [
                        "account",
                        "portfolioaccount",
                        "schedule_skip",
                        "analysis_calendar_day",
                    ]
                    .contains(&table);
                    if auxiliary != (record == 0) {
                        return Err("Invalid fact owner".into());
                    }
                    let names = schema.names();
                    for row in rows {
                        for (column, owner) in [("account_id", self.account), ("record_id", record)]
                        {
                            if let Some(i) = names.iter().position(|name| *name == column) {
                                if row[i].as_i64() != Some(owner) {
                                    return Err("Invalid fact owner".into());
                                }
                            }
                        }
                        if ["account", "executerecord"].contains(&table)
                            && row[0].as_i64()
                                != Some(if record == 0 { self.account } else { record })
                        {
                            return Err("Invalid fact identity".into());
                        }
                    }
                    if pending["rows"][table].is_null() {
                        pending["rows"][table] = json!([]);
                    }
                    pending["rows"][table]
                        .as_array_mut()
                        .unwrap()
                        .extend(rows.clone());
                } else {
                    let old: Option<String> = self
                        .db
                        .query_row("SELECT time FROM facts WHERE record_id=?1", [record], |r| {
                            r.get(0)
                        })
                        .optional()
                        .map_err(sql)?
                        .flatten();
                    if let Some(time) = old {
                        dirty(&mut job, &time);
                    }
                    self.cost_days(&mut job, record)?;
                    local::remove(&self.db,version,"record_id=?1 AND (kind GLOB 'performance_cost_*' OR kind='performance_execution_fee' OR kind='source_event')",&[&record])?;
                    if part["deleted"] == true {
                        self.db
                            .execute("DELETE FROM facts WHERE record_id=?1", [record])
                            .map_err(sql)?;
                    } else {
                        let normal = self.normalize(record, &pending["rows"], &job)?;
                        let time = normal["observation"]["time"].as_str();
                        if let Some(t) = time {
                            dirty(&mut job, t);
                        }
                        let eligible = normal["observation"]["asset"]
                            .as_f64()
                            .is_some_and(|a| a > 0.0)
                            && normal["eligible"].as_bool().unwrap_or(false);
                        self.db.execute("INSERT INTO facts VALUES(?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(record_id) DO UPDATE SET seq=excluded.seq,time=excluded.time,asset=excluded.asset,eligible=excluded.eligible,body=excluded.body,normal=excluded.normal",params![record,part["seq"].as_str().ok_or("Invalid fact revision")?,time,normal["observation"]["asset"].as_f64(),eligible,pending["rows"].to_string(),normal.to_string()]).map_err(sql)?;
                        self.cost_days(&mut job, record)?;
                    }
                    pending = Value::Null;
                }
            }
            job["cursor"] = page["cursor"].clone();
            if page["complete"] == true {
                local::set(&self.db, "downloaded", &job["remote"]["cursor"])?;
                job["phase"] = json!("calculate");
                pending = Value::Null;
            }
            local::set(&self.db, "pending", &pending)?;
            local::set(&self.db, "job", &job)?;
            Ok(job)
        })
    }
    pub(crate) fn adapt(&self, version: i64, remote: &Value) -> Result<()> {
        local::adapters(
            &self.db,
            &self.schemas,
            version,
            "working",
            self.account,
            remote,
            &self.build,
        )
    }
    fn cost_days(&self, job: &mut Value, record: i64) -> Result<()> {
        let mut days: std::collections::BTreeSet<String> =
            serde_json::from_value(job["cost_days"].clone()).map_err(sql)?;
        let mut s = self.db.prepare("SELECT DISTINCT json_extract(body,'$.day') FROM local_results WHERE kind='performance_cost_trade' AND record_id=?1").map_err(sql)?;
        for day in s
            .query_map([record], |r| r.get::<_, String>(0))
            .map_err(sql)?
        {
            days.insert(day.map_err(sql)?);
        }
        job["cost_days"] = json!(days);
        Ok(())
    }
    pub(crate) fn normalize(&self, record: i64, raw: &Value, job: &Value) -> Result<Value> {
        let scratch = local::scratch()?;
        local::load(&scratch, raw)?;
        if record == 0 {
            let mut events = Vec::new();
            for (table, time_col, tag) in [
                ("portfolioaccount", 2, "换绑"),
                ("schedule_skip", 2, "跳过"),
            ] {
                if let Some(rows) = raw[table].as_array() {
                    for row in rows {
                        let time = row[time_col].as_str().ok_or("Invalid event time")?;
                        let text = if table == "portfolioaccount" {
                            row[3]
                                .as_i64()
                                .map(|id| format!("组合 #{id}"))
                                .unwrap_or("解绑组合".into())
                        } else if row[3] == "BUSY" {
                            "执行中".into()
                        } else {
                            "休市".into()
                        };
                        let event = json!({"time":time,"tag":tag,"text":text,"execution_id":null});
                        events.push((format!("{table}:{}", row[0]), event));
                    }
                }
            }
            for (key, event) in events {
                local::put(
                    &self.db,
                    "source_event",
                    "",
                    &key,
                    job["generation"].as_i64().unwrap(),
                    0,
                    &crate::calendar::canonical_time(event["time"].as_str().unwrap())?,
                    &event,
                )?;
            }
            return Ok(json!({}));
        }
        scratch
            .execute(
                "INSERT OR REPLACE INTO account VALUES(?1,?2)",
                params![
                    self.account,
                    job["remote"]["trade_channel"]
                        .as_str()
                        .ok_or("Missing channel")?
                ],
            )
            .map_err(sql)?;
        crate::fact_integrity::validate(&scratch, self.account)?;
        let observation = crate::fact_observation::ObservationReader::new(&scratch)
            .map_err(sql)?
            .read(self.account, record)?;
        let contribution = crate::contributions::input(
            &scratch,
            record,
            job["remote"]["trade_channel"].as_str().unwrap(),
        )
        .map_err(sql)?;
        let mut next_id = local::get(&self.db, "trade_id")?.as_i64().unwrap_or(0);
        crate::fact_cost_rows::trades_from(&scratch, self.account, &scratch, &[], &mut next_id)
            .map_err(sql)?;
        local::set(&self.db, "trade_id", &json!(next_id))?;
        crate::publication::events(
            &scratch,
            self.account,
            &[("all", f64::NEG_INFINITY, f64::INFINITY)],
            &scratch,
        )
        .map_err(sql)?;
        let event:Option<String>=scratch.query_row("SELECT json_object('time',time,'tag',tag,'text',text,'execution_id',execution_id) FROM native_stage.performance_result_event LIMIT 1",[],|r|r.get(0)).optional().map_err(sql)?;
        if let Some(event) = event {
            local::put(
                &self.db,
                "source_event",
                "",
                &format!("execution:{record}"),
                job["generation"].as_i64().unwrap(),
                record,
                &observation.time,
                &serde_json::from_str(&event).map_err(sql)?,
            )?;
        }
        scratch
            .execute("DELETE FROM native_stage.performance_result_event", [])
            .map_err(sql)?;
        local::save_stage(
            &self.db,
            &scratch,
            &self.schemas,
            job["generation"].as_i64().unwrap(),
            &observation.time,
        )?;
        let eligible = observation.target.as_ref().is_some_and(|t| {
            t.iter()
                .all(|(s, w)| *w == 0.0 || observation.prices.contains_key(s))
        });
        Ok(json!({"observation":observation,"contribution":contribution,"eligible":eligible}))
    }
    pub fn calculate(&mut self, computed_at: &str) -> Result<Value> {
        crate::engine::step(self, computed_at)?;
        self.metadata(&Value::Null)
    }
    pub fn query(&self, request: &Value) -> Result<Value> {
        let p = local::get(&self.db, "published")?;
        if p["build"].as_str() != Some(&self.build)
            || request["snapshot_id"] != p["snapshot_id"]
            || p.is_null()
        {
            return Err("SNAPSHOT_EXPIRED: 分析结果已更新，请重新读取".into());
        }
        local::adapters(
            &self.db,
            &self.schemas,
            p["generation"].as_i64().unwrap(),
            p["snapshot_id"].as_str().unwrap(),
            self.account,
            &p,
            &self.build,
        )?;
        let range = request["range"].as_str().unwrap_or("all");
        let limit = request["limit"].as_i64().unwrap_or(200).clamp(1, 500);
        let offset = request["offset"].as_i64().unwrap_or(0).max(0);
        let snapshot = p["snapshot_id"].as_str().unwrap();
        match request["command"].as_str() {
            Some("chart") => crate::views::chart(&self.db, self.account, snapshot, range),
            Some("execution-summary") => crate::views::execution_summary(
                &self.db,
                self.account,
                snapshot,
                range,
                limit,
                offset,
                request["start"].as_f64(),
                request["end"].as_f64(),
            ),
            Some("execution-detail") => crate::views::execution_detail(
                &self.db,
                self.account,
                snapshot,
                request["record_id"].as_i64().ok_or("Missing record")?,
                limit,
                offset,
            ),
            Some("costs") => {
                let mut r = request.clone();
                r["account_id"] = json!(self.account);
                r["logic_version"] = json!(self.build);
                r["engine_version"] = json!("equity-ledger-1");
                crate::costs::query_costs(&self.db, &r)
            }
            _ => Err("Unknown browser analysis query".into()),
        }
    }
}
fn revision(value: &Value) -> Result<i64> {
    value
        .as_str()
        .and_then(|s| s.parse::<i64>().ok())
        .filter(|v| *v >= 0)
        .ok_or("Invalid fact revision".into())
}
fn validate_page(page: &Value, job: &Value) -> Result<()> {
    let from = revision(&job["cursor"]["seq"])?;
    let until = revision(&job["remote"]["cursor"])?;
    let seq = revision(&page["cursor"]["seq"])?;
    let table = page["cursor"]["table"]
        .as_u64()
        .filter(|v| *v <= TABLES.len() as u64)
        .ok_or("Invalid table cursor")?;
    let after = &page["cursor"]["after"];
    let complete = page["complete"]
        .as_bool()
        .ok_or("Missing page completion")?;
    let parts = page["parts"].as_array().ok_or("Invalid fact page")?;
    if page["from"] != job["cursor"]
        || page["until"] != job["remote"]["cursor"]
        || seq < from
        || seq > until
        || page.to_string().len() > 2 * 1024 * 1024
        || (complete && (seq != until || table != TABLES.len() as u64 || !after.is_null()))
        || (!complete && page["cursor"] == job["cursor"])
    {
        return Err("Invalid synchronization boundary".into());
    }
    if !after.is_null()
        && (table == TABLES.len() as u64
            || after.as_array().is_none_or(|keys| {
                keys.len() != TABLES[table as usize].key.split(',').count()
                    || keys
                        .iter()
                        .any(|key| key.as_str().is_none_or(|s| s.len() > 8192))
            }))
    {
        return Err("Invalid row cursor".into());
    }
    let mut count = 0;
    let mut previous = from;
    for part in parts {
        let current = revision(&part["seq"])?;
        if current < previous || current > seq || current > until || current == 0 {
            return Err("Invalid fact order".into());
        }
        previous = current;
        if part["table"].is_null() {
            if !part["deleted"].is_boolean() {
                return Err("Invalid deletion marker".into());
            }
        } else {
            count += part["rows"].as_array().ok_or("Invalid fact rows")?.len();
        }
    }
    if count > 4096 {
        return Err("Fact page exceeds row limit".into());
    }
    Ok(())
}
fn dirty(job: &mut Value, time: &str) {
    if job["dirty"].as_str().is_none_or(|old| time < old) {
        job["dirty"] = json!(time);
    }
}
