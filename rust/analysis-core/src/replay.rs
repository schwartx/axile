//! Fixed-slot replay folds. Source references survive; histories and proof member sets do not.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
#[path = "replay_derive.rs"]
mod derive;
pub use derive::Derivation;

pub const SLOTS: usize = 8;
pub const DEPENDENCY_SLOTS: usize = 128;
const FIELDS: &[&str] = &[
    "symbol",
    "order_id",
    "command_id",
    "execution_id",
    "origin",
    "operation",
    "phase",
    "success",
    "accepted",
    "status",
    "terminal",
    "price",
    "quantity",
    "filled_quantity",
    "remaining_quantity",
    "average_price",
    "direction",
    "position_side",
    "offset",
    "order_type",
    "confirmed_book",
    "on_book",
    "source",
    "source_seq",
    "identity_quality",
    "trade_key",
    "today_quantity",
    "yesterday_quantity",
    "available_quantity",
    "frozen_quantity",
    "sync_generation",
    "synchronized",
    "connected",
    "ready",
    "available",
    "book_valid",
    "valid_until_us",
    "reason",
];
const PHYSICAL: &[&str] = &[
    "symbol",
    "price",
    "quantity",
    "direction",
    "position_side",
    "offset",
    "order_id",
];
const POSITION_FIELDS: &[&str] = &[
    "quantity",
    "today_quantity",
    "yesterday_quantity",
    "available_quantity",
    "frozen_quantity",
];

