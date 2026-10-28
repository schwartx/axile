//! Resume a persisted ledger, replaying the open day when its closing observation changes.
use crate::{
    contributions::{self, Input},
    local::{self, sql, Result},
    performance,
    replay::Observation,
    BrowserStore,
};
use chrono::Duration;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
#[derive(Clone, Serialize, Deserialize)]
struct Checkpoint {
    performance: performance::Incremental,
    after: Option<String>,
    day: String,
    contribution: Option<contributions::Incremental>,
    fill_until: Option<String>,
    next_point: usize,
}
#[derive(Serialize, Deserialize)]
struct Work {
    key: String,
    start: String,
    equity: Option<f64>,
    current: Checkpoint,
    before: Checkpoint,
    phase: String,
    cost_days: Option<Vec<String>>,
}
fn decode<T: serde::de::DeserializeOwned>(v: Value) -> Result<T> {
    serde_json::from_value(v).map_err(sql)
}
fn normal(store: &BrowserStore, record: i64) -> Result<Value> {
    let s: String = store
        .db
        .query_row(
            "SELECT normal FROM facts WHERE record_id=?1",
            [record],
            |r| r.get(0),
        )
        .map_err(sql)?;
    serde_json::from_str(&s).map_err(sql)
}
fn baseline(store: &BrowserStore, key: &str) -> Result<(String, Option<f64>)> {
    let (first, last): (Option<String>, Option<String>) = store
        .db
        .query_row(
            "SELECT min(time),max(time) FROM facts WHERE record_id>0",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .map_err(sql)?;
    let cutoff = match (first, last) {
        (Some(first), Some(last)) => {
            if key == "all" {
                first
            } else {
                (crate::calendar::local_time(&last)?
                    - Duration::days(key.parse::<i64>().map_err(sql)?))
                .format("%Y-%m-%dT%H:%M:%S%.6f")
                .to_string()
            }
        }
        _ => String::new(),
    };
    let selected=store.db.query_row("SELECT time,asset FROM facts WHERE record_id IN (SELECT max(record_id) FROM facts WHERE record_id>0 GROUP BY time) AND eligible=1 AND time<=?1 ORDER BY time DESC,record_id DESC LIMIT 1",[&cutoff],|r|Ok((r.get::<_,String>(0)?,r.get::<_,f64>(1)?))).optional().map_err(sql)?;
    let selected=match selected {Some(v)=>Some(v),None=>store.db.query_row("SELECT time,asset FROM facts WHERE record_id IN (SELECT max(record_id) FROM facts WHERE record_id>0 GROUP BY time) AND eligible=1 ORDER BY time,record_id DESC LIMIT 1",[],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(sql)?};
    if let Some((time, asset)) = selected {
        return Ok((time, Some(asset)));
    }
    let fallback=store.db.query_row("SELECT asset FROM facts WHERE record_id IN (SELECT max(record_id) FROM facts WHERE record_id>0 GROUP BY time) AND asset>0 AND time>=?1 ORDER BY time,record_id DESC LIMIT 1",[&cutoff],|r|r.get::<_,f64>(0)).optional().map_err(sql)?;
    Ok((cutoff, fallback))
}
fn begin(store: &BrowserStore, job: &Value, key: &str) -> Result<Work> {
    let (start, equity) = baseline(store, key)?;
    let fee = job["remote"]["settings"]["backtest_fee_rate"]
        .as_f64()
        .unwrap();
    let initial = Checkpoint {
        performance: performance::Incremental::new(key, start.clone(), equity, fee)?,
        after: None,
        day: String::new(),
        contribution: None,
        fill_until: None,
        next_point: 0,
    };
    let saved = local::get(&store.db, &format!("range:{key}"))?;
    let same_baseline = !job["reset"].as_bool().unwrap_or(true)
        && saved["start"] == start
        && saved["equity"] == json!(equity);
    let cost_days: Option<Vec<String>> = if same_baseline {
        Some(decode(job["cost_days"].clone())?)
    } else {
        None
    };
    let (current, before) = if same_baseline {
        let saved: Work = decode(saved)?;
        if job["dirty"].is_null() {
            (saved.current, saved.before)
        } else if saved
            .current
            .after
            .as_deref()
            .is_some_and(|last| job["dirty"].as_str().is_some_and(|dirty| dirty > last))
        {
            let mut current = saved.current;
            // Appends continue the equity ledger; only the unfinished daily contribution is replaced.
            if job["dirty"].as_str().and_then(|t| t.get(..10)) == Some(&current.day) {
                current.contribution = saved.before.contribution.clone();
                current.fill_until = saved.before.fill_until.clone();
                current.next_point = saved.before.next_point;
            }
            (current, saved.before)
        } else if saved
            .before
            .after
            .as_deref()
            .is_none_or(|last| job["dirty"].as_str().is_some_and(|dirty| dirty > last))
        {
            (saved.before.clone(), saved.before)
        } else {
            (initial.clone(), initial)
        }
    } else {
        (initial.clone(), initial)
    };
    let version = job["generation"].as_i64().unwrap();
    local::remove(
        &store.db,
        version,
        "range_key=?1 AND kind GLOB 'performance_result_*' AND kind NOT IN ('performance_result_daily_cost','performance_result_daily_fee')",
        &[&key],
    )?;
    let days = cost_days.as_ref().map(|days| json!(days).to_string());
    local::remove(&store.db, version,
        "range_key=?1 AND kind IN ('performance_result_daily_cost','performance_result_daily_fee') AND (?2 IS NULL OR json_extract(body,'$.day') IN (SELECT value FROM json_each(?2)))",
        &[&key, &days])?;
    local::remove(
        &store.db,
        version,
        "range_key=?1 AND kind='performance_contribution' AND json_extract(body,'$.idx')>=?2",
        &[&key, &(current.next_point as i64)],
    )?;
    if let Some(until) = &current.fill_until {
        local::remove(
            &store.db,
            version,
            "range_key=?1 AND kind='seen' AND time>?2",
            &[&key, until],
        )?;
    } else {
        local::remove(&store.db, version, "range_key=?1 AND kind='seen'", &[&key])?;
    }
    Ok(Work {
        key: key.into(),
        start,
        equity,
        current,
        before,
        phase: "observations".into(),
        cost_days,
    })
}
pub(crate) fn step(store: &BrowserStore, computed_at: &str) -> Result<()> {
    let mut job = store.job()?;
    if job.is_null() {
        return Ok(());
    }
    if job["phase"] != "calculate" {
        return Err("SOURCE_FACTS_INCOMPLETE: 同步尚未完成".into());
    }
    let version = job["generation"].as_i64().ok_or("Missing generation")?;
    store.adapt(version, &job["remote"])?;
    local::transaction(&store.db, || {
        if job["renormalize"] == true && job["normalized"] != true {
            // A channel change affects product attribution; reuse raw local facts, not the network.
            let last = job["normalize_after"].as_i64().unwrap_or(0);
            let values=store.db.prepare("SELECT record_id,body FROM facts WHERE record_id>?1 ORDER BY record_id LIMIT 16").map_err(sql)?.query_map([last],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?))).map_err(sql)?.collect::<rusqlite::Result<Vec<_>>>().map_err(sql)?;
            for (record, body) in &values {
                local::remove(&store.db,version,"record_id=?1 AND (kind GLOB 'performance_cost_*' OR kind='performance_execution_fee' OR kind='source_event')",&[record])?;
                let n =
                    store.normalize(*record, &serde_json::from_str(body).map_err(sql)?, &job)?;
                store
                    .db
                    .execute(
                        "UPDATE facts SET normal=?1 WHERE record_id=?2",
                        params![n.to_string(), record],
                    )
                    .map_err(sql)?;
                job["normalize_after"] = json!(record);
            }
            if values.len() < 16 {
                job["normalized"] = json!(true);
            }
            local::set(&store.db, "job", &job)?;
            return Ok(());
        }
        let index = job["range_index"].as_u64().unwrap_or(0) as usize;
        if index == 3 {
            let mut published = job["remote"].clone();
            published["generation"] = json!(version);
            published["build"] = json!(store.build);
            published["snapshot_id"] = json!(format!(
                "{}-{version}",
                job["remote"]["source_id"].as_str().unwrap()
            ));
            published["computed_at"] = json!(computed_at);
            let until: Option<String> = store
                .db
                .query_row("SELECT max(time) FROM facts WHERE record_id>0", [], |r| {
                    r.get(0)
                })
                .map_err(sql)?;
            published["data_until"] = json!(until);
            local::set(&store.db, "published", &published)?;
            local::set(&store.db, "job", &Value::Null)?;
            // Only keys changed by this publication can have obsolete versions.
            store.db.execute("DELETE FROM results WHERE (kind,range_key,key) IN (SELECT kind,range_key,key FROM results WHERE version=?1) AND version<?1",[version]).map_err(sql)?;
            store
                .db
                .execute(
                    "DELETE FROM results WHERE version=?1 AND body IS NULL",
                    [version],
                )
                .map_err(sql)?;
            return Ok(());
        }
        let mut work: Work = if job["work"].is_null() {
            begin(store, &job, ["30", "90", "all"][index])?
        } else {
            decode(job["work"].clone())?
        };
        let count: i64 = store
            .db
            .query_row("SELECT count(*) FROM facts WHERE record_id>0", [], |r| {
                r.get(0)
            })
            .map_err(sql)?;
        if work.phase == "observations" {
            let mut s=store.db.prepare("SELECT normal,time FROM facts WHERE record_id IN (SELECT max(record_id) FROM facts WHERE record_id>0 AND time>=?1 AND (?2 IS NULL OR time>?2) GROUP BY time ORDER BY time LIMIT 64) ORDER BY time").map_err(sql)?;
            let mut rows = s
                .query(params![work.start, work.current.after])
                .map_err(sql)?;
            let mut used = 0;
            while let Some(row) = rows.next().map_err(sql)? {
                let n: Value =
                    serde_json::from_str(&row.get::<_, String>(0).map_err(sql)?).map_err(sql)?;
                let time: String = row.get(1).map_err(sql)?;
                let day = &time[..10];
                if day != work.current.day {
                    work.before.performance = work.current.performance.clone();
                    work.before.after = work.current.after.clone();
                    work.before.day = day.into();
                    work.current.day = day.into();
                }
                let item: Observation = decode(n["observation"].clone())?;
                work.current.performance.push(&item)?;
                work.current.after = Some(time);
                used += 1;
            }
            if used < 64 {
                work.phase = "contributions".into();
            }
        }
        if work.phase == "contributions" {
            let range = work
                .current
                .performance
                .range(&job["remote"]["settings"], count);
            if work.current.next_point < range.points.len() {
                let idx = work.current.next_point;
                let point = &range.points[idx];
                let cutoff = crate::calendar::canonical_time(&point.observed_at)?;
                let input: Input = decode(
                    normal(store, point.record_id.ok_or("Missing observation")?)?["contribution"]
                        .clone(),
                )?;
                let scratch = local::scratch()?;
                if work.current.contribution.is_none() {
                    work.current.contribution = Some(contributions::Incremental::new(&input));
                    // Seed frozen multipliers before the baseline without carrying prior cash flows.
                    let mut s=store.db.prepare("SELECT normal FROM facts WHERE record_id>0 AND time<=?1 ORDER BY time,record_id").map_err(sql)?;
                    let mut rows = s.query([&cutoff]).map_err(sql)?;
                    while let Some(row) = rows.next().map_err(sql)? {
                        let n: Value = serde_json::from_str(&row.get::<_, String>(0).map_err(sql)?)
                            .map_err(sql)?;
                        let mut prior: Input = decode(n["contribution"].clone())?;
                        prior.fills.clear();
                        work.current
                            .contribution
                            .as_mut()
                            .unwrap()
                            .fills(&prior, &scratch)
                            .map_err(sql)?;
                    }
                    work.current.fill_until = Some(cutoff.clone());
                }
                let day = &point.date[..10];
                if day == work.current.day
                    && (idx == 0 || range.points[idx - 1].date.get(..10) != Some(day))
                {
                    work.before.contribution = if idx == 0 {
                        None
                    } else {
                        work.current.contribution.clone()
                    };
                    work.before.fill_until = if idx == 0 {
                        None
                    } else {
                        work.current.fill_until.clone()
                    };
                    work.before.next_point = idx;
                }
                if idx > 0 {
                    let prior = work.current.fill_until.clone().unwrap_or_default();
                    let mut s=store.db.prepare("SELECT normal FROM facts WHERE record_id>0 AND time>?1 AND time<=?2 ORDER BY time,record_id").map_err(sql)?;
                    let mut rows = s.query(params![prior, cutoff]).map_err(sql)?;
                    while let Some(row) = rows.next().map_err(sql)? {
                        let n: Value = serde_json::from_str(&row.get::<_, String>(0).map_err(sql)?)
                            .map_err(sql)?;
                        let input: Input = decode(n["contribution"].clone())?;
                        for (symbol, id) in contributions::Incremental::identifiers(&input) {
                            let seen:Option<String>=store.db.query_row("SELECT body FROM local_results WHERE kind='seen' AND range_key=?1 AND key=?2 AND time<=?3",params![work.key,json!([symbol,id]).to_string(),prior],|r|r.get(0)).optional().map_err(sql)?;
                            if let Some(seen) = seen {
                                let v: Value = serde_json::from_str(&seen).map_err(sql)?;
                                scratch.execute("INSERT OR IGNORE INTO native_stage.contribution_seen VALUES(?1,?2,?3,?4,?5,?6)",params![symbol,id,v["quantity"].as_f64(),v["amount"].as_f64(),v["fee"].as_f64(),v["currency"].as_str()]).map_err(sql)?;
                            }
                        }
                        work.current
                            .contribution
                            .as_mut()
                            .unwrap()
                            .fills(&input, &scratch)
                            .map_err(sql)?;
                    }
                }
                work.current
                    .contribution
                    .as_mut()
                    .unwrap()
                    .mark(&input, &scratch, &work.key, idx as i64)
                    .map_err(sql)?;
                local::save_stage(&store.db, &scratch, &store.schemas, version, &cutoff)?;
                let columns =
                    ["symbol", "id", "quantity", "amount", "fee", "currency"].map(str::to_owned);
                let mut s=scratch.prepare("SELECT symbol,id,quantity,amount,fee,currency FROM native_stage.contribution_seen").map_err(sql)?;
                let mut rows = s.query([]).map_err(sql)?;
                while let Some(row) = rows.next().map_err(sql)? {
                    let body = local::object(row, &columns).map_err(sql)?;
                    let key = json!([body["symbol"], body["id"]]).to_string();
                    // Keep the first fingerprint and its date when a later execution repeats a fill.
                    if store.db.query_row("SELECT EXISTS(SELECT 1 FROM local_results WHERE kind='seen' AND range_key=?1 AND key=?2)", params![work.key,key], |r| r.get::<_,bool>(0)).map_err(sql)? { continue; }
                    local::put(
                        &store.db, "seen", &work.key, &key, version, 0, &cutoff, &body,
                    )?;
                }
                work.current.fill_until = Some(cutoff);
                work.current.next_point += 1;
            }
            if work.current.next_point == range.points.len() {
                finish_range(store, &job, &work, count)?;
                local::set(
                    &store.db,
                    &format!("range:{}", work.key),
                    &serde_json::to_value(&work).map_err(sql)?,
                )?;
                job["range_index"] = json!(index + 1);
                job["work"] = Value::Null;
                local::set(&store.db, "job", &job)?;
                return Ok(());
            }
        }
        job["work"] = serde_json::to_value(work).map_err(sql)?;
        local::set(&store.db, "job", &job)
    })
}
fn finish_range(store: &BrowserStore, job: &Value, work: &Work, count: i64) -> Result<()> {
    let mut range = work
        .current
        .performance
        .range(&job["remote"]["settings"], count);
    let scratch = local::scratch()?;
    let aux: Option<String> = store
        .db
        .query_row("SELECT body FROM facts WHERE record_id=0", [], |r| r.get(0))
        .optional()
        .map_err(sql)?;
    if let Some(aux) = aux {
        local::load(&scratch, &serde_json::from_str(&aux).map_err(sql)?)?;
    }
    scratch
        .execute(
            "INSERT OR REPLACE INTO account VALUES(?1,?2)",
            params![store.account, job["remote"]["trade_channel"].as_str()],
        )
        .map_err(sql)?;
    crate::calendar::enrich(&scratch, store.account, &mut range.header)?;
    let start = range.header["baseline"]
        .as_str()
        .map(crate::calendar::canonical_time)
        .transpose()?
        .unwrap_or_default();
    let end = range.header["end"]
        .as_str()
        .map(crate::calendar::canonical_time)
        .transpose()?
        .unwrap_or_default();
    let total:i64=store.db.query_row("SELECT count(*) FROM local_results WHERE kind='source_event' AND time>=?1 AND time<=?2",params![start,end],|r|r.get(0)).map_err(sql)?;
    let events=store.db.prepare("SELECT body FROM local_results WHERE kind='source_event' AND time>=?1 AND time<=?2 ORDER BY time DESC,CASE WHEN key LIKE 'portfolioaccount:%' THEN 0 WHEN key LIKE 'schedule_skip:%' THEN 1 ELSE 2 END,CAST(substr(key,instr(key,':')+1) AS INTEGER) LIMIT 100").map_err(sql)?.query_map(params![start,end],|r|r.get::<_,String>(0)).map_err(sql)?.collect::<rusqlite::Result<Vec<_>>>().map_err(sql)?;
    for (idx, e) in events.iter().enumerate() {
        let e: Value = serde_json::from_str(e).map_err(sql)?;
        scratch
            .execute(
                "INSERT INTO native_stage.performance_result_event VALUES(?1,?2,?3,?4,?5,?6)",
                params![
                    work.key,
                    idx,
                    e["time"].as_str(),
                    e["tag"].as_str(),
                    e["text"].as_str(),
                    e["execution_id"].as_str()
                ],
            )
            .map_err(sql)?;
    }
    let from = crate::calendar::timestamp_ms(&start).unwrap_or(0.0);
    let until = crate::calendar::timestamp_ms(&end).unwrap_or(-1.0);
    let days = work.cost_days.as_ref().map(|days| json!(days).to_string());
    let day_filter = if days.is_some() {
        "day IN (SELECT value FROM json_each(?3))"
    } else {
        "?3 IS NULL"
    };
    let query=format!("SELECT day,{} FROM performance_cost_trade WHERE record_id IN (SELECT record_id FROM performance_cost_execution WHERE time>=?1 AND time<=?2) AND {day_filter} GROUP BY day ORDER BY day",crate::cost_summary::AGGREGATES);
    let mut s = store.db.prepare(&query).map_err(sql)?;
    let mut rows = s.query(params![from, until, days]).map_err(sql)?;
    while let Some(row) = rows.next().map_err(sql)? {
        let day: String = row.get(0).map_err(sql)?;
        let summary = crate::cost_summary::Summary::read(row, 1).map_err(sql)?;
        let (loss, coverage) = (
            summary
                .loss_bp()
                .filter(|_| summary.covered_value.unwrap_or(0.0) > 0.0),
            summary
                .coverage()
                .filter(|_| summary.value.unwrap_or(0.0) > 0.0),
        );
        let fee_count:i64=store.db.query_row("SELECT count(*) FROM performance_cost_trade t WHERE day=?1 AND fee IS NOT NULL AND fee_currency IS NOT NULL AND record_id IN (SELECT record_id FROM performance_cost_execution WHERE time>=?2 AND time<=?3)",params![day,from,until],|r|r.get(0)).map_err(sql)?;
        scratch.execute("INSERT INTO native_stage.performance_result_daily_cost VALUES(?1,?2,?3,?4,?5,?6,NULL,?7,?8,?9,NULL,?10,255)",params![work.key,day,summary.value,summary.cost,loss,coverage,summary.covered,summary.count,fee_count,summary.amount_complete()]).map_err(sql)?;
        let mut fees=store.db.prepare("SELECT fee_currency,sum(fee) FROM performance_cost_trade WHERE day=?1 AND fee IS NOT NULL AND fee_currency IS NOT NULL AND record_id IN (SELECT record_id FROM performance_cost_execution WHERE time>=?2 AND time<=?3) GROUP BY fee_currency").map_err(sql)?;
        let mut fees = fees.query(params![day, from, until]).map_err(sql)?;
        while let Some(f) = fees.next().map_err(sql)? {
            scratch
                .execute(
                    "INSERT INTO native_stage.performance_result_daily_fee VALUES(?1,?2,?3,?4)",
                    params![
                        work.key,
                        day,
                        f.get::<_, String>(0).map_err(sql)?,
                        f.get::<_, f64>(1).map_err(sql)?
                    ],
                )
                .map_err(sql)?;
        }
    }
    crate::publication::range_rows(&scratch, &work.key, range, total as usize).map_err(sql)?;
    local::save_stage(
        &store.db,
        &scratch,
        &store.schemas,
        job["generation"].as_i64().unwrap(),
        &end,
    )?;
    Ok(())
}
