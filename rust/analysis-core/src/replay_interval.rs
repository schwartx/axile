//! Fixed-memory interval reductions. Observation cutoffs and fact inclusion are separate.
use serde_json::{json, Value};
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

const SAFE: u64 = 9_007_199_254_740_991;
fn known(data: &Value, field: &str) -> bool {
    data["field_quality"][field]
        .as_str()
        .is_none_or(|v| v == "known")
        && !data[field].is_null()
}
fn applies(data: &Value, symbol: &str) -> bool {
    data["scope"]["kind"] == "account"
        || data["symbol"] == symbol
        || data["scope"].is_null() && data["symbol"].is_null()
        || data["scope"]
            .as_array()
            .or_else(|| data["scope"]["symbols"].as_array())
            .is_some_and(|a| a.iter().any(|s| s == symbol || s == "*"))
}

#[derive(Clone, Copy, Default)]
struct Sum {
    value: f64,
    compensation: f64,
}
impl Sum {
    fn add(&mut self, value: f64) -> Result<(), String> {
        let adjusted = value - self.compensation;
        let next = self.value + adjusted;
        if !next.is_finite() {
            return Err("REPLAY_INTERVAL_OVERFLOW".into());
        }
        self.compensation = (next - self.value) - adjusted;
        self.value = next;
        Ok(())
    }
}
struct Trades {
    count: u64,
    weak: u64,
    unknown: u64,
    unowned: u64,
    complete: bool,
    quantity: Sum,
    amount: Sum,
    min: Option<i64>,
    max: Option<i64>,
    width: i64,
    bins: [Sum; 128],
    counts: [u64; 128],
    first_hash: DefaultHasher,
    second_hash: DefaultHasher,
    timeline: Vec<(u64, Sum, Sum, u64)>,
}
impl Trades {
    fn new() -> Self {
        Self {
            count: 0,
            weak: 0,
            unknown: 0,
            unowned: 0,
            complete: true,
            quantity: Sum::default(),
            amount: Sum::default(),
            min: None,
            max: None,
            width: 1,
            bins: [Sum::default(); 128],
            counts: [0; 128],
            first_hash: DefaultHasher::new(),
            second_hash: DefaultHasher::new(),
            timeline: vec![(0, Sum::default(), Sum::default(), 0); 128],
        }
    }
    fn feed(&mut self, row: &Value, own: bool, request: &Interval) -> Result<(), String> {
        let phase = request.second;
        row.to_string().hash(if phase {
            &mut self.second_hash
        } else {
            &mut self.first_hash
        });
        if row["first_seq"] == 0 || row["resolved_seq"].as_u64().unwrap_or(0) > 0 {
            return Ok(());
        }
        let d = &row["data"];
        let observed = row["first_time_us"]
            .as_u64()
            .ok_or("REPLAY_INTERVAL_OBSERVATION")?;
        if observed < request.start
            || observed >= request.end
            || d["symbol"].as_str().is_some_and(|s| s != request.symbol)
        {
            return Ok(());
        }
        let owner = d["execution_id"].as_str();
        if own {
            if owner.is_none()
                && matches!(d["origin"].as_str(), Some("manual" | "external"))
                && row["owner_conflict"] != true
            {
                return Ok(());
            }
            if owner.is_none() || row["owner_conflict"] == true {
                if !phase {
                    self.unowned += 1;
                    self.complete = false;
                }
            }
            if owner.is_some_and(|id| id != request.execution) {
                return Ok(());
            }
        }
        let stable = matches!(row["kind"].as_str(), Some("trade" | "market_trade"));
        let bucket = (((observed - request.start) as u128 * 128)
            / (request.end - request.start) as u128)
            .min(127) as usize;
        if !phase {
            if stable {
                self.count += 1
            } else {
                self.weak += 1
            }
        }
        if !stable
            || row["valid"] != true
            || d["symbol"].as_str().is_none()
            || own && (owner.is_none() || row["owner_conflict"] == true)
        {
            if !phase {
                self.unknown += 1;
                self.complete = false;
                if request.viewport {
                    self.timeline[bucket].3 += 1;
                }
            }
            return Ok(());
        }
        let p = d["price"].as_f64().ok_or("REPLAY_INTERVAL_PRICE")?;
        let q = d["quantity"]
            .as_f64()
            .filter(|v| *v > 0.0)
            .ok_or("REPLAY_INTERVAL_QUANTITY")?;
        let raw = p / request.tick;
        let tick = raw.round();
        let tick = if tick.is_finite()
            && tick.abs() <= SAFE as f64
            && (raw - tick).abs() <= 1e-7_f64.max(raw.abs() * 1e-12)
        {
            Some(tick as i64)
        } else {
            None
        };
        if !phase {
            self.quantity.add(q)?;
            self.amount.add(p * q)?;
            if request.viewport {
                let bin = &mut self.timeline[bucket];
                bin.0 += 1;
                bin.1.add(q)?;
                bin.2.add(p * q)?;
            }
            if request.distribution {
                if let Some(t) = tick {
                    self.min = Some(self.min.map_or(t, |v| v.min(t)));
                    self.max = Some(self.max.map_or(t, |v| v.max(t)));
                } else {
                    self.complete = false;
                }
            }
        } else if let (Some(t), Some(origin)) = (tick, self.min) {
            let index = (t as i128 - origin as i128) / self.width as i128;
            if t < origin || !(0..128).contains(&index) {
                return Err("REPLAY_INTERVAL_GRID_CHANGED".into());
            }
            self.bins[index as usize].add(q)?;
            self.counts[index as usize] += 1;
        }
        Ok(())
    }
    fn second(&mut self) {
        if let (Some(min), Some(max)) = (self.min, self.max) {
            self.width = (((max as i128 - min as i128 + 1) + 127) / 128).max(1) as i64;
        }
    }
    fn result(&self, distribution: bool, second: bool, viewport: bool) -> Result<Value, String> {
        if distribution && (!second || self.first_hash.finish() != self.second_hash.finish()) {
            return Err("REPLAY_INTERVAL_PASSES".into());
        }
        Ok(
            json!({"stable_count":self.count,"weak_count":self.weak,"unknown_count":self.unknown,"unowned_count":self.unowned,"complete":self.complete,
            "known_quantity":self.quantity.value,"known_amount":self.amount.value,"vwap":if self.quantity.value>0.0 {Some(self.amount.value/self.quantity.value)}else{None},
            "timeline":if viewport {json!(self.timeline.iter().enumerate().filter(|(_,b)|b.0>0||b.3>0).map(|(index,b)|json!({"index":index,"count":b.0,"quantity":b.1.value,"vwap":if b.1.value>0.0 {Some(b.2.value/b.1.value)}else{None},"unknown":b.3})).collect::<Vec<_>>())}else{Value::Null},
            "distribution":if distribution {json!({"origin_tick":self.min,"width_ticks":self.width,"bins":self.counts.iter().zip(self.bins).enumerate().filter(|(_,(&count,_))|count>0).map(|(index,(&count,sum))|json!({"index":index,"count":count,"quantity":sum.value})).collect::<Vec<_>>()})}else{Value::Null}}),
        )
    }
}