fn known(data: &Value, field: &str) -> bool {
    let numeric = matches!(
        field,
        "price"
            | "quantity"
            | "filled_quantity"
            | "remaining_quantity"
            | "average_price"
            | "today_quantity"
            | "yesterday_quantity"
            | "available_quantity"
            | "frozen_quantity"
    );
    !data[field].is_null()
        && (!numeric || data[field].as_f64().is_some_and(f64::is_finite))
        && !matches!(
            data["field_quality"][field].as_str(),
            Some("missing" | "invalid")
        )
}
fn number(data: &Value, field: &str) -> Option<f64> {
    known(data, field)
        .then(|| data[field].as_f64())
        .flatten()
        .filter(|n| n.is_finite())
}
pub(crate) fn connection_unavailable(data: &Value) -> bool {
    ["connected", "ready", "available", "synchronized"]
        .iter()
        .any(|field| {
            data[*field] == false
                || data["field_quality"]
                    .get(*field)
                    .is_some_and(|quality| quality != "known")
        })
}
fn text<'a>(data: &'a Value, field: &str) -> Option<&'a str> {
    known(data, field).then(|| data[field].as_str()).flatten()
}
fn lower(data: &Value, field: &str) -> Option<String> {
    text(data, field).map(str::to_ascii_lowercase)
}
fn equal(field: &str, a: &Value, b: &Value) -> bool {
    if matches!(field, "direction" | "position_side" | "offset") {
        a.as_str()
            .zip(b.as_str())
            .is_some_and(|(a, b)| a.eq_ignore_ascii_case(b))
    } else if a.is_number() && b.is_number() {
        a.as_f64() == b.as_f64()
    } else {
        a == b
    }
}
fn merge(target: &mut Value, incoming: &Value, trade: bool) -> Result<(), String> {
    for field in FIELDS {
        let value = incoming.get(*field);
        let quality = incoming["field_quality"].get(*field);
        if trade && known(target, field) && (value.is_none() || !known(incoming, field)) {
            continue;
        }
        if let Some(value) = value.filter(|v| !v.is_null() || target.get(*field).is_none()) {
            if !value.is_null() && !value.is_boolean() && !value.is_number() && !value.is_string() {
                return Err("REPLAY_FIELD_TYPE".into());
            }
            if value.as_str().is_some_and(|s| s.len() > 4096) {
                return Err("REPLAY_FIELD_BUDGET".into());
            }
            target[*field] = value.clone();
        }
        if let Some(quality) = quality {
            if !matches!(quality.as_str(), Some("known" | "missing" | "invalid")) {
                return Err("REPLAY_FIELD_QUALITY".into());
            }
            target["field_quality"][*field] = quality.clone();
        }
    }
    Ok(())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Subject {
    pub id: u64,
    pub kind: String,
    pub key: String,
    #[serde(default)]
    pub aux: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Fold {
    pub subject: Subject,
    data: Value,
    first_seq: u64,
    #[serde(default)]
    first_time: u64,
    seq: u64,
    time: u64,
    fact_seq: u64,
    conflict: bool,
    owner_conflict: bool,
    generation: Option<u64>,
    global_generation: u64,
    set_revision: u64,
    set_seq: u64,
    account_set_revision: u64,
    account_set_seq: u64,
    #[serde(default)]
    account_set_generation: u64,
    change_seq: u64,
    fill_start: bool,
    proofs: Value,
    owner_seq: u64,
    resolved_seq: u64,
    #[serde(default)]
    history: Vec<Value>,
}

impl Fold {
    pub fn new(subject: Subject) -> Result<Self, String> {
        if subject.key.len() > 4096
            || !matches!(
                subject.kind.as_str(),
                "trade"
                    | "weak_trade"
                    | "market_trade"
                    | "weak_market_trade"
                    | "command"
                    | "order"
                    | "position"
                    | "execution"
            )
        {
            return Err("REPLAY_SUBJECT".into());
        }
        if !subject.aux.is_null() && subject.aux.to_string().len() > 32 * 1024 {
            return Err("REPLAY_SUBJECT_BUDGET".into());
        }
        Ok(Self {
            subject,
            data: json!({"field_quality":{}}),
            first_seq: 0,
            first_time: 0,
            seq: 0,
            time: 0,
            fact_seq: 0,
            conflict: false,
            owner_conflict: false,
            generation: None,
            global_generation: 0,
            set_revision: 0,
            set_seq: 0,
            account_set_revision: 0,
            account_set_seq: 0,
            account_set_generation: 0,
            change_seq: 0,
            fill_start: false,
            proofs: json!({}),
            owner_seq: 0,
            resolved_seq: 0,
            history: Vec::new(),
        })
    }
    pub fn version(&mut self, generation: u64) {
        self.global_generation = self.global_generation.max(generation);
    }
    fn identity(&self) -> Option<(String, String)> {
        let value: Value = serde_json::from_str(&self.subject.key).ok()?;
        Some((value[0].as_str()?.into(), value[1].as_str()?.into()))
    }
    fn matches(&self, data: &Value, seq: u64, row: i64) -> bool {
        match self.subject.kind.as_str() {
            "trade" | "market_trade" => {
                data["identity_quality"] == "stable"
                    && data["trade_key"].as_str() == Some(self.subject.key.as_str())
            }
            "weak_trade" | "weak_market_trade" => {
                self.subject.key.parse::<u64>().ok().is_some_and(|initial| {
                    initial == seq || data["related_capture_seq"].as_u64() == Some(initial)
                })
            }
            "command" => {
                data["command_id"]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("observation:{seq}"))
                    == self.subject.key
            }
            "execution" => data["execution_id"].as_str() == Some(self.subject.key.as_str()),
            "order" => self.identity().is_some_and(|(symbol, id)| {
                data["symbol"] == symbol
                    && data["order_id"]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| format!("observation:{seq}:{row}"))
                        == id
            }),
            "position" => self.identity().is_some_and(|(symbol, side)| {
                data["symbol"] == symbol
                    && data["position_side"]
                        .as_str()
                        .map(str::to_ascii_lowercase)
                        .unwrap_or_else(|| format!("observation:{seq}:{row}"))
                        == side
            }),
            _ => false,
        }
    }
    fn applies_scope(&self, data: &Value) -> bool {
        let Some((symbol, _)) = self.identity() else {
            return false;
        };
        data["scope"]["kind"] == "account"
            || data["scope"]["symbols"]
                .as_array()
                .or_else(|| data["scope"].as_array())
                .is_some_and(|items| items.iter().any(|s| s == &symbol || s == "*"))
            || data["symbol"] == symbol
    }
    fn generation(&mut self, data: &Value) -> bool {
        let generation = data["sync_generation"].as_u64();
        if generation
            .zip(self.generation)
            .is_some_and(|(new, old)| new < old)
        {
            return false;
        }
        if generation.is_some_and(|new| self.generation.is_none_or(|old| new > old)) {
            self.generation = generation;
            self.set_revision = 0;
            self.set_seq = 0;
            self.data["confirmed_book"] = json!(false);
            self.data["confirmation_state"] = json!("awaiting_sync");
        }
        true
    }
    fn query(&self, data: &Value, seq: u64, recording: &str) -> Option<(u64, u64, u64)> {
        let basis = &data["query_basis"];
        let start = basis["start_seq"].as_u64()?;
        let revision = basis["request_revision"].as_u64()?;
        let generation = data["sync_generation"].as_u64()?;
        (basis["recording_id"] == recording && start < seq && revision > 0)
            .then_some((generation, start, revision))
    }
    fn trade_change(&self, seq: u64) -> Option<u64> {
        self.subject.aux["trade_fences"]
            .as_array()?
            .iter()
            .find(|v| v["seq"].as_u64() == Some(seq))?["last_fact_seq"]
            .as_u64()
    }
    fn order_row(
        &mut self,
        data: &Value,
        seq: u64,
        time: u64,
        authority: bool,
    ) -> Result<(), String> {
        let previous = self.data.clone();
        merge(&mut self.data, data, false)?;
        let terminal = |d: &Value| {
            d["terminal"] == true
                || matches!(
                    lower(d, "status").as_deref(),
                    Some("filled" | "canceled" | "cancelled" | "rejected" | "expired")
                )
        };
        if terminal(&previous) && !terminal(data) {
            for name in ["status", "remaining_quantity", "confirmed_book"] {
                if let Some(v) = previous.get(name) {
                    self.data[name] = v.clone();
                }
            }
        }
        if number(&previous, "filled_quantity")
            .zip(number(data, "filled_quantity"))
            .is_some_and(|(a, b)| b < a)
        {
            for name in ["status", "filled_quantity", "remaining_quantity"] {
                if let Some(v) = previous.get(name) {
                    self.data[name] = v.clone();
                }
            }
        }
        if let Some(filled) = number(data, "filled_quantity") {
            if number(&previous, "filled_quantity").is_none_or(|old| filled >= old) {
                self.data["observed_filled_quantity"] = json!(filled);
                self.fill_start |= filled == 0.0;
            }
        }
        if let Some(remaining) = number(data, "remaining_quantity") {
            self.data["observed_remaining_quantity"] = json!(remaining);
        }
        let confirmed = authority
            && !terminal(&self.data)
            && if data.get("confirmed_book").is_some() {
                known(data, "confirmed_book") && data["confirmed_book"] == true
            } else {
                previous["confirmed_book"] == true
            };
        self.data["confirmed_book"] = json!(confirmed);
        self.data["confirmation_state"] = json!(if terminal(&self.data) {
            "terminal"
        } else if confirmed {
            "confirmed"
        } else {
            "unknown"
        });
        self.observed(seq, time);
        Ok(())
    }
    fn observed(&mut self, seq: u64, time: u64) {
        if self.first_seq == 0 {
            self.first_seq = seq;
            self.first_time = time;
        }
        self.seq = seq;
        self.time = time;
        if self.subject.kind == "order" {
            // Display samples are bounded; exact intervening states are rebuilt on navigation.
            if self.history.len() == 16 {
                self.history = self.history.iter().step_by(2).cloned().collect();
            }
            self.history.push(json!({"time_us":time,"capture_seq":seq,"price":number(&self.data,"price"),"quantity":number(&self.data,"quantity"),"filled_quantity":number(&self.data,"observed_filled_quantity")}));
        }
    }
    pub fn apply(&mut self, event: &Value, recording: &str) -> Result<(), String> {
        self.apply_cached(event, recording, &Value::Null)
    }
    fn apply_cached(
        &mut self,
        event: &Value,
        recording: &str,
        proofs: &Value,
    ) -> Result<(), String> {
        self.apply_source(event, recording, proofs, false)
    }
    fn apply_source(
        &mut self,
        event: &Value,
        recording: &str,
        proofs: &Value,
        linked: bool,
    ) -> Result<(), String> {
        let seq = event["capture_seq"]
            .as_u64()
            .ok_or("REPLAY_EVENT_SEQUENCE")?;
        let time = event["time_us"].as_u64().ok_or("REPLAY_EVENT_TIME")?;
        let topic = event["topic"].as_str().ok_or("REPLAY_EVENT_TOPIC")?;
        let data = &event["data"];
        if self.subject.kind == "position"
            && matches!(topic, "/connection" | "/capture")
            && (self.applies_scope(data) || data["scope"].is_null() && data["symbol"].is_null())
        {
            let mut interrupted = false;
            if topic == "/connection" {
                if let Some(generation) = data["sync_generation"].as_u64() {
                    if self.generation.is_some_and(|old| generation < old) {
                        return Ok(());
                    }
                    interrupted = self.first_seq > 0 && self.generation != Some(generation);
                    self.generation = Some(generation);
                }
                interrupted |= connection_unavailable(data);
            } else {
                interrupted = data["boundary"] != "start"
                    && data["streams"]
                        .as_array()
                        .is_some_and(|a| a.iter().any(|s| s == "/trade"));
            }
            // Reconnection cannot prove what happened during the missing interval.
            if interrupted {
                self.proofs = json!({});
            }
            return Ok(());
        }
        if self.subject.kind == "order"
            && topic == "/connection"
            && self.applies_scope(data)
            && self.generation(data)
        {
            if connection_unavailable(data) {
                self.data["confirmed_book"] = json!(false);
                self.data["confirmation_state"] = json!("connection_unavailable");
            }
            return Ok(());
        }
        if self.subject.kind == "order" && topic == "/order_set" {
            return self.order_set(data, seq, time, recording);
        }
        if self.subject.kind == "position" && topic == "/account_observation" {
            return self.position(data, seq, time, proofs);
        }
        let expected = match self.subject.kind.as_str() {
            "trade" | "weak_trade" => "/trade",
            "market_trade" | "weak_market_trade" => "/market_trade/",
            "command" => "/command",
            "order" => "/order",
            "execution" => "/execution",
            _ => "",
        };
        if !(topic == expected || expected == "/market_trade/" && topic.starts_with(expected))
            || !linked && !self.matches(data, seq, -1)
        {
            return Ok(());
        }
        if self.subject.kind == "order" {
            if data.get("query_basis").is_some() {
                let Some((generation, start, _)) = self.query(data, seq, recording) else {
                    return Ok(());
                };
                let Some(trade_change) = self.trade_change(seq) else {
                    self.data["confirmed_book"] = json!(false);
                    self.data["confirmation_state"] = json!("dependencies_pending");
                    return Ok(());
                };
                if self.change_seq.max(trade_change) > start
                    || self.set_seq > start
                    || self.account_set_seq > start
                    || self.generation.is_some_and(|g| g > generation)
                {
                    return Ok(());
                }
            }
            if !self.generation(data) {
                return Ok(());
            }
            if data["sync_generation"].is_null() && self.first_seq > 0 {
                self.data["confirmed_book"] = json!(false);
                return Ok(());
            }
            self.order_row(data, seq, time, data["sync_generation"].is_u64())?;
            self.change_seq = seq;
        } else if self.subject.kind.contains("trade") {
            if self.subject.kind.starts_with("weak_")
                && data["identity_quality"] == "stable"
                && text(data, "trade_key").is_some()
            {
                self.resolved_seq = seq;
                return Ok(());
            }
            let previous = self.data.clone();
            merge(&mut self.data, data, true)?;
            for field in PHYSICAL {
                if known(&previous, field)
                    && known(data, field)
                    && !equal(field, &previous[*field], &data[*field])
                {
                    self.conflict = true;
                    self.data[*field] = previous[*field].clone();
                }
            }
            if known(&previous, "execution_id")
                && known(data, "execution_id")
                && previous["execution_id"] != data["execution_id"]
            {
                self.owner_conflict = true;
            }
            if self.fact_seq == 0
                || self.conflict
                    && PHYSICAL.iter().any(|f| {
                        known(data, f) && known(&previous, f) && !equal(f, &previous[*f], &data[*f])
                    })
                || PHYSICAL.iter().any(|f| {
                    known(&previous, f) != known(&self.data, f)
                        || known(&self.data, f) && !equal(f, &previous[*f], &self.data[*f])
                })
            {
                self.fact_seq = seq;
            }
            self.observed(seq, time);
        } else {
            if self.subject.kind == "command" && self.first_seq > 0 {
                for field in ["operation", "order_id", "symbol", "execution_id"] {
                    if known(&self.data, field)
                        && known(data, field)
                        && !equal(field, &self.data[field], &data[field])
                    {
                        self.owner_conflict = true;
                    }
                }
            }
            merge(&mut self.data, data, false)?;
            if self.subject.kind == "command"
                && self.owner_seq == 0
                && lower(&self.data, "operation").as_deref() == Some("place_order")
                && text(&self.data, "execution_id").is_some()
                && text(&self.data, "order_id").is_some()
                && text(&self.data, "symbol").is_some()
            {
                self.owner_seq = seq;
            }
            if self.subject.kind == "execution"
                && matches!(lower(data, "phase").as_deref(), Some("start" | "started"))
            {
                self.data["start_observed"] = json!(true);
            }
            self.observed(seq, time);
        }
        Ok(())
    }
    fn order_set(
        &mut self,
        data: &Value,
        seq: u64,
        time: u64,
        recording: &str,
    ) -> Result<(), String> {
        let Some((generation, start, revision)) = self.query(data, seq, recording) else {
            return Ok(());
        };
        let Some(rows) = data["orders"].as_array() else {
            return Err("REPLAY_ORDER_SET".into());
        };
        if data["scope_complete"] != true
            || data["synchronized"] != true
            || rows
                .iter()
                .any(|r| r["order_id"].as_str().is_none() || r["symbol"].as_str().is_none())
        {
            return Ok(());
        }
        let account = data["scope"]["kind"] == "account"
            || data["scope"]
                .as_array()
                .or_else(|| data["scope"]["symbols"].as_array())
                .is_some_and(|a| a.iter().any(|s| s == "*"));
        if generation < self.generation.unwrap_or(0) {
            return Ok(());
        }
        if account
            && generation >= self.global_generation
            && (generation > self.account_set_generation || revision > self.account_set_revision)
        {
            self.account_set_generation = generation;
            self.account_set_revision = revision;
            self.account_set_seq = seq;
        }
        if !self.applies_scope(data)
            || self.subject.aux["symbol_first_seq"]
                .as_u64()
                .is_some_and(|first| first > seq)
        {
            return Ok(());
        }
        if self.generation == Some(generation) && revision <= self.set_revision {
            return Ok(());
        }
        self.generation(data);
        let Some(facts) = self.trade_change(seq) else {
            self.data["confirmed_book"] = json!(false);
            self.data["confirmation_state"] = json!("dependencies_pending");
            return Ok(());
        };
        if self.change_seq.max(facts) <= start {
            if let Some(row) = rows.iter().find(|r| self.matches(r, seq, -1)) {
                self.order_row(row, seq, time, true)?;
            } else if self.first_seq > 0 {
                self.data["confirmed_book"] = json!(false);
                self.data["confirmation_state"] = json!("absent");
            }
        }
        self.set_revision = revision;
        self.set_seq = seq;
        Ok(())
    }
    fn position(
        &mut self,
        data: &Value,
        seq: u64,
        time: u64,
        proofs: &Value,
    ) -> Result<(), String> {
        if !self.applies_scope(data) {
            return Ok(());
        }
        let rows = data["positions"].as_array().ok_or("REPLAY_POSITION_ROWS")?;
        if let Some((row, item)) = rows
            .iter()
            .enumerate()
            .find(|(row, item)| self.matches(item, seq, *row as i64))
        {
            merge(&mut self.data, item, false)?;
            let (basis, path) = if let Some(basis) = item.get("position_basis") {
                (basis, row as i64)
            } else {
                (&data["position_basis"], -1)
            };
            let descriptor = proofs
                .get(path.to_string())
                .cloned()
                .unwrap_or_else(|| proof_descriptor(basis, seq, path));
            for field in POSITION_FIELDS {
                if item.get(*field).is_some_and(|value| !value.is_null())
                    || item["field_quality"].get(*field).is_some()
                {
                    self.proofs[*field] = descriptor.clone();
                }
            }
            self.data["observation_kind"] = json!("observed");
            self.observed(seq, time);
        } else if data["scope_complete"] == true && self.first_seq > 0 {
            let descriptor = proofs
                .get("-1")
                .cloned()
                .unwrap_or_else(|| proof_descriptor(&data["position_basis"], seq, -1));
            for field in POSITION_FIELDS {
                if self.data.get(*field).is_some() {
                    self.data[*field] = json!(0);
                    self.data["field_quality"][*field] = json!("known");
                }
            }
            for field in POSITION_FIELDS {
                if self.data.get(*field).is_some() {
                    self.proofs[*field] = descriptor.clone();
                }
            }
            self.data["observation_kind"] = json!("absent_in_complete_scope");
            self.observed(seq, time);
        }
        Ok(())
    }
    pub fn result(&self) -> Value {
        let mut data = self.data.clone();
        for field in FIELDS {
            if data.get(*field).is_some() && !known(&data, field) {
                data[*field] = Value::Null;
            }
        }
        let owner_conflict = self.owner_conflict || self.subject.aux["owner"]["conflict"] == true;
        if owner_conflict {
            data["execution_id"] = Value::Null;
        }
        if !known(&data, "execution_id")
            && !matches!(
                lower(&data, "origin").as_deref(),
                Some("manual" | "external")
            )
            && !owner_conflict
        {
            if let Some(owner) = self.subject.aux["owner"]["execution_id"].as_str() {
                data["execution_id"] = json!(owner);
                data["association_seq"] = self.subject.aux["owner"]["seq"].clone();
            }
        }
        if self.subject.kind == "order" {
            data["history"] = json!(self.history);
            let facts = &self.subject.aux["final_trades"];
            let quantity = facts["quantity"].as_f64();
            let filled = number(&data, "observed_filled_quantity")
                .or_else(|| number(&data, "filled_quantity"))
                .zip(quantity)
                .map(|(a, b)| a.max(b))
                .or(quantity.filter(|n| *n > 0.0));
            data["known_trade_quantity"] = quantity.map_or(Value::Null, |v| json!(v));
            data["filled_quantity"] = filled.map_or(Value::Null, |v| json!(v));
            let remaining = number(&data, "quantity")
                .zip(filled)
                .map(|(a, b)| a - b)
                .filter(|v| v.is_finite() && *v >= 0.0);
            data["remaining_quantity"] = if facts["complete"] == true {
                remaining.map_or_else(|| data["observed_remaining_quantity"].clone(), |v| json!(v))
            } else {
                Value::Null
            };
            data["remaining_quantity_complete"] =
                json!(facts["complete"] == true && self.fill_start && remaining.is_some());
            data["cancel_pending"] = json!(
                self.subject.aux["owner"]["cancel_pending"] == true
                    && data["terminal"] != true
                    && !matches!(
                        lower(&data, "status").as_deref(),
                        Some("filled" | "canceled" | "cancelled" | "rejected" | "expired")
                    )
            );
        }
        let valid = !self.conflict
            && number(&data, "quantity").is_some_and(|v| v > 0.0)
            && number(&data, "price")
                .is_some_and(|p| number(&data, "quantity").is_some_and(|q| (p * q).is_finite()));
        json!({"id":self.subject.id,"kind":self.subject.kind,"key":self.subject.key,"data":data,"first_seq":self.first_seq,"first_time_us":self.first_time,"observed_seq":self.seq,"time_us":self.time,"last_fact_seq":self.fact_seq,"conflict":self.conflict,"owner_conflict":owner_conflict,"valid":valid,"proofs":self.proofs,"owner_seq":self.owner_seq,"resolved_seq":self.resolved_seq})
    }
}

