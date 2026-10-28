//! Incremental runtime hydrated from typed scalar and current-position rows.
use super::*;
use crate::analysis::{self, State};
use crate::error::AnalysisError;
fn error(e: impl ToString) -> AnalysisError {
    analysis::failure("ANALYSIS_COMPUTE", e.to_string())
}
fn range(value: &str) -> &'static str {
    match value {
        "30" => "30",
        "90" => "90",
        _ => "all",
    }
}
fn request(account: i64, key: &str, fee: f64) -> PerformanceRequest {
    PerformanceRequest {
        account_id: account,
        range: Some(range(key)),
        include_backtest: true,
        include_metadata: false,
        require_observation_facts: true,
        expected_source_version: SourceVersionCheck::Skip,
        backtest_fee_rate: fee,
    }
}
pub(crate) fn point(value: Value) -> Result<Point, AnalysisError> {
    let mut v = value;
    v["date"] = v["day"].clone();
    v["sizing_fallback"] = json!(v["sizing_fallback"] == 1);
    serde_json::from_value(v).map_err(error)
}
fn participants(c: &Connection, s: &State, mode: &str) -> Result<Participants, AnalysisError> {
    let target = mode == "target";
    let mut positions = BTreeMap::new();
    for p in analysis::rows(
        c,
        "SELECT * FROM analysis_replay_position WHERE account_id=?1 AND range_key=?2 AND mode=?3",
        &[&s.account_id, &s.range_key, &mode],
    )? {
        positions.insert(
            p["symbol"].as_str().unwrap().to_owned(),
            crate::replay::Holding {
                units: p["units"].as_f64().unwrap(),
                quantity: p["quantity"].as_f64(),
                mark: p["mark"].as_f64().unwrap(),
            },
        );
    }
    let (start, used, skips, equity, initialized, previous, day, close) = if target {
        (
            &s.target_start,
            s.target_used,
            Skips {
                count: s.target_skipped as usize,
                missing_target: s.target_missing_target as usize,
                missing_ticks: s.target_missing_price as usize,
                first_time: s.target_skip_first.clone(),
                last_time: s.target_skip_last.clone(),
            },
            s.target_equity,
            s.target_initialized,
            s.target_previous_close,
            &s.target_close_day,
            s.target_close_equity,
        )
    } else {
        (
            &s.executable_start,
            s.used,
            Skips {
                count: s.skipped as usize,
                missing_target: s.missing_target as usize,
                missing_ticks: s.missing_price as usize,
                first_time: s.skip_first.clone(),
                last_time: s.skip_last.clone(),
            },
            s.executable_equity,
            s.executable_initialized,
            s.executable_previous_close,
            &s.executable_close_day,
            s.executable_close_equity,
        )
    };
    let previous_symbols = positions.keys().cloned().collect();
    let ledger = s.initial_equity.map(|initial| ReplayLedger {
        initial_equity: initial,
        equity: equity.unwrap_or(initial),
        fee_rate: s.fee_rate,
        target_weight: target,
        initialized: initialized == 1,
        previous_close: previous.unwrap_or(initial),
        positions,
        closes: day
            .as_ref()
            .zip(close)
            .map(|(d, v)| BTreeMap::from([(d.clone(), v)]))
            .unwrap_or_default(),
    });
    Ok(Participants {
        previous_symbols,
        used: used as usize,
        start: start.clone(),
        skips,
        ledger,
    })
}
fn hydrate(c: &Connection, s: &State) -> Result<RangeState, AnalysisError> {
    let baseline = s
        .baseline_time
        .as_ref()
        .zip(s.baseline_id)
        .map(|(time, id)| Sample {
            time: time.clone(),
            id,
            execution_id: s.baseline_execution.clone(),
            asset: s.baseline_asset,
        });
    let mut days = BTreeMap::new();
    if let Some(day) = &s.current_day {
        let last = Sample {
            time: s
                .last_time
                .as_deref()
                .map(display_time)
                .transpose()
                .map_err(error)?
                .unwrap_or_default(),
            id: s.last_id.unwrap(),
            execution_id: s.last_execution.clone(),
            asset: s.last_asset,
        };
        let participant = if s.day_participant == 1 {
            analysis::rows(
                c,
                "SELECT * FROM analysis_day WHERE account_id=?1 AND range_key=?2 AND day=?3",
                &[&s.account_id, &s.range_key, day],
            )?
            .pop()
            .map(|p| Sample {
                time: p["observed_at"].as_str().unwrap().into(),
                id: p["record_id"].as_i64().unwrap(),
                execution_id: p["execution_id"].as_str().map(str::to_owned),
                asset: p["account_equity"].as_f64(),
            })
        } else {
            None
        };
        days.insert(day.clone(), DaySamples { last, participant });
    }
    Ok(RangeState{key:s.range_key.clone(),start:s.start.clone(),count:s.observations as usize,invalid_assets:s.invalid_assets as usize,end:s.last_time.clone(),baseline,days,executable:participants(c,s,"executable")?,target:participants(c,s,"target")?,fallback_days:analysis::rows(c,"SELECT day FROM analysis_day WHERE account_id=?1 AND range_key=?2 AND day=?3 AND sizing_fallback=1",&[&s.account_id,&s.range_key,&s.current_day])?.iter().filter_map(|p|p["day"].as_str().map(str::to_owned)).collect(),fallback_count:s.fallback_count as usize,fallback_ranges:Vec::new(),in_fallback:false})
}