pub struct Interval {
    recording: String,
    symbol: String,
    execution: String,
    start: u64,
    end: u64,
    cutoff: u64,
    tick: f64,
    distribution: bool,
    point: bool,
    viewport: bool,
    quotes: Vec<Option<Value>>,
    last_quote_slot: Option<usize>,
    second: bool,
    own: Trades,
    market: Trades,
    last_time: u64,
    last_seq: u64,
    quote: Option<(f64, Option<f64>, u64)>,
    quote_capture: bool,
    market_capture: bool,
    own_capture: bool,
    quote_seen: bool,
    market_seen: bool,
    own_seen: bool,
    connected: bool,
    generation: Option<u64>,
    spread: Sum,
    imbalance: Sum,
    spread_time: u64,
    imbalance_time: u64,
    quote_time: u64,
    market_time: u64,
    own_time: u64,
}
impl Interval {
    pub fn new(request: &Value) -> Result<Self, String> {
        let recording = request["recording"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 128)
            .ok_or("REPLAY_INTERVAL_RECORDING")?
            .to_owned();
        let symbol = request["scope_symbol"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 1024)
            .ok_or("REPLAY_INTERVAL_SCOPE")?
            .to_owned();
        let start = request["interval_start_us"]
            .as_u64()
            .ok_or("REPLAY_INTERVAL_START")?;
        let end = request["interval_end_us"]
            .as_u64()
            .ok_or("REPLAY_INTERVAL_END")?;
        let cutoff = request["cutoff_seq"]
            .as_u64()
            .filter(|n| *n <= SAFE)
            .ok_or("REPLAY_INTERVAL_CUTOFF")?;
        let tick = request["price_tick"]
            .as_f64()
            .filter(|p| p.is_finite() && *p > 0.0)
            .ok_or("REPLAY_INTERVAL_TICK")?;
        if start >= end || end > SAFE || request["point"] == true && end - start != 1 {
            return Err("REPLAY_INTERVAL_RANGE".into());
        }
        let mut own = Trades::new();
        own.complete = request["start_observed"] == true;
        Ok(Self {
            recording,
            symbol,
            execution: request["execution"].as_str().unwrap_or("").into(),
            start,
            end,
            cutoff,
            tick,
            distribution: request["distribution"] == true,
            point: request["point"] == true,
            viewport: request["viewport"] == true,
            quotes: if request["viewport"] == true {
                vec![None; 128]
            } else {
                Vec::new()
            },
            last_quote_slot: None,
            second: false,
            own,
            market: Trades::new(),
            last_time: 0,
            last_seq: 0,
            quote: None,
            quote_capture: false,
            market_capture: false,
            own_capture: request["start_observed"] == true,
            quote_seen: false,
            market_seen: false,
            own_seen: false,
            connected: true,
            generation: None,
            spread: Sum::default(),
            imbalance: Sum::default(),
            spread_time: 0,
            imbalance_time: 0,
            quote_time: 0,
            market_time: 0,
            own_time: 0,
        })
    }
    pub fn feed(&mut self, request: &Value) -> Result<(), String> {
        let rows = request["subjects"]
            .as_array()
            .filter(|a| a.len() <= 128)
            .ok_or("REPLAY_INTERVAL_BATCH")?;
        // Temporarily move a fixed reducer so the immutable parameters remain available during its fold.
        let own = request["source"] == "own";
        if !own && request["source"] != "market" {
            return Err("REPLAY_INTERVAL_SOURCE".into());
        }
        let mut reducer = if own {
            std::mem::replace(&mut self.own, Trades::new())
        } else {
            std::mem::replace(&mut self.market, Trades::new())
        };
        for row in rows {
            reducer.feed(row, own, self)?;
        }
        if own {
            self.own = reducer
        } else {
            self.market = reducer
        }
        Ok(())
    }
    pub fn begin_second(&mut self) -> Result<(), String> {
        if !self.distribution || self.second {
            return Err("REPLAY_INTERVAL_PHASE".into());
        }
        self.own.second();
        self.market.second();
        self.second = true;
        Ok(())
    }
    fn advance(&mut self, time: u64) -> Result<(), String> {
        if time < self.last_time {
            return Err("REPLAY_INTERVAL_ORDER".into());
        }
        let start = self.last_time.max(self.start);
        let end = time.min(self.end);
        if end > start && self.connected {
            if self.quote_capture {
                self.quote_time += end - start;
            }
            if self.market_capture {
                self.market_time += end - start;
            }
            if self.own_capture {
                self.own_time += end - start;
            }
            if self.quote_capture {
                if let Some((spread, imbalance, expiry)) = self.quote {
                    let until = end.min(expiry);
                    if until > start {
                        let span = until - start;
                        self.spread.add(spread * span as f64)?;
                        self.spread_time += span;
                        if let Some(value) = imbalance {
                            self.imbalance.add(value * span as f64)?;
                            self.imbalance_time += span;
                        }
                    }
                }
            }
        }
        self.last_time = time;
        Ok(())
    }
    fn clip_quotes(&mut self, time: u64) {
        if let Some(row) = self.last_quote_slot.and_then(|i| self.quotes[i].as_mut()) {
            if row["until_us"].as_u64().is_some_and(|until| until > time) {
                row["until_us"] = json!(time);
            }
        }
    }
    pub fn observe(&mut self, event: &Value) -> Result<(), String> {
        let seq = event["capture_seq"]
            .as_u64()
            .ok_or("REPLAY_INTERVAL_SEQUENCE")?;
        if event["recording_id"] != self.recording || seq == 0 || seq > SAFE {
            return Err("REPLAY_INTERVAL_SOURCE".into());
        }
        if seq <= self.last_seq {
            return Err("REPLAY_INTERVAL_ORDER".into());
        }
        self.last_seq = seq;
        if seq > self.cutoff {
            return Ok(());
        }
        let time = event["time_us"].as_u64().ok_or("REPLAY_INTERVAL_TIME")?;
        self.advance(time)?;
        let d = &event["data"];
        let topic = event["topic"].as_str().unwrap_or("");
        if topic == "/connection" && applies(d, &self.symbol) {
            if let Some(generation) = d["sync_generation"].as_u64() {
                if self.generation.is_some_and(|old| generation < old) {
                    return Ok(());
                }
                if self.generation != Some(generation) {
                    self.clip_quotes(time);
                    self.quote = None;
                    self.generation = Some(generation);
                }
            }
            if crate::replay::connection_unavailable(d) {
                self.connected = false;
                self.clip_quotes(time);
                self.quote = None;
            } else if ["connected", "ready", "available"]
                .iter()
                .any(|f| known(d, f) && d[*f] == true)
            {
                self.connected = true;
            }
        }
        if topic == "/capture" && applies(d, &self.symbol) {
            for (name, state, seen) in [
                (
                    "/trade".to_owned(),
                    &mut self.own_capture,
                    &mut self.own_seen,
                ),
                (
                    format!("/quote/{}", self.symbol),
                    &mut self.quote_capture,
                    &mut self.quote_seen,
                ),
                (
                    format!("/market_trade/{}", self.symbol),
                    &mut self.market_capture,
                    &mut self.market_seen,
                ),
            ] {
                if d["streams"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|s| s == &name))
                {
                    *seen = true;
                    *state = d["boundary"] == "start";
                }
            }
            if !self.quote_capture {
                self.clip_quotes(time);
                self.quote = None;
            }
        }
        if topic == format!("/quote/{}", self.symbol) {
            self.clip_quotes(time);
            self.quote = None;
            if known(d, "book_valid")
                && d["book_valid"] == true
                && self.connected
                && d["available"] != false
                && d["ready"] != false
                && d["connected"] != false
                && self
                    .generation
                    .is_none_or(|g| d["sync_generation"].as_u64() == Some(g))
                && known(d, "bids")
                && known(d, "asks")
            {
                let level = |side: &str| -> Option<(f64, f64)> {
                    let row = d[side].as_array()?.first()?;
                    let p = row["price"].as_f64().or_else(|| row[0].as_f64())?;
                    let q = row["quantity"].as_f64().or_else(|| row[1].as_f64())?;
                    if p.is_finite() && q.is_finite() && q >= 0.0 {
                        Some((p, q))
                    } else {
                        None
                    }
                };
                let expiry = if known(d, "valid_until_us") {
                    d["valid_until_us"].as_u64()
                } else if self.point
                    && time == self.start
                    && d["valid_until_us"].is_null()
                    && d["field_quality"]["valid_until_us"] != "invalid"
                {
                    time.checked_add(1)
                } else {
                    None
                };
                if let (Some((bid, bq)), Some((ask, aq)), Some(expiry)) =
                    (level("bids"), level("asks"), expiry)
                {
                    if ask >= bid && expiry > time {
                        self.generation = self.generation.or(d["sync_generation"].as_u64());
                        self.quote = Some((
                            ask - bid,
                            if bq + aq > 0.0 {
                                Some((bq - aq) / (bq + aq))
                            } else {
                                None
                            },
                            expiry,
                        ));
                    }
                }
            }
        }
        if self.viewport && topic == format!("/quote/{}", self.symbol) && time < self.end {
            let index = (((time.saturating_sub(self.start)) as u128 * 128)
                / (self.end - self.start) as u128)
                .min(127) as usize;
            let levels = |side: &str| {
                d[side]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .take(8)
                    .filter_map(|v| {
                        let p = v["price"].as_f64().or_else(|| v[0].as_f64())?;
                        let q = v["quantity"].as_f64().or_else(|| v[1].as_f64())?;
                        (p.is_finite() && q.is_finite() && q >= 0.0).then(|| json!([p, q]))
                    })
                    .collect::<Vec<_>>()
            };
            let until = if self.connected && self.quote_capture {
                self.quote
                    .map(|(_, _, expiry)| expiry.min(self.end))
                    .unwrap_or(time)
            } else {
                time
            };
            self.quotes[index] = Some(
                json!({"time_us":time.max(self.start),"observed_us":time,"capture_seq":seq,"until_us":until,"bids":levels("bids"),"asks":levels("asks")}),
            );
            self.last_quote_slot = Some(index);
        }
        Ok(())
    }
    pub fn results(&mut self) -> Result<Value, String> {
        self.advance(self.end.max(self.last_time))?;
        let duration = self.end - self.start;
        let mut own = self
            .own
            .result(self.distribution, self.second, self.viewport)?;
        own["complete"] = json!(own["complete"] == true && self.own_time == duration);
        let mut market = self
            .market
            .result(self.distribution, self.second, self.viewport)?;
        market["complete"] =
            json!(market["complete"] == true && self.market_seen && self.market_time == duration);
        Ok(
            json!({"interval_start_us":self.start,"interval_end_us":self.end,"symbol":self.symbol,"price_tick":self.tick,"own":own,"market":market,
            "viewport":if self.viewport {json!({"buckets":128,"quotes":self.quotes.iter().flatten().collect::<Vec<_>>()})}else{Value::Null},
            "book":{"spread_mean":if self.spread_time>0 {Some(self.spread.value/self.spread_time as f64)}else{None},"imbalance_mean":if self.imbalance_time>0 {Some(self.imbalance.value/self.imbalance_time as f64)}else{None},
                "spread_valid_us":self.spread_time,"imbalance_valid_us":self.imbalance_time,"quote_capture_us":self.quote_time,"market_capture_us":self.market_time,"own_capture_us":self.own_time,"query_us":duration,
                "coverage":self.spread_time as f64/duration as f64,"complete":self.quote_seen && self.quote_time==duration && self.spread_time==duration && self.imbalance_time==duration}}),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request() -> Value {
        json!({"recording":"R","scope_symbol":"S","execution":"E","start_observed":true,"interval_start_us":10,"interval_end_us":30,"cutoff_seq":100,"price_tick":1,"distribution":true})
    }
    #[test]
    fn absent_expiry_is_available_only_at_the_observation_in_point_mode() {
        for (point, start, expected) in [(true, 5, 1), (true, 6, 0), (false, 5, 0)] {
            let mut request = request();
            request["point"] = json!(point);
            request["interval_start_us"] = json!(start);
            request["interval_end_us"] = json!(start + 1);
            request["distribution"] = json!(false);
            let mut task = Interval::new(&request).unwrap();
            task.observe(&json!({"recording_id":"R","capture_seq":1,"time_us":1,"topic":"/capture","data":{"symbol":"S","streams":["/quote/S"],"boundary":"start"}})).unwrap();
            task.observe(&json!({"recording_id":"R","capture_seq":2,"time_us":5,"topic":"/quote/S","data":{"symbol":"S","book_valid":true,"bids":[[100,3]],"asks":[[102,1]]}})).unwrap();
            assert_eq!(task.results().unwrap()["book"]["spread_valid_us"], expected);
        }
    }
    #[test]
    fn invalid_connection_quality_cannot_extend_a_healthy_interval() {
        let mut input = request();
        input["distribution"] = json!(false);
        let mut task = Interval::new(&input).unwrap();
        task.observe(&json!({"recording_id":"R","capture_seq":1,"time_us":1,"topic":"/capture","data":{"symbol":"S","streams":["/quote/S"],"boundary":"start"}})).unwrap();
        task.observe(&json!({"recording_id":"R","capture_seq":2,"time_us":5,"topic":"/quote/S","data":{"symbol":"S","book_valid":true,"valid_until_us":100,"bids":[[100,3]],"asks":[[102,1]]}})).unwrap();
        task.observe(&json!({"recording_id":"R","capture_seq":3,"time_us":12,"topic":"/connection","data":{"scope":{"kind":"account"},"connected":null,"field_quality":{"connected":"invalid"}}})).unwrap();
        let out = task.results().unwrap();
        assert_eq!(out["book"]["spread_valid_us"], 2);
        assert_eq!(out["own"]["complete"], false);
    }
    #[test]
    fn reconnect_requires_a_fresh_quote_in_the_current_generation() {
        let mut request = request();
        request["distribution"] = json!(false);
        let mut task = Interval::new(&request).unwrap();
        let event = |seq, time, topic, data| json!({"schema_version":2,"recording_id":"R","capture_seq":seq,"time_us":time,"topic":topic,"data":data});
        task.observe(&event(
            1,
            1,
            "/capture",
            json!({"scope":["S"],"streams":["/quote/S"],"boundary":"start"}),
        ))
        .unwrap();
        let quote = json!({"symbol":"S","book_valid":true,"bids":[[100,3]],"asks":[[102,1]],"valid_until_us":100});
        task.observe(&event(2, 5, "/quote/S", quote.clone()))
            .unwrap();
        task.observe(&event(
            3,
            12,
            "/connection",
            json!({"scope":{"kind":"account"},"connected":false,"sync_generation":1}),
        ))
        .unwrap();
        task.observe(&event(
            4,
            14,
            "/connection",
            json!({"scope":{"kind":"account"},"connected":true,"sync_generation":1}),
        ))
        .unwrap();
        task.observe(&event(5, 16, "/quote/S", quote.clone()))
            .unwrap();
        let mut unavailable = quote.clone();
        unavailable["sync_generation"] = json!(1);
        unavailable["available"] = json!(false);
        task.observe(&event(6, 18, "/quote/S", unavailable))
            .unwrap();
        let mut fresh = quote;
        fresh["sync_generation"] = json!(1);
        task.observe(&event(7, 20, "/quote/S", fresh)).unwrap();
        let out = task.results().unwrap();
        assert_eq!(out["book"]["spread_valid_us"], 12);
        assert_eq!(out["book"]["spread_mean"], 2.0);
    }
    #[test]
    fn window_is_left_closed_and_late_knowledge_does_not_move_original_observation() {
        let mut request = request();
        request["distribution"] = json!(false);
        let mut task = Interval::new(&request).unwrap();
        let mut before = trade(1, 10.0, 1.0);
        before["first_time_us"] = json!(9);
        before["data"]["execution_id"] = Value::Null;
        let inside = trade(2, 10.0, 2.0);
        let mut at_end = trade(3, 100.0, 3.0);
        at_end["first_time_us"] = json!(30);
        task.feed(&json!({"source":"own","subjects":[before,inside,at_end]}))
            .unwrap();
        let output = task.results().unwrap();
        assert_eq!(output["own"]["stable_count"], 1);
        assert_eq!(output["own"]["known_quantity"], 2.0);
        assert_eq!(output["own"]["unowned_count"], 0);
    }
    #[test]
    fn viewport_samples_are_bounded_clip_disconnect_and_do_not_double_count_trades() {
        let mut input = request();
        input["viewport"] = json!(true);
        input["interval_end_us"] = json!(400);
        let mut task = Interval::new(&input).unwrap();
        task.observe(&json!({"recording_id":"R","capture_seq":1,"time_us":1,"topic":"/capture","data":{"symbol":"S","streams":["/quote/S"],"boundary":"start"}})).unwrap();
        for i in 2..=300 {
            task.cutoff = 400;
            task.observe(&json!({"recording_id":"R","capture_seq":i,"time_us":i,"topic":"/quote/S","data":{"symbol":"S","book_valid":true,"valid_until_us":400,"bids":vec![[100,1];20],"asks":vec![[102,1];20]}})).unwrap();
        }
        task.observe(&json!({"recording_id":"R","capture_seq":301,"time_us":301,"topic":"/connection","data":{"scope":{"kind":"account"},"connected":false}})).unwrap();
        let row = trade(1, 100.0, 2.0);
        task.feed(&json!({"source":"own","subjects":[row.clone()]}))
            .unwrap();
        task.begin_second().unwrap();
        task.feed(&json!({"source":"own","subjects":[row]}))
            .unwrap();
        let out = task.results().unwrap();
        let quotes = out["viewport"]["quotes"].as_array().unwrap();
        assert!(quotes.len() <= 128);
        assert!(quotes
            .iter()
            .all(|q| q["bids"].as_array().unwrap().len() <= 8
                && q["until_us"].as_u64().unwrap() <= 301));
        assert_eq!(out["own"]["timeline"][0]["quantity"], 2.0);
        assert_eq!(out["own"]["known_quantity"], 2.0);
    }
    #[test]
    fn distribution_refuses_changed_evidence_between_passes() {
        let mut task = Interval::new(&request()).unwrap();
        task.feed(&json!({"source":"own","subjects":[trade(1,100.0,1.0)]}))
            .unwrap();
        task.begin_second().unwrap();
        task.feed(&json!({"source":"own","subjects":[trade(1,100.0,2.0)]}))
            .unwrap();
        assert!(task.results().is_err());
    }
    fn trade(id: u64, price: f64, q: f64) -> Value {
        json!({"id":id,"key":format!("T{id}"),"kind":"trade","first_seq":id,"first_time_us":10,"valid":true,"data":{"execution_id":"E","symbol":"S","price":price,"quantity":q}})
    }
    #[test]
    fn distribution_uses_one_global_grid_and_does_not_count_the_second_pass_twice() {
        let mut task = Interval::new(&request()).unwrap();
        let first = (1..=4)
            .zip([100.0, 102.0, 103.0, 355.0])
            .map(|(i, p)| trade(i, p, 1.0))
            .collect::<Vec<_>>();
        let second = vec![trade(5, 356.0, 2.0)];
        for source in [&first, &second] {
            task.feed(&json!({"source":"own","subjects":source}))
                .unwrap();
        }
        task.begin_second().unwrap();
        for source in [&first, &second] {
            task.feed(&json!({"source":"own","subjects":source}))
                .unwrap();
        }
        let result = task.results().unwrap();
        assert_eq!(result["own"]["known_quantity"], 6.0);
        assert_eq!(result["own"]["distribution"]["width_ticks"], 3);
        assert_eq!(result["own"]["stable_count"], 5);
        assert_eq!(result["own"]["known_amount"], 1372.0);
    }
    #[test]
    fn book_clips_expiry_gaps_and_unknown_capture_without_inventing_zero() {
        let mut r = request();
        r["distribution"] = json!(false);
        let mut task = Interval::new(&r).unwrap();
        for mut event in [
            json!({"capture_seq":1,"time_us":1,"topic":"/capture","data":{"symbol":"S","streams":["/quote/S","/market_trade/S"],"boundary":"start"}}),
            json!({"capture_seq":2,"time_us":5,"topic":"/quote/S","data":{"book_valid":true,"bids":[[100,3]],"asks":[[102,1]],"valid_until_us":15}}),
            json!({"capture_seq":3,"time_us":20,"topic":"/connection","data":{"connected":false}}),
        ] {
            event["recording_id"] = json!("R");
            task.observe(&event).unwrap();
        }
        let result = task.results().unwrap();
        assert_eq!(result["book"]["spread_valid_us"], 5);
        assert_eq!(result["book"]["spread_mean"], 2.0);
        assert_eq!(result["book"]["imbalance_mean"], 0.5);
        assert_eq!(result["book"]["quote_capture_us"], 10);
        assert_eq!(result["market"]["complete"], false);
        let mut empty = Interval::new(&r).unwrap();
        let result = empty.results().unwrap();
        assert!(result["book"]["spread_mean"].is_null());
        assert_eq!(result["market"]["complete"], false);
    }
}