pub fn proof_descriptor(basis: &Value, seq: u64, row: i64) -> Value {
    let proof = &basis["proof"];
    let kind = proof["kind"]
        .as_str()
        .filter(|s| matches!(*s, "included_trade_keys" | "source_watermark"));
    let source = proof["source"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 1024);
    let through = proof["through"]
        .as_u64()
        .filter(|n| *n <= 9_007_199_254_740_991);
    let valid = basis["status"] == "aligned"
        && match kind {
            Some("included_trade_keys") => {
                proof["complete"] == true
                    && proof["included_trade_keys"].as_array().is_some_and(|a| {
                        a.iter()
                            .all(|v| v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 4096))
                    })
            }
            Some("source_watermark") => source.is_some() && through.is_some(),
            _ => false,
        };
    json!({"seq":seq,"row":row,"valid":valid,"kind":kind,"source":source,"through":through,"members":proof["included_trade_keys"].as_array().map(Vec::len)})
}

#[derive(Deserialize)]
pub struct BatchRequest {
    pub recording: String,
    pub through_seq: u64,
    pub cutoff_seq: u64,
    pub subjects: Vec<Subject>,
    #[serde(default)]
    pub after_seq: u64,
    #[serde(default)]
    pub states: Vec<Fold>,
    #[serde(default)]
    pub cutoffs: Vec<u64>,
    #[serde(default)]
    pub orders: Vec<Value>,
}