fn baseline_moves(c: &Connection, s: &State, last: &str) -> Result<bool, AnalysisError> {
    if s.baseline_asset.is_none() {
        return Ok(true);
    }
    let mut reader = crate::fact_observation::ObservationReader::new(c).map_err(analysis::sql)?;
    let confirmed = s
        .baseline_id
        .map(|id| reader.read(s.account_id, id).map_err(error))
        .transpose()?
        .as_ref()
        .is_some_and(baseline_eligible);
    if confirmed && s.range_key == "all" {
        return Ok(false);
    }
    let cutoff = if confirmed {
        canonical_time(
            parse_time(last).map_err(error)?
                - Duration::days(s.range_key.parse::<i64>().map_err(error)?),
        )
    } else {
        last.to_owned()
    };
    let after = if confirmed {
        s.baseline_time.as_deref()
    } else {
        s.last_time.as_deref()
    };
    let ids=analysis::rows(c,"SELECT max(id) AS id FROM executerecord WHERE account_id=?1 AND result_staged=0 AND axile_time(created_at)>?2 AND axile_time(created_at)<=?3 GROUP BY axile_time(created_at) ORDER BY axile_time(created_at) DESC",&[&s.account_id,&after.map(crate::calendar::canonical_time).transpose().map_err(error)?.unwrap_or_default(),&cutoff])?;
    for id in ids {
        let item = reader
            .read(s.account_id, id["id"].as_i64().unwrap())
            .map_err(error)?;
        if baseline_eligible(&item) {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn compute(
    source: &Connection,
    stage: &Connection,
    account: i64,
    key: &str,
    version: i64,
    now: &str,
) -> Result<(bool, usize), AnalysisError> {
    let (channel,raw):(String,Option<String>)=source.query_row("SELECT a.trade_channel,s.backtest FROM account a LEFT JOIN account_settings s ON s.account_id=a.id WHERE a.id=?1",[account],|r|Ok((r.get(0)?,r.get(1)?))).map_err(analysis::sql)?;
    let settings: Value = raw
        .map(|v| serde_json::from_str(&v).map_err(error))
        .transpose()?
        .unwrap_or_else(|| json!({}));
    let fee = settings["backtest_fee_rate"].as_f64().unwrap_or(0.0);
    if !fee.is_finite() || !(0.0..1.0).contains(&fee) {
        return Err(error("invalid fee rate"));
    }
    let old = analysis::state(source, account, key)?;
    let dirty: Option<i64> = source
        .query_row(
            "SELECT dirty_record FROM account_analysis WHERE account_id=?1",
            [account],
            |r| r.get(0),
        )
        .map_err(analysis::sql)?;
    let maximum: i64 = source
        .query_row(
            "SELECT COALESCE(max(id),0) FROM executerecord WHERE account_id=?1 AND result_staged=0",
            [account],
            |r| r.get(0),
        )
        .map_err(analysis::sql)?;
    let pending=analysis::rows(source,"SELECT max(id) AS id,axile_time(created_at) AS time FROM executerecord WHERE account_id=?1 AND result_staged=0 AND id>?2 GROUP BY axile_time(created_at) ORDER BY axile_time(created_at)",&[&account,&old.as_ref().map_or(0,|s|s.max_id)])?;
    let reset = needs_replay(source, old.as_ref(), dirty, &pending, fee, &channel)?;
    let mut runtime = if reset {
        calculate_states(source, &request(account, key, fee))
            .map_err(error)?
            .0
            .remove(0)
    } else {
        hydrate(source, old.as_ref().unwrap())?
    };
    let mut processed = if reset { runtime.count } else { 0 };
    if !reset {
        crate::fact_integrity::validate_since(source, account, old.as_ref().unwrap().max_id)
            .map_err(error)?;
        let mut reader =
            crate::fact_observation::ObservationReader::new(source).map_err(analysis::sql)?;
        for p in &pending {
            let mut item = reader
                .read(account, p["id"].as_i64().unwrap())
                .map_err(error)?;
            item.time = display_time(&item.time).map_err(error)?;
            runtime
                .push(&item, p["time"].as_str().unwrap(), true)
                .map_err(error)?;
            processed += 1;
        }
    }
    let mut saved = if reset {
        State::default()
    } else {
        old.unwrap()
    };
    saved.account_id = account;
    saved.range_key = key.into();
    saved.version = version;
    saved.logic_version = analysis::LOGIC_VERSION.into();
    saved.engine_version = ENGINE_VERSION.into();
    saved.computed_at = now.into();
    saved.fee_rate = fee;
    saved.trade_channel = channel;
    saved.max_id = maximum;
    save_runtime(stage, &runtime, &mut saved)?;
    let first = runtime.days.keys().next().cloned().unwrap_or_default();
    let seed = if reset {
        None
    } else {
        analysis::rows(source,"SELECT * FROM analysis_day WHERE account_id=?1 AND range_key=?2 AND day<?3 ORDER BY day DESC LIMIT 1",&[&account,&key,&first])?.pop().map(point).transpose()?
    };
    let result = runtime.finish(
        &json!({"backtest_weight_type":"cs","backtest_fee_rate":fee}),
        0,
        true,
        seed.as_ref(),
    );
    let points = result.points.into_iter().skip(1).collect::<Vec<_>>();
    for p in &points {
        let mut body = serde_json::to_value(p).map_err(error)?;
        let body = body.as_object_mut().unwrap();
        let day = body.remove("date").unwrap();
        body.insert("day".into(), day);
        body.insert("account_id".into(), json!(account));
        body.insert("range_key".into(), json!(key));
        analysis::put(stage, "analysis_day", &Value::Object(body.clone()))?;
    }
    crate::analysis::contribution::advance(source, stage, &mut saved, &points, reset)?;
    analysis::put(
        stage,
        "analysis_state",
        &serde_json::to_value(saved).map_err(error)?,
    )?;
    Ok((reset, processed))
}

fn save_runtime(stage: &Connection, r: &RangeState, s: &mut State) -> Result<(), AnalysisError> {
    s.start = r.start.clone();
    s.last_time = r
        .end
        .as_deref()
        .map(crate::calendar::canonical_time)
        .transpose()
        .map_err(error)?;
    s.observations = r.count as i64;
    s.invalid_assets = r.invalid_assets as i64;
    s.fallback_count = r.fallback_count as i64;
    if let Some(b) = &r.baseline {
        s.baseline_time = Some(b.time.clone());
        s.baseline_id = Some(b.id);
        s.baseline_execution = b.execution_id.clone();
        s.baseline_asset = b.asset;
    }
    if let Some((day, d)) = r.days.iter().next_back() {
        s.current_day = Some(day.clone());
        s.last_id = Some(d.last.id);
        s.last_asset = d.last.asset;
        s.last_execution = d.last.execution_id.clone();
        s.day_participant = i64::from(d.participant.is_some());
    }
    for (mode, p) in [("executable", &r.executable), ("target", &r.target)] {
        let (equity, init, previous, day, close) = if let Some(l) = &p.ledger {
            s.initial_equity = Some(l.initial_equity);
            for (symbol, h) in &l.positions {
                analysis::put(
                    stage,
                    "analysis_replay_position",
                    &json!({"account_id":s.account_id,"range_key":s.range_key,"mode":mode,"symbol":symbol,"quantity":h.quantity,"units":h.units,"mark":h.mark}),
                )?;
            }
            (
                Some(l.equity),
                i64::from(l.initialized),
                Some(
                    l.closes
                        .values()
                        .rev()
                        .nth(1)
                        .copied()
                        .unwrap_or(l.previous_close),
                ),
                l.closes.keys().next_back().cloned(),
                l.closes.values().next_back().copied(),
            )
        } else {
            (None, 0, None, None, None)
        };
        if mode == "executable" {
            s.executable_equity = equity;
            s.executable_initialized = init;
            s.executable_previous_close = previous;
            s.executable_close_day = day;
            s.executable_close_equity = close;
            s.executable_start = p.start.clone();
            s.used = p.used as i64;
            s.skipped = p.skips.count as i64;
            s.missing_target = p.skips.missing_target as i64;
            s.missing_price = p.skips.missing_ticks as i64;
            s.skip_first = p.skips.first_time.clone();
            s.skip_last = p.skips.last_time.clone();
        } else {
            s.target_equity = equity;
            s.target_initialized = init;
            s.target_previous_close = previous;
            s.target_close_day = day;
            s.target_close_equity = close;
            s.target_start = p.start.clone();
            s.target_used = p.used as i64;
            s.target_skipped = p.skips.count as i64;
            s.target_missing_target = p.skips.missing_target as i64;
            s.target_missing_price = p.skips.missing_ticks as i64;
            s.target_skip_first = p.skips.first_time.clone();
            s.target_skip_last = p.skips.last_time.clone();
        }
    }
    Ok(())
}

fn needs_replay(
    source: &Connection,
    old: Option<&State>,
    dirty: Option<i64>,
    pending: &[Value],
    fee: f64,
    channel: &str,
) -> Result<bool, AnalysisError> {
    let mut reset = old.is_none_or(|s| {
        s.logic_version != analysis::LOGIC_VERSION
            || s.engine_version != ENGINE_VERSION
            || s.fee_rate != fee
            || s.trade_channel != channel
            || dirty.is_some_and(|id| id <= s.max_id)
            || pending.first().is_some_and(|p| {
                s.last_time
                    .as_deref()
                    .is_some_and(|t| p["time"].as_str().unwrap() <= t)
            })
    });
    if !reset {
        if let (Some(s), Some(last)) = (old, pending.last()) {
            reset = baseline_moves(source, s, last["time"].as_str().unwrap())?;
        }
    }
    Ok(reset)
}