pub struct Batch {
    request: BatchRequest,
    folds: Vec<Fold>,
    last_seq: u64,
    fences: Vec<Value>,
    next_cutoff: usize,
    state_sizes: Vec<usize>,
    state_bytes: usize,
    links_seq: u64,
    links: Vec<(u64, bool)>,
}

impl Batch {
    pub fn new(mut request: BatchRequest) -> Result<Self, String> {
        if request.recording.is_empty()
            || request.recording.len() > 128
            || request.subjects.len() > DEPENDENCY_SLOTS
            || request.subjects.len() > SLOTS
                && request.subjects.iter().any(|s| {
                    !matches!(
                        s.kind.as_str(),
                        "trade" | "weak_trade" | "command" | "market_trade" | "weak_market_trade"
                    )
                })
            || request.cutoff_seq > request.through_seq
            || request.after_seq > request.cutoff_seq
            || request.cutoffs.len() > 128
            || request.orders.len() > SLOTS
            || request.cutoffs.windows(2).any(|w| w[0] >= w[1])
            || request
                .cutoffs
                .last()
                .is_some_and(|s| *s > request.cutoff_seq)
        {
            return Err("REPLAY_BATCH_BUDGET".into());
        }
        for (i, subject) in request.subjects.iter().enumerate() {
            if subject.id == 0
                || request.subjects[..i]
                    .iter()
                    .any(|previous| previous.id == subject.id)
            {
                return Err("REPLAY_BATCH_IDENTITY".into());
            }
        }
        let mut folds = std::mem::take(&mut request.subjects)
            .into_iter()
            .map(Fold::new)
            .collect::<Result<Vec<_>, _>>()?;
        if !request.states.is_empty() {
            if request.states.len() != folds.len()
                || request.states.iter().zip(&folds).any(|(a, b)| {
                    a.subject.id != b.subject.id
                        || a.subject.key != b.subject.key
                        || a.subject.kind != b.subject.kind
                })
            {
                return Err("REPLAY_BATCH_RESUME".into());
            }
            for (old, new) in request.states.iter_mut().zip(&folds) {
                old.subject.aux = new.subject.aux.clone();
            }
            folds = std::mem::take(&mut request.states);
        }
        let state_sizes = if folds.len() > SLOTS {
            folds
                .iter()
                .map(|fold| {
                    serde_json::to_vec(fold)
                        .map(|v| v.len())
                        .map_err(|_| "REPLAY_BATCH_STATE".to_string())
                })
                .collect::<Result<Vec<_>, _>>()?
        } else {
            vec![0; folds.len()]
        };
        let state_bytes = state_sizes.iter().sum();
        if state_bytes > 256 * 1024 {
            return Err("REPLAY_BATCH_STATE_BUDGET".into());
        }
        Ok(Self {
            request,
            folds,
            last_seq: 0,
            fences: Vec::new(),
            next_cutoff: 0,
            state_sizes,
            state_bytes,
            links_seq: 0,
            links: Vec::new(),
        })
    }
    pub fn apply(&mut self, event: &Value) -> Result<(), String> {
        let seq = event["capture_seq"]
            .as_u64()
            .ok_or("REPLAY_EVENT_SEQUENCE")?;
        if event["schema_version"] != 2
            || event["recording_id"] != self.request.recording
            || seq == 0
            || seq <= self.last_seq
        {
            return Err("REPLAY_BATCH_SOURCE".into());
        }
        self.last_seq = seq;
        self.capture(seq.saturating_sub(1));
        if seq > self.request.cutoff_seq {
            return Ok(());
        }
        if seq <= self.request.after_seq {
            return Ok(());
        }
        if matches!(
            event["topic"].as_str(),
            Some("/connection" | "/order" | "/order_set")
        ) {
            if let Some(generation) = event["data"]["sync_generation"].as_u64() {
                for fold in &mut self.folds {
                    fold.version(generation);
                }
            }
        }
        let mut proofs = json!({});
        if event["topic"] == "/account_observation"
            && self.folds.iter().any(|f| f.subject.kind == "position")
        {
            proofs["-1"] = proof_descriptor(&event["data"]["position_basis"], seq, -1);
            if let Some(rows) = event["data"]["positions"].as_array() {
                for fold in &self.folds {
                    if fold.subject.kind != "position" {
                        continue;
                    }
                    if let Some((row, item)) = rows
                        .iter()
                        .enumerate()
                        .find(|(row, item)| fold.matches(item, seq, *row as i64))
                    {
                        if item.get("position_basis").is_some()
                            && proofs.get(row.to_string()).is_none()
                        {
                            proofs[row.to_string()] =
                                proof_descriptor(&item["position_basis"], seq, row as i64);
                        }
                    }
                }
            }
        }
        let wide = self.folds.len() > SLOTS;
        for (i, fold) in self.folds.iter_mut().enumerate() {
            let link = if self.links_seq == seq {
                self.links.iter().find(|(id, _)| *id == fold.subject.id)
            } else {
                None
            };
            if link.is_some_and(|(_, conflict)| *conflict) {
                fold.conflict = true;
            }
            fold.apply_source(event, &self.request.recording, &proofs, link.is_some())?;
            if wide && fold.seq == seq {
                let size = serde_json::to_vec(fold)
                    .map_err(|_| "REPLAY_BATCH_STATE")?
                    .len();
                self.state_bytes = self.state_bytes - self.state_sizes[i] + size;
                self.state_sizes[i] = size;
                if self.state_bytes > 256 * 1024 {
                    return Err("REPLAY_BATCH_STATE_BUDGET".into());
                }
            }
        }
        Ok(())
    }
    pub fn results(&self) -> Vec<Value> {
        self.folds.iter().map(Fold::result).collect()
    }
    pub fn links(&mut self, seq: u64, links: Vec<(u64, bool)>) -> Result<(), String> {
        if seq <= self.last_seq
            || links.len() > DEPENDENCY_SLOTS
            || links.iter().any(|(id, _)| {
                !self.folds.iter().any(|f| {
                    f.subject.id == *id
                        && matches!(f.subject.kind.as_str(), "trade" | "market_trade")
                })
            })
        {
            return Err("REPLAY_BATCH_LINKS".into());
        }
        self.links_seq = seq;
        self.links = links;
        Ok(())
    }
    fn capture(&mut self, through: u64) {
        while self.next_cutoff < self.request.cutoffs.len()
            && self.request.cutoffs[self.next_cutoff] <= through
        {
            let seq = self.request.cutoffs[self.next_cutoff];
            self.fences.push(json!({"seq":seq,"orders":derive::order_facts(&self.results(),&self.request.orders)}));
            self.next_cutoff += 1;
        }
    }
    pub fn fences(&mut self) -> Vec<Value> {
        self.capture(self.request.cutoff_seq);
        self.fences.clone()
    }
    pub fn states(&self) -> &[Fold] {
        &self.folds
    }
    pub fn version(&mut self, generation: u64) {
        for fold in &mut self.folds {
            fold.version(generation);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_weak_lineage_keeps_original_observation_and_ambiguity_for_interval_inclusion() {
        for kind in ["trade", "market_trade"] {
            let request = json!({"recording":"R","through_seq":20,"cutoff_seq":20,"subjects":[{"id":1,"kind":kind,"key":"T"}]});
            let mut batch = Batch::new(serde_json::from_value(request.clone()).unwrap()).unwrap();
            batch.links(5, vec![(1, false)]).unwrap();
            let topic = if kind == "trade" {
                "/trade"
            } else {
                "/market_trade/S"
            };
            batch
                .apply(&event(
                    5,
                    topic,
                    json!({"symbol":"S","identity_quality":"weak","price":10,"quantity":2}),
                ))
                .unwrap();
            batch.apply(&event(20,topic,json!({"trade_key":"T","symbol":"S","identity_quality":"stable","price":10,"quantity":2,"related_capture_seq":5}))).unwrap();
            assert_eq!(batch.results()[0]["first_time_us"], 5);
            assert_eq!(batch.results()[0]["valid"], true);
            let mut conflict = Batch::new(serde_json::from_value(request).unwrap()).unwrap();
            conflict.links(5, vec![(1, true)]).unwrap();
            conflict
                .apply(&event(
                    5,
                    topic,
                    json!({"symbol":"S","identity_quality":"weak","price":10,"quantity":2}),
                ))
                .unwrap();
            assert_eq!(conflict.results()[0]["conflict"], true);
        }
    }
    #[test]
    fn wide_dependencies_stop_retaining_large_fields_before_the_heap_grows_with_every_slot() {
        let request = json!({"recording":"R","through_seq":128,"cutoff_seq":128,"subjects":(1..=128).map(|i|json!({"id":i,"kind":"trade","key":format!("T{i}")})).collect::<Vec<_>>()});
        let mut batch = Batch::new(serde_json::from_value(request).unwrap()).unwrap();
        let mut stopped = false;
        for seq in 1..=128 {
            let result = batch.apply(&event(seq,"/trade",json!({"trade_key":format!("T{seq}"),"identity_quality":"stable","order_id":"O".repeat(4000),"quantity":1,"price":10})));
            if let Err(error) = result {
                assert_eq!(error, "REPLAY_BATCH_STATE_BUDGET");
                stopped = true;
                break;
            }
        }
        assert!(stopped);
        assert!(batch.state_bytes < 272 * 1024);
    }

    #[test]
    fn dependency_batches_grow_only_to_a_fixed_limit_and_keep_display_slots_small() {
        let subjects = (1..=DEPENDENCY_SLOTS)
            .map(|id| json!({"id":id,"kind":"trade","key":format!("T{id}")}))
            .collect::<Vec<_>>();
        let mut request =
            json!({"recording":"R","through_seq":128,"cutoff_seq":128,"subjects":subjects});
        let mut batch = Batch::new(serde_json::from_value(request.clone()).unwrap()).unwrap();
        for seq in 1..=DEPENDENCY_SLOTS as u64 {
            batch.apply(&event(seq,"/trade",json!({"trade_key":format!("T{seq}"),"identity_quality":"stable","quantity":1,"price":10}))).unwrap();
        }
        assert_eq!(batch.results().len(), DEPENDENCY_SLOTS);
        assert!(batch.results().iter().all(|row| row["valid"] == true));
        request["subjects"]
            .as_array_mut()
            .unwrap()
            .push(json!({"id":129,"kind":"trade","key":"T129"}));
        assert!(Batch::new(serde_json::from_value(request.clone()).unwrap()).is_err());
        request["subjects"] = json!((1..=9)
            .map(|id| json!({"id":id,"kind":"position","key":"[\"S\",\"long\"]"}))
            .collect::<Vec<_>>());
        assert!(Batch::new(serde_json::from_value(request).unwrap()).is_err());
    }

    fn event(seq: u64, topic: &str, data: Value) -> Value {
        json!({"schema_version":2,"recording_id":"R","capture_seq":seq,"time_us":seq,"topic":topic,"data":data})
    }
    fn fold(kind: &str, key: &str) -> Fold {
        Fold::new(Subject {
            id: 1,
            kind: kind.into(),
            key: key.into(),
            aux: Value::Null,
        })
        .unwrap()
    }
    #[test]
    fn trade_merge_conflict_and_physical_cursor_are_separate_from_owner() {
        let mut f = fold("trade", "T");
        f.apply(&event(1,"/trade",json!({"trade_key":"T","identity_quality":"stable","symbol":"S","quantity":2,"price":10,"order_id":"O"})),"R").unwrap();
        f.apply(&event(2,"/trade",json!({"trade_key":"T","identity_quality":"stable","execution_id":"E","quantity":99,"field_quality":{"quantity":"missing"}})),"R").unwrap();
        assert_eq!(f.result()["last_fact_seq"], 1);
        assert_eq!(f.result()["data"]["quantity"], 2);
        assert_eq!(f.result()["data"]["execution_id"], "E");
        f.apply(
            &event(
                3,
                "/trade",
                json!({"trade_key":"T","identity_quality":"stable","quantity":4}),
            ),
            "R",
        )
        .unwrap();
        assert_eq!(f.result()["conflict"], true);
        assert_eq!(f.result()["valid"], false);
    }
    #[test]
    fn position_proof_belongs_to_each_updated_field() {
        let mut f = fold("position", r#"["S","long"]"#);
        for (seq, row) in [
            (
                1,
                json!({"symbol":"S","position_side":"long","quantity":5,"today_quantity":2}),
            ),
            (
                2,
                json!({"symbol":"S","position_side":"long","today_quantity":3}),
            ),
        ] {
            f.apply(&event(seq,"/account_observation",json!({"scope":{"kind":"account"},"positions":[row],"position_basis":{"status":"aligned","proof":{"kind":"source_watermark","source":"broker","through":seq}}})),"R").unwrap();
        }
        let result = f.result();
        assert_eq!(result["proofs"]["quantity"]["seq"], 1);
        assert_eq!(result["proofs"]["today_quantity"]["seq"], 2);
        f.apply(&event(3,"/account_observation",json!({"scope":{"kind":"account"},"positions":[{"symbol":"S","position_side":"long","quantity":null}]})),"R").unwrap();
        assert_eq!(f.result()["proofs"]["quantity"]["seq"], 1);
        assert_eq!(f.result()["data"]["quantity"], 5);
    }
    #[test]
    fn disconnected_position_keeps_its_baseline_but_requires_new_coverage() {
        let mut f = fold("position", r#"["S","long"]"#);
        let baseline = |quantity| json!({"scope":{"kind":"account"},"positions":[{"symbol":"S","position_side":"long","quantity":quantity}],"position_basis":{"status":"aligned","proof":{"kind":"source_watermark","source":"B","through":9}}});
        f.apply(&event(1, "/account_observation", baseline(4)), "R")
            .unwrap();
        f.apply(
            &event(
                2,
                "/connection",
                json!({"scope":{"kind":"account"},"connected":false,"sync_generation":1}),
            ),
            "R",
        )
        .unwrap();
        f.apply(
            &event(
                3,
                "/connection",
                json!({"scope":{"kind":"account"},"connected":true,"sync_generation":1}),
            ),
            "R",
        )
        .unwrap();
        let request = |target| json!({"mode":"position","targets":[target]});
        let invalid = Derivation::new(&request(f.result())).unwrap().results();
        assert_eq!(invalid[0]["baseline"]["quantity"], 4);
        assert!(invalid[0]["data"]["quantity"].is_null());
        f.apply(&event(4, "/account_observation", baseline(5)), "R")
            .unwrap();
        assert_eq!(
            Derivation::new(&request(f.result())).unwrap().results()[0]["data"]["quantity"],
            5.0
        );
        f.apply(
            &event(
                5,
                "/capture",
                json!({"symbol":"S","streams":["/trade"],"boundary":"stop"}),
            ),
            "R",
        )
        .unwrap();
        assert!(
            Derivation::new(&request(f.result())).unwrap().results()[0]["data"]["quantity"]
                .is_null()
        );
    }
    #[test]
    fn order_display_history_is_bounded_and_keeps_first_and_latest_observation() {
        let mut f = fold("order", r#"["S","O"]"#);
        for seq in 1..=40 {
            f.apply(&event(seq,"/order",json!({"symbol":"S","order_id":"O","sync_generation":1,"price":100+seq,"quantity":1})),"R").unwrap();
        }
        let result = f.result();
        let history = result["data"]["history"].as_array().unwrap();
        assert!(history.len() <= 16);
        assert_eq!(history[0]["capture_seq"], 1);
        assert_eq!(history.last().unwrap()["capture_seq"], 40);
        assert_eq!(result["data"]["price"], 140);
    }
    #[test]
    fn malformed_proof_retains_only_bounded_typed_metadata() {
        let descriptor = proof_descriptor(
            &json!({"status":"aligned","proof":{"kind":"source_watermark","source":{"nested":[1,2,3]},"through":9007199254740992u64}}),
            1,
            -1,
        );
        assert_eq!(descriptor["valid"], false);
        assert!(descriptor["source"].is_null());
        assert!(descriptor["through"].is_null());
        assert_eq!(
            proof_descriptor(
                &json!({"status":"aligned","proof":{"kind":"included_trade_keys","complete":true,"included_trade_keys":[""]}}),
                1,
                -1
            )["valid"],
            false
        );
    }
    #[test]
    fn batch_enforces_slots_source_order_and_frozen_cutoff() {
        let request = |count| BatchRequest {
            after_seq: 0,
            states: vec![],
            cutoffs: vec![],
            orders: vec![],
            recording: "R".into(),
            through_seq: 3,
            cutoff_seq: 2,
            subjects: (0..count)
                .map(|i| Subject {
                    id: i + 1,
                    kind: "trade".into(),
                    key: format!("T{i}"),
                    aux: Value::Null,
                })
                .collect(),
        };
        assert!(Batch::new(request(DEPENDENCY_SLOTS as u64 + 1)).is_err());
        let mut batch = Batch::new(request(1)).unwrap();
        let first = event(
            1,
            "/trade",
            json!({"identity_quality":"stable","trade_key":"T0","quantity":2,"price":4}),
        );
        let mut foreign = first.clone();
        foreign["recording_id"] = json!("other");
        assert!(batch.apply(&foreign).is_err());
        batch.apply(&first).unwrap();
        assert!(batch.apply(&first).is_err());
        batch
            .apply(&event(
                3,
                "/trade",
                json!({"identity_quality":"stable","trade_key":"T0","quantity":100}),
            ))
            .unwrap();
        assert_eq!(batch.results()[0]["data"]["quantity"], 2);
        assert_eq!(batch.results()[0]["conflict"], false);
    }
    #[test]
    fn malformed_numbers_do_not_become_known_or_destroy_established_trade_facts() {
        let mut trade = fold("trade", "T");
        trade
            .apply(
                &event(
                    1,
                    "/trade",
                    json!({"identity_quality":"stable","trade_key":"T","quantity":"bad","price":2}),
                ),
                "R",
            )
            .unwrap();
        assert_eq!(trade.result()["data"]["quantity"], Value::Null);
        assert_eq!(trade.result()["valid"], false);
        trade
            .apply(
                &event(
                    2,
                    "/trade",
                    json!({"identity_quality":"stable","trade_key":"T","quantity":3}),
                ),
                "R",
            )
            .unwrap();
        trade
            .apply(
                &event(
                    3,
                    "/trade",
                    json!({"identity_quality":"stable","trade_key":"T","quantity":false}),
                ),
                "R",
            )
            .unwrap();
        assert_eq!(trade.result()["data"]["quantity"], 3);
        assert_eq!(trade.result()["conflict"], false);
        assert_eq!(trade.result()["last_fact_seq"], 2);
    }
    #[test]
    fn weak_identity_requires_an_explicit_resolution_and_later_commands_do_not_own_it() {
        let mut f = fold("weak_trade", "1");
        f.apply(&event(1, "/trade", json!({"quantity":1,"price":3})), "R")
            .unwrap();
        f.apply(
            &event(
                2,
                "/trade",
                json!({"identity_quality":"stable","trade_key":"T","quantity":1,"price":3}),
            ),
            "R",
        )
        .unwrap();
        assert_eq!(f.result()["resolved_seq"], 0);
        f.apply(
            &event(
                3,
                "/trade",
                json!({"identity_quality":"stable","trade_key":"T","related_capture_seq":1}),
            ),
            "R",
        )
        .unwrap();
        assert_eq!(f.result()["resolved_seq"], 3);
        let mut command = fold("command", "C");
        command.apply(&event(4,"/command",json!({"command_id":"C","operation":"place_order","symbol":"S","order_id":"O","execution_id":"E"})),"R").unwrap();
        command
            .apply(
                &event(5, "/command", json!({"command_id":"C","phase":"result"})),
                "R",
            )
            .unwrap();
        assert_eq!(command.result()["owner_seq"], 4);
        command
            .apply(
                &event(
                    6,
                    "/command",
                    json!({"command_id":"C","operation":"cancel_order","execution_id":"X"}),
                ),
                "R",
            )
            .unwrap();
        assert_eq!(command.result()["owner_conflict"], true);
    }
    #[test]
    fn position_partial_quality_and_complete_absence() {
        for case in serde_json::from_str::<Value>(include_str!(
            "../../../tests/fixtures/execution_replay/position-field-merge.json"
        ))
        .unwrap()
        .as_array()
        .unwrap()
        {
            let mut f = fold("position", r#"["S","long"]"#);
            for (seq, row) in [(1, &case["initial"]), (2, &case["update"])] {
                let mut row = row.clone();
                row["symbol"] = json!("S");
                row["position_side"] = json!("LONG");
                f.apply(
                    &event(
                        seq,
                        "/account_observation",
                        json!({"scope":{"kind":"account"},"positions":[row]}),
                    ),
                    "R",
                )
                .unwrap();
            }
            assert_eq!(
                f.result()["data"]["today_quantity"],
                case["visible"],
                "{}",
                case["name"]
            );
            f.apply(
                &event(
                    3,
                    "/account_observation",
                    json!({"scope":{"kind":"account"},"scope_complete":true,"positions":[]}),
                ),
                "R",
            )
            .unwrap();
            assert_eq!(f.result()["data"]["today_quantity"], 0);
            assert!(f.result()["data"].get("quantity").is_none());
        }
    }
    #[test]
    fn query_trade_fences_are_as_of_each_query_and_resume_keeps_push_fences() {
        let mut batch=Batch::new(serde_json::from_value(json!({"recording":"R","through_seq":20,"cutoff_seq":20,"subjects":[{"id":1,"kind":"trade","key":"T"}],"cutoffs":[4,8],"orders":[{"key":"[\"S\",\"O\"]"}]})).unwrap()).unwrap();
        batch.apply(&event(3,"/trade",json!({"trade_key":"T","identity_quality":"stable","symbol":"S","order_id":"O","quantity":2,"price":5}))).unwrap();
        batch
            .apply(&event(
                6,
                "/trade",
                json!({"trade_key":"T","identity_quality":"stable","execution_id":"E"}),
            ))
            .unwrap();
        let fences = batch.fences();
        assert_eq!(fences[0]["orders"][0]["last_fact_seq"], 3);
        assert_eq!(fences[1]["orders"][0]["last_fact_seq"], 3);
        let mut order = fold("order", r#"["S","O"]"#);
        order.subject.aux =
            json!({"trade_fences":[{"seq":4,"last_fact_seq":3},{"seq":8,"last_fact_seq":3}]});
        order.apply(&event(1,"/order",json!({"symbol":"S","order_id":"O","sync_generation":1,"status":"new","filled_quantity":0,"quantity":4})),"R").unwrap();
        order.apply(&event(4,"/order",json!({"symbol":"S","order_id":"O","sync_generation":1,"status":"canceled","query_basis":{"recording_id":"R","start_seq":2,"request_revision":1}})),"R").unwrap();
        assert_eq!(order.result()["data"]["status"], "new");
        let mut resumed: Fold =
            serde_json::from_str(&serde_json::to_string(&order).unwrap()).unwrap();
        resumed.apply(&event(8,"/order",json!({"symbol":"S","order_id":"O","sync_generation":1,"status":"filled","query_basis":{"recording_id":"R","start_seq":5,"request_revision":2}})),"R").unwrap();
        assert_eq!(resumed.result()["data"]["status"], "filled");
    }
    #[test]
    fn new_generation_resets_scope_revision_and_conflicts_advance_fact_fence() {
        let mut order = fold("order", r#"["S","O"]"#);
        order.subject.aux =
            json!({"trade_fences":[{"seq":2,"last_fact_seq":0},{"seq":4,"last_fact_seq":0}]});
        for (seq, generation, revision, status) in [(2, 1, 99, "new"), (4, 2, 1, "filled")] {
            order.version(generation);
            order.apply(&event(seq,"/order_set",json!({"scope":{"kind":"account"},"scope_complete":true,"synchronized":true,"sync_generation":generation,"query_basis":{"recording_id":"R","start_seq":seq-1,"request_revision":revision},"orders":[{"symbol":"S","order_id":"O","status":status}]})),"R").unwrap();
        }
        assert_eq!(order.result()["data"]["status"], "filled");
        let mut trade = fold("trade", "T");
        for (seq, q) in [(1, 1), (3, 2)] {
            trade
                .apply(
                    &event(
                        seq,
                        "/trade",
                        json!({"identity_quality":"stable","trade_key":"T","quantity":q}),
                    ),
                    "R",
                )
                .unwrap();
        }
        assert_eq!(trade.result()["last_fact_seq"], 3);
        assert_eq!(trade.result()["conflict"], true);
    }
    #[test]
    fn cancel_command_does_not_become_an_owner() {
        let mut f = fold("command", "C");
        f.apply(&event(1,"/command",json!({"command_id":"C","operation":"cancel_order","execution_id":"E","symbol":"S","order_id":"O"})),"R").unwrap();
        assert_eq!(f.result()["data"]["operation"], "cancel_order");
    }
}
