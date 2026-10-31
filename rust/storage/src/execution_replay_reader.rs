//! Bounded reads of immutable MCAP parts; a cursor names one fixed recording view.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Cursor;
use std::path::Path;
use std::time::Instant;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use mcap::sans_io::LinearReaderOptions;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::execution_replay::{
    account_directory, checked_us, child_file, collect_observed_symbols, day_directory, digest,
    failure, identifier, io_error, read_bounded, validate_message, validate_symbols,
    CatalogRecording, PartDescriptor, ReplayResult, CHECKPOINT_LIMIT, CONTEXT_LIMIT, JS_SAFE,
    MESSAGE_LIMIT, PAGE_LIMIT, PART_FILE_LIMIT, PART_LIMIT, SCAN_BYTES, SCAN_DAYS, SCAN_EVENTS,
    SCAN_FILES, SCAN_SECONDS, SCHEMA,
};
use crate::execution_replay_projection::Projection;
use crate::execution_replay_recovery::load_read_index;

const PAGE_MESSAGES: usize = 2000;
const CURSOR_LIMIT: usize = 256 * 1024;
const WINDOW_PARTS: usize = 128;

pub(crate) struct PartData {
    pub context: Value,
    pub projection: Value,
    pub messages: Vec<Value>,
}

#[derive(Default)]
pub(crate) struct Budget {
    started: Option<Instant>,
    bytes: u64,
    events: usize,
    entries: usize,
    days: usize,
}

impl Budget {
    pub(crate) fn charge(&mut self, bytes: u64, events: usize) -> ReplayResult<()> {
        let started = self.started.get_or_insert_with(Instant::now);
        self.bytes = self.bytes.saturating_add(bytes);
        self.events = self.events.saturating_add(events);
        if self.bytes > SCAN_BYTES
            || self.events > SCAN_EVENTS
            || started.elapsed().as_secs() >= SCAN_SECONDS
        {
            return Err(failure("BUDGET", "window recovery exceeds the scan budget"));
        }
        Ok(())
    }

    pub(crate) fn directory_entry(&mut self) -> ReplayResult<()> {
        self.entries = self.entries.saturating_add(1);
        if self.entries > SCAN_FILES {
            return Err(failure(
                "BUDGET",
                "replay directory scan exceeds the entry budget",
            ));
        }
        self.charge(0, 0)
    }

    pub(crate) fn day(&mut self) -> ReplayResult<()> {
        self.days = self.days.saturating_add(1);
        if self.days > SCAN_DAYS {
            return Err(failure(
                "BUDGET",
                "replay directory scan exceeds the day budget",
            ));
        }
        self.charge(0, 0)
    }
}

fn integer(value: &Value, name: &str) -> ReplayResult<u64> {
    let number = value[name]
        .as_u64()
        .filter(|number| *number <= JS_SAFE)
        .ok_or_else(|| failure("CORRUPT", format!("invalid {name}")))?;
    Ok(number)
}

pub(crate) fn validate_descriptor(part: &PartDescriptor) -> ReplayResult<()> {
    identifier(&part.recording_id)?;
    validate_symbols(&part.symbols)?;
    if part.part == 0
        || !valid_part_sequences(part)
        || part.start_us >= part.end_us
        || part.size_bytes == 0
        || part.size_bytes > PART_FILE_LIMIT
        || part.sha256.len() != 64
        || !part.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err(failure("CORRUPT", "invalid sealed part descriptor"));
    }
    checked_us(part.start_us)?;
    checked_us(part.end_us)?;
    if let Some(time) = part.checkpoint_time_us {
        checked_us(time)?;
        if time > part.start_us {
            return Err(failure(
                "CORRUPT",
                "checkpoint is after its first observation",
            ));
        }
    } else if part.checkpoint_through_seq != 0 {
        return Err(failure(
            "CORRUPT",
            "nonempty checkpoint has no observation time",
        ));
    }
    Ok(())
}

fn valid_part_sequences(part: &PartDescriptor) -> bool {
    part.first_seq > 0
        && part.first_seq == part.checkpoint_through_seq.saturating_add(1)
        && part.first_seq <= part.last_seq
        && part.last_seq <= JS_SAFE
}

fn read_part_budget(
    account: &Path,
    part: &PartDescriptor,
    budget: &mut Budget,
) -> ReplayResult<PartData> {
    validate_descriptor(part)?;
    budget.charge(part.size_bytes, 0)?;
    let directory = day_directory(account, &part.day, false)?;
    let path = child_file(&directory, &part.file_name())?;
    let bytes = read_bounded(&path, PART_FILE_LIMIT as usize)?;
    if bytes.len() as u64 != part.size_bytes || digest(&bytes) != part.sha256 {
        return Err(failure(
            "VIEW_CHANGED",
            "sealed replay part changed; reload the window",
        ));
    }
    let account_id = account
        .file_name()
        .and_then(|name| name.to_str())
        .and_then(|name| name.strip_prefix("account-"))
        .and_then(|name| name.parse::<u64>().ok())
        .ok_or_else(|| failure("PATH", "invalid account directory"))?;
    validate_part_bytes_budget(&bytes, part, account_id, budget)
}

pub(crate) fn validate_part_bytes(
    bytes: &[u8],
    part: &PartDescriptor,
    account_id: u64,
) -> ReplayResult<PartData> {
    let mut budget = Budget::default();
    budget.charge(bytes.len() as u64, 0)?;
    validate_part_bytes_budget(bytes, part, account_id, &mut budget)
}

pub(crate) fn validate_part_bytes_budget(
    bytes: &[u8],
    part: &PartDescriptor,
    account_id: u64,
    budget: &mut Budget,
) -> ReplayResult<PartData> {
    validate_descriptor(part)?;
    if bytes.len() as u64 != part.size_bytes || digest(bytes) != part.sha256 {
        return Err(failure(
            "CORRUPT",
            "part bytes do not match their descriptor",
        ));
    }
    let options = LinearReaderOptions::default()
        .with_prevalidate_chunk_crcs(true)
        .with_validate_data_section_crc(true)
        .with_validate_summary_section_crc(true)
        .with_check_finishes_after_end_magic(true)
        .with_record_length_limit(8 * 1024 * 1024);
    let mut reader = mcap::io::MessageReader::new_with_options(Cursor::new(bytes), options);
    let mut checkpoint = None;
    let mut messages = Vec::new();
    let mut next_seq = part.first_seq;
    let mut last_time = part.checkpoint_time_us.unwrap_or(0);
    let mut uncompressed = 0_u64;
    for message in &mut reader {
        let message = message.map_err(|error| failure("CORRUPT", error.to_string()))?;
        uncompressed = uncompressed.saturating_add(message.data.len() as u64);
        budget.charge(message.data.len() as u64, 1)?;
        if uncompressed > PART_LIMIT {
            return Err(failure(
                "BUDGET",
                "part exceeds its uncompressed byte limit",
            ));
        }
        let value = decode_message(&message, part, checkpoint.is_none())?;
        if checkpoint.is_none() {
            validate_checkpoint(account_id, part, &value)?;
            checkpoint = Some(value);
            continue;
        }
        validate_message(&value, &checkpoint.as_ref().unwrap()["context"])?;
        let seq = integer(&value, "capture_seq")?;
        let time = integer(&value, "time_us")?;
        if seq != next_seq || time < last_time || message.log_time != checked_us(time)? {
            return Err(failure(
                "CORRUPT",
                "observation order or MCAP time is invalid",
            ));
        }
        next_seq = next_seq
            .checked_add(1)
            .ok_or_else(|| failure("CORRUPT", "sequence overflow"))?;
        last_time = time;
        messages.push(value);
    }
    let checkpoint = checkpoint.ok_or_else(|| failure("CORRUPT", "empty MCAP part"))?;
    if messages
        .first()
        .and_then(|message| message["time_us"].as_u64())
        != Some(part.start_us)
        || next_seq != part.last_seq.saturating_add(1)
        || last_time >= part.end_us
    {
        return Err(failure(
            "CORRUPT",
            "part bounds disagree with its observations",
        ));
    }
    Ok(PartData {
        context: checkpoint["context"].clone(),
        projection: checkpoint["projection"].clone(),
        messages,
    })
}

pub(crate) fn collect_part_symbols(
    data: &PartData,
    symbols: &mut BTreeSet<String>,
) -> ReplayResult<()> {
    for name in [
        "quotes",
        "orders",
        "executions",
        "commands",
        "observed_positions",
        "derived_positions",
    ] {
        if let Some(rows) = data.projection.get(name).and_then(Value::as_object) {
            for row in rows.values() {
                collect_observed_symbols(row, symbols)?;
                if let Some(by_symbol) = row.get("by_symbol").and_then(Value::as_object) {
                    for value in by_symbol.values() {
                        collect_observed_symbols(value, symbols)?;
                    }
                }
            }
        }
    }
    for message in &data.messages {
        collect_observed_symbols(&message["data"], symbols)?;
    }
    Ok(())
}

fn decode_message(
    message: &mcap::Message<'_>,
    part: &PartDescriptor,
    first: bool,
) -> ReplayResult<Value> {
    if message.channel.message_encoding != "json"
        || message.channel.schema.as_ref().is_none_or(|schema| {
            schema.encoding != "jsonschema" || schema.data.as_ref() != SCHEMA.as_bytes()
        })
    {
        return Err(failure("SCHEMA", "unsupported replay message encoding"));
    }
    let limit = if first {
        CHECKPOINT_LIMIT
    } else {
        MESSAGE_LIMIT
    };
    if message.data.len() > limit {
        return Err(failure("BUDGET", "MCAP message exceeds its size limit"));
    }
    let value: Value = serde_json::from_slice(&message.data)
        .map_err(|error| failure("CORRUPT", error.to_string()))?;
    if value["schema_version"] != 1 || value["recording_id"] != part.recording_id {
        return Err(failure(
            "SCHEMA",
            "unknown schema or mixed recording identities",
        ));
    }
    if (message.channel.topic == "/snapshot") != first
        || (!first && value["topic"] != message.channel.topic)
    {
        return Err(failure(
            "CORRUPT",
            "unexpected checkpoint or mismatched topic",
        ));
    }
    Ok(value)
}

fn validate_checkpoint(account_id: u64, part: &PartDescriptor, value: &Value) -> ReplayResult<()> {
    safe_numbers(value, 0)?;
    if value["account_id"].as_u64() != Some(account_id)
        || value["projection"]["recording_id"] != part.recording_id
        || value["projection"]["state_through_seq"].as_u64() != Some(part.checkpoint_through_seq)
        || value["projection"]["time_us"].as_u64() != part.checkpoint_time_us
    {
        return Err(failure(
            "CORRUPT",
            "checkpoint identity or watermark is invalid",
        ));
    }
    let context = &value["context"];
    if !context.is_object()
        || context["schema_version"] != 1
        || serde_json::to_vec(context).map_err(io_error)?.len() > CONTEXT_LIMIT
    {
        return Err(failure("SCHEMA", "invalid frozen replay context"));
    }
    Projection::restore(value["projection"].clone())?;
    Ok(())
}

fn safe_numbers(value: &Value, depth: usize) -> ReplayResult<()> {
    if depth > 32 {
        return Err(failure("BUDGET", "checkpoint nesting exceeds budget"));
    }
    match value {
        Value::Number(number) => {
            let literal = number.to_string();
            let integer = !literal.contains(['.', 'e', 'E']);
            if number.as_u64().is_some_and(|value| value > JS_SAFE)
                || number
                    .as_i64()
                    .is_some_and(|value| value.unsigned_abs() > JS_SAFE)
                || (integer && number.as_i64().is_none() && number.as_u64().is_none())
                || number.as_f64().is_none_or(|value| !value.is_finite())
            {
                return Err(failure(
                    "CORRUPT",
                    "checkpoint contains an unsafe JSON number",
                ));
            }
        }
        Value::Array(values) => {
            for value in values {
                safe_numbers(value, depth + 1)?;
            }
        }
        Value::Object(values) => {
            for value in values.values() {
                safe_numbers(value, depth + 1)?;
            }
        }
        _ => (),
    }
    Ok(())
}

#[derive(Default)]
struct RecordingView {
    metadata: Option<CatalogRecording>,
    parts: Vec<PartDescriptor>,
}

struct RecordingIndex {
    views: BTreeMap<String, RecordingView>,
    partial_recordings: BTreeSet<String>,
}

fn recording_views(account: &Path, budget: &mut Budget) -> ReplayResult<RecordingIndex> {
    let index = load_read_index(account, budget)?;
    let mut recordings: BTreeMap<String, RecordingView> = BTreeMap::new();
    let mut count = 0;
    for catalog in index.catalogs {
        for (identity, mut metadata) in catalog.recordings {
            budget.charge(0, 0)?;
            identifier(&identity)?;
            let view = recordings.entry(identity).or_default();
            if let Some(existing) = &view.metadata {
                if existing.context != metadata.context
                    || existing.previous_recording_id != metadata.previous_recording_id
                {
                    return Err(failure(
                        "CORRUPT",
                        "recording context changes between parts",
                    ));
                }
                metadata
                    .executions
                    .extend(existing.executions.iter().cloned());
                metadata.symbols.extend(existing.symbols.iter().cloned());
            }
            view.metadata = Some(metadata);
        }
        for part in catalog.parts {
            budget.charge(0, 0)?;
            count += 1;
            if count > SCAN_FILES {
                return Err(failure("BUDGET", "too many sealed replay parts"));
            }
            validate_descriptor(&part)?;
            if part.day != catalog.day {
                return Err(failure("CORRUPT", "part is in a different catalog day"));
            }
            recordings
                .entry(part.recording_id.clone())
                .or_default()
                .parts
                .push(part);
        }
    }
    for view in recordings.values_mut() {
        if view.metadata.is_none() {
            return Err(failure("CORRUPT", "part has no frozen context"));
        }
        view.parts.sort_by_key(|part| part.part);
        for pair in view.parts.windows(2) {
            if pair[0].part >= pair[1].part
                || pair[0].last_seq >= pair[1].first_seq
                || pair[0].start_us > pair[1].start_us
            {
                return Err(failure(
                    "CORRUPT",
                    "sealed parts overlap or have invalid order",
                ));
            }
        }
        let metadata = view.metadata.as_mut().unwrap();
        for part in &view.parts {
            metadata.symbols.extend(part.symbols.iter().cloned());
        }
        validate_symbols(&metadata.symbols)?;
    }
    for identity in &index.partial_recordings {
        if let Some(metadata) = recordings
            .get_mut(identity)
            .and_then(|view| view.metadata.as_mut())
        {
            if metadata.unsealed_start_us.is_none() && metadata.error.is_none() {
                metadata.error =
                    Some("Unsealed replay data exists; its coverage is not yet known".into());
            }
        }
    }
    for identity in recordings.keys() {
        let mut seen = BTreeSet::new();
        let mut current = Some(identity.as_str());
        while let Some(identity) = current {
            budget.charge(0, 0)?;
            if !seen.insert(identity) {
                return Err(failure("CORRUPT", "cyclic recording predecessor evidence"));
            }
            current = recordings
                .get(identity)
                .and_then(|view| view.metadata.as_ref())
                .and_then(|metadata| metadata.previous_recording_id.as_deref());
        }
    }
    Ok(RecordingIndex {
        views: recordings,
        partial_recordings: index.partial_recordings,
    })
}

fn execution_id(value: &str) -> ReplayResult<()> {
    if value.is_empty() || value.len() > 128 || value.chars().any(char::is_control) {
        return Err(failure("INVALID_REQUEST", "invalid execution identity"));
    }
    Ok(())
}

fn coverage(parts: &[PartDescriptor], start: Option<u64>, end: Option<u64>) -> Vec<Value> {
    let mut intervals: Vec<(u64, u64)> = Vec::new();
    for part in parts {
        let left = part.start_us.max(start.unwrap_or(0));
        let right = part.end_us.min(end.unwrap_or(JS_SAFE));
        if left >= right {
            continue;
        }
        if let Some(previous) = intervals.last_mut() {
            if left <= previous.1 {
                previous.1 = previous.1.max(right);
                continue;
            }
        }
        intervals.push((left, right));
    }
    intervals
        .into_iter()
        .map(|(start_us, end_us)| json!({"start_us": start_us, "end_us": end_us}))
        .collect()
}

fn recorded_gaps(parts: &[PartDescriptor]) -> Vec<Value> {
    parts
        .windows(2)
        .filter(|pair| pair[1].checkpoint_through_seq != pair[0].last_seq)
        .map(|pair| {
            json!({"reason":"missing_part", "after_seq":pair[0].last_seq,
            "before_seq":pair[1].first_seq})
        })
        .collect()
}

fn metadata_gaps(metadata: &CatalogRecording) -> Vec<Value> {
    let mut result = Vec::new();
    if let Some(start_us) = metadata.unsealed_start_us {
        result.push(json!({"reason":"unsealed","start_us":start_us}));
    }
    if let Some(error) = &metadata.error {
        result.push(json!({"reason":"recording_error","message":error}));
    }
    result
}

fn has_execution(view: &RecordingView, execution: &str) -> bool {
    view.metadata
        .as_ref()
        .is_some_and(|metadata| metadata.executions.contains(execution))
        || view
            .parts
            .iter()
            .any(|part| part.executions.contains(execution))
}

fn manifest_recording(
    account: &Path,
    identity: &str,
    view: &RecordingView,
    views: &BTreeMap<String, RecordingView>,
) -> Value {
    let metadata = view.metadata.as_ref().unwrap();
    let mut gaps = recorded_gaps(&view.parts);
    gaps.extend(metadata_gaps(metadata));
    let missing = view.parts.iter().any(|part| {
        day_directory(account, &part.day, false)
            .and_then(|directory| child_file(&directory, &part.file_name()))
            .and_then(|path| fs::metadata(path).map_err(io_error))
            .map_or(true, |metadata| metadata.len() != part.size_bytes)
    });
    if missing {
        gaps.push(json!({"reason":"file_missing_or_changed"}));
    }
    let previous = metadata.previous_recording_id.as_ref();
    let order_known = previous.is_some_and(|previous| views.contains_key(previous));
    json!({"recording_id":identity,"coverage":coverage(&view.parts,None,None),
        "gaps":gaps,"parts":view.parts,"context":metadata.context,"symbols":metadata.symbols,
        "symbols_complete":!metadata.symbols.is_empty(),
        "instrument":metadata.context.get("instrument").cloned().unwrap_or_else(|| json!({})),
        "streams":metadata.context.get("streams").cloned().unwrap_or_else(|| json!({})),
        "previous_recording_id":previous,"order_known":order_known,
        "state":metadata.state,"unsealed_start_us":metadata.unsealed_start_us,
        "boundary_reason":if previous.is_some() {"new_recording"} else {"unknown"},
        "availability":if missing || !gaps.is_empty() || view.parts.is_empty() {"partial"} else {"available"}})
}

pub(crate) fn manifest(root: &Path, account_id: u64, execution: &str) -> ReplayResult<Value> {
    execution_id(execution)?;
    if account_id == 0 || account_id > JS_SAFE {
        return Err(failure("INVALID_REQUEST", "invalid account identity"));
    }
    if !root.exists() {
        return Ok(
            json!({"schema_version":1,"account_id":account_id,"execution_id":execution,
            "availability":"not_recorded","reason":"not_recorded","recordings":[]}),
        );
    }
    let account = account_directory(root, account_id, false)?;
    let mut budget = Budget::default();
    budget.charge(0, 0)?;
    let index = recording_views(&account, &mut budget)?;
    let views = index.views;
    let selected: BTreeSet<String> = views
        .iter()
        .filter(|(_, view)| has_execution(view, execution))
        .map(|(identity, _)| identity.clone())
        .collect();
    if selected.is_empty() && !index.partial_recordings.is_empty() {
        return Err(failure(
            "INCOMPLETE",
            "unsealed replay data cannot yet be associated with this execution",
        ));
    }
    let ordered = recording_order(&views, &selected);
    let mut result = Vec::new();
    for identity in ordered {
        budget.charge(0, 0)?;
        result.push(manifest_recording(
            &account,
            &identity,
            &views[&identity],
            &views,
        ));
    }
    let legacy_directories: BTreeSet<String> = result
        .iter()
        .filter(|recording| recording["symbols_complete"] != true)
        .map(|recording| recording["recording_id"].as_str().unwrap().to_owned())
        .collect();
    fill_legacy_symbol_directories(&account, &views, &mut result, &mut budget)?;
    let partial = result
        .iter()
        .any(|recording| recording["availability"] == "partial");
    let mut response = json!({"schema_version":1,"account_id":account_id,"execution_id":execution,
        "availability":if result.is_empty() {"not_recorded"} else if partial {"partial"} else {"available"},
        "reason":if result.is_empty() {Some("not_recorded")} else {None},"recordings":result});
    if serde_json::to_vec(&response).map_err(io_error)?.len() > PAGE_LIMIT {
        for recording in response["recordings"].as_array_mut().unwrap() {
            if legacy_directories.contains(recording["recording_id"].as_str().unwrap()) {
                recording["symbols"] = json!([]);
                recording["symbols_complete"] = json!(false);
            }
        }
    }
    if serde_json::to_vec(&response).map_err(io_error)?.len() > PAGE_LIMIT {
        return Err(failure(
            "BUDGET",
            "manifest metadata exceeds the response budget",
        ));
    }
    Ok(response)
}

fn fill_legacy_symbol_directories(
    account: &Path,
    views: &BTreeMap<String, RecordingView>,
    recordings: &mut [Value],
    budget: &mut Budget,
) -> ReplayResult<()> {
    for recording in recordings {
        if recording["symbols_complete"] == true {
            continue;
        }
        let identity = recording["recording_id"].as_str().unwrap();
        let view = &views[identity];
        if view.parts.is_empty() {
            continue;
        }
        let mut symbols = BTreeSet::new();
        let mut complete = true;
        for part in &view.parts {
            let path = child_file(
                &day_directory(account, &part.day, false)?,
                &part.file_name(),
            )?;
            match fs::metadata(path) {
                Ok(metadata) if metadata.len() == part.size_bytes => (),
                Ok(_) => {
                    complete = false;
                    continue;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    complete = false;
                    continue;
                }
                Err(error) => return Err(io_error(error)),
            }
            let scan = read_part_budget(account, part, budget)
                .and_then(|data| collect_part_symbols(&data, &mut symbols));
            match scan {
                Ok(()) => (),
                Err(error) if error.code == "REPLAY_BUDGET" => {
                    recording["symbols"] = json!(symbols);
                    // A best-effort directory must not hide otherwise readable legacy recordings.
                    return Ok(());
                }
                Err(error) => return Err(error),
            }
        }
        recording["symbols"] = json!(symbols);
        recording["symbols_complete"] = json!(complete);
    }
    Ok(())
}

fn recording_order(
    views: &BTreeMap<String, RecordingView>,
    selected: &BTreeSet<String>,
) -> Vec<String> {
    let mut pending: BTreeSet<String> = views.keys().cloned().collect();
    let mut result = Vec::new();
    while !pending.is_empty() {
        let next = pending
            .iter()
            .find(|identity| {
                views[*identity]
                    .metadata
                    .as_ref()
                    .and_then(|metadata| metadata.previous_recording_id.as_ref())
                    .is_none_or(|previous| !pending.contains(previous))
            })
            .cloned();
        let Some(next) = next else {
            result.extend(
                pending
                    .into_iter()
                    .filter(|identity| selected.contains(identity)),
            );
            break;
        };
        pending.remove(&next);
        if selected.contains(&next) {
            result.push(next);
        }
    }
    result
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WindowRequest {
    recording_id: String,
    start_us: u64,
    end_us: u64,
    #[serde(default)]
    symbols: Vec<String>,
    cursor: Option<String>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WindowCursor {
    schema_version: u32,
    account_id: u64,
    execution_id: String,
    recording_id: String,
    start_us: u64,
    end_us: u64,
    symbols: Vec<String>,
    parts: Vec<PartDescriptor>,
    context_hash: String,
    gaps: Vec<Value>,
    window_id: String,
    after_seq: u64,
}

impl WindowCursor {
    fn matches_request(&self, request: &WindowRequest, account_id: u64, execution: &str) -> bool {
        self.schema_version == 1
            && self.account_id == account_id
            && self.execution_id == execution
            && self.recording_id == request.recording_id
            && self.start_us == request.start_us
            && self.end_us == request.end_us
            && self.symbols == request.symbols
    }

    fn view_id(&self) -> ReplayResult<String> {
        let mut view = self.clone();
        view.window_id.clear();
        view.after_seq = 0;
        Ok(digest(&serde_json::to_vec(&view).map_err(io_error)?))
    }

    fn encode(&self) -> ReplayResult<String> {
        let bytes = serde_json::to_vec(self).map_err(io_error)?;
        if bytes.len() > CURSOR_LIMIT {
            return Err(failure("BUDGET", "fixed window cursor exceeds budget"));
        }
        Ok(URL_SAFE_NO_PAD.encode(bytes))
    }
}

fn parse_request(request: &str) -> ReplayResult<WindowRequest> {
    if request.len() > CURSOR_LIMIT * 2 {
        return Err(failure("INVALID_REQUEST", "window request is too large"));
    }
    let mut request: WindowRequest = serde_json::from_str(request)
        .map_err(|error| failure("INVALID_REQUEST", error.to_string()))?;
    identifier(&request.recording_id)?;
    checked_us(request.start_us)?;
    checked_us(request.end_us)?;
    if request.start_us >= request.end_us
        || request.symbols.len() > 128
        || request.symbols.iter().any(|symbol| {
            symbol.is_empty() || symbol.len() > 256 || symbol.chars().any(char::is_control)
        })
    {
        return Err(failure(
            "INVALID_REQUEST",
            "invalid window range or symbols",
        ));
    }
    request.symbols.sort();
    request.symbols.dedup();
    Ok(request)
}

fn select_cursor(
    request: &WindowRequest,
    account_id: u64,
    execution: &str,
    view: &RecordingView,
) -> ReplayResult<WindowCursor> {
    let metadata = view.metadata.as_ref().unwrap();
    let context_hash = digest(&serde_json::to_vec(&metadata.context).map_err(io_error)?);
    if let Some(encoded) = &request.cursor {
        return resume_cursor(encoded, request, account_id, execution, view, &context_hash);
    }
    let checkpoint = view
        .parts
        .iter()
        .rposition(|part| {
            part.checkpoint_through_seq == 0
                || part
                    .checkpoint_time_us
                    .is_some_and(|time| time < request.start_us)
        })
        .ok_or_else(|| {
            failure(
                "INCOMPLETE",
                "no checkpoint strictly before the window; an earlier part is required",
            )
        })?;
    let mut parts = Vec::new();
    for part in &view.parts[checkpoint..] {
        if !parts.is_empty() && part.start_us >= request.end_us {
            break;
        }
        parts.push(part.clone());
        if parts.len() > WINDOW_PARTS {
            return Err(failure("BUDGET", "window covers too many parts"));
        }
    }
    validate_part_chain(&parts)?;
    let mut gaps = recorded_gaps(&parts);
    gaps.extend(metadata_gaps(metadata));
    let mut cursor = WindowCursor {
        schema_version: 1,
        account_id,
        execution_id: execution.to_owned(),
        recording_id: request.recording_id.clone(),
        start_us: request.start_us,
        end_us: request.end_us,
        symbols: request.symbols.clone(),
        parts,
        context_hash,
        gaps,
        window_id: String::new(),
        after_seq: 0,
    };
    cursor.window_id = cursor.view_id()?;
    Ok(cursor)
}

fn resume_cursor(
    encoded: &str,
    request: &WindowRequest,
    account_id: u64,
    execution: &str,
    view: &RecordingView,
    context_hash: &str,
) -> ReplayResult<WindowCursor> {
    if encoded.len() > CURSOR_LIMIT * 2 {
        return Err(failure("INVALID_CURSOR", "cursor is too large"));
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| failure("INVALID_CURSOR", "invalid cursor encoding"))?;
    let cursor: WindowCursor = serde_json::from_slice(&bytes)
        .map_err(|_| failure("INVALID_CURSOR", "invalid cursor payload"))?;
    if !cursor.matches_request(request, account_id, execution)
        || cursor.context_hash != context_hash
        || cursor.parts.is_empty()
        || cursor.parts.len() > WINDOW_PARTS
        || cursor.view_id()? != cursor.window_id
        || cursor.parts.iter().any(|part| !view.parts.contains(part))
    {
        return Err(failure(
            "INVALID_CURSOR",
            "cursor does not match this fixed window",
        ));
    }
    validate_part_chain(&cursor.parts)?;
    if cursor.after_seq < cursor.parts[0].checkpoint_through_seq
        || cursor.after_seq > cursor.parts.last().unwrap().last_seq
    {
        return Err(failure(
            "INVALID_CURSOR",
            "cursor sequence is outside its fixed view",
        ));
    }
    Ok(cursor)
}

fn validate_part_chain(parts: &[PartDescriptor]) -> ReplayResult<()> {
    for pair in parts.windows(2) {
        if pair[1].checkpoint_through_seq != pair[0].last_seq || pair[1].part != pair[0].part + 1 {
            return Err(failure(
                "INCOMPLETE",
                "a sealed part is missing inside the requested window",
            ));
        }
    }
    Ok(())
}

struct WindowPage<'a> {
    request: &'a WindowRequest,
    symbols: &'a BTreeSet<String>,
    first_page: bool,
    after_seq: u64,
    cursor_reserve: usize,
    page_bytes: usize,
    projection: Option<Projection>,
    baseline: Option<Value>,
    messages: Vec<Value>,
    last_returned: u64,
}

impl WindowPage<'_> {
    fn begin_part(&mut self, data: &PartData, part: &PartDescriptor) -> ReplayResult<()> {
        if self.projection.is_none() {
            if part.checkpoint_through_seq != 0
                && part
                    .checkpoint_time_us
                    .is_none_or(|time| time >= self.request.start_us)
            {
                return Err(failure(
                    "INCOMPLETE",
                    "checkpoint includes a window-boundary observation",
                ));
            }
            self.projection = Some(Projection::restore(data.projection.clone())?);
        }
        Ok(())
    }

    fn ensure_baseline(&mut self) -> ReplayResult<()> {
        if self.baseline.is_none() {
            let projection = self
                .projection
                .as_ref()
                .ok_or_else(|| failure("INCOMPLETE", "no checkpoint"))?;
            let state = filtered_projection(projection.snapshot()?, self.symbols);
            if self.first_page {
                self.page_bytes += serde_json::to_vec(&state).map_err(io_error)?.len() + 24;
            }
            self.baseline = Some(state);
        }
        Ok(())
    }

    fn consume(&mut self, message: Value) -> ReplayResult<bool> {
        let seq = integer(&message, "capture_seq")?;
        let time = integer(&message, "time_us")?;
        if time < self.request.start_us {
            self.projection.as_mut().unwrap().apply(&message)?;
            return Ok(false);
        }
        self.ensure_baseline()?;
        if time >= self.request.end_us || seq <= self.after_seq {
            return Ok(false);
        }
        let Some(message) = filtered_message(message, self.symbols) else {
            return Ok(false);
        };
        let bytes = serde_json::to_vec(&message).map_err(io_error)?.len() + 1;
        if self.messages.len() >= PAGE_MESSAGES
            || self.page_bytes + bytes + self.cursor_reserve > PAGE_LIMIT
        {
            if self.messages.is_empty() {
                return Err(failure(
                    "BUDGET",
                    "baseline and one observation exceed the page limit",
                ));
            }
            return Ok(true);
        }
        self.page_bytes += bytes;
        self.last_returned = seq;
        self.messages.push(message);
        Ok(false)
    }
}

pub(crate) fn window(
    root: &Path,
    account_id: u64,
    execution: &str,
    request: &str,
) -> ReplayResult<Value> {
    execution_id(execution)?;
    let request = parse_request(request)?;
    let account = account_directory(root, account_id, false)?;
    let mut budget = Budget::default();
    budget.charge(0, 0)?;
    let index = recording_views(&account, &mut budget)?;
    let views = index.views;
    if index.partial_recordings.contains(&request.recording_id)
        && views
            .get(&request.recording_id)
            .is_none_or(|view| !has_execution(view, execution))
    {
        return Err(failure(
            "INCOMPLETE",
            "unsealed recording has no confirmed execution mapping",
        ));
    }
    let view = views
        .get(&request.recording_id)
        .filter(|view| has_execution(view, execution))
        .ok_or_else(|| {
            failure(
                "NOT_RECORDED",
                "recording does not belong to this account and execution",
            )
        })?;
    let mut cursor = select_cursor(&request, account_id, execution, view)?;
    let first_page = request.cursor.is_none();
    let symbols: BTreeSet<String> = request.symbols.iter().cloned().collect();
    let mut more = false;
    let context = view.metadata.as_ref().unwrap().context.clone();
    let frozen_coverage = coverage(&cursor.parts, Some(request.start_us), Some(request.end_us));
    let mut response = json!({"schema_version":1,"account_id":account_id,"execution_id":execution,
        "recording_id":request.recording_id,"window_id":cursor.window_id,"context":filtered_context(context.clone(),&symbols),
        "coverage":frozen_coverage,"gaps":cursor.gaps,"messages":[],"next_cursor":null,"complete":true});
    let mut page = WindowPage {
        request: &request,
        symbols: &symbols,
        first_page,
        after_seq: cursor.after_seq,
        cursor_reserve: cursor.encode()?.len() + 1024,
        page_bytes: serde_json::to_vec(&response).map_err(io_error)?.len(),
        projection: None,
        baseline: None,
        messages: Vec::new(),
        last_returned: cursor.after_seq,
    };
    for part in &cursor.parts {
        let data = read_part_budget(&account, part, &mut budget)?;
        if data.context != context {
            return Err(failure(
                "CORRUPT",
                "part context differs from the fixed catalog view",
            ));
        }
        page.begin_part(&data, part)?;
        for message in data.messages {
            if page.consume(message)? {
                more = true;
                break;
            }
        }
        if more {
            break;
        }
    }
    page.ensure_baseline()?;
    if first_page {
        response["state_before_start"] = page.baseline.unwrap();
    }
    response["messages"] = Value::Array(page.messages);
    response["complete"] = Value::Bool(!more);
    if more {
        cursor.after_seq = page.last_returned;
        response["next_cursor"] = Value::String(cursor.encode()?);
    }
    if serde_json::to_vec(&response).map_err(io_error)?.len() > PAGE_LIMIT {
        return Err(failure("BUDGET", "window response exceeds the page limit"));
    }
    Ok(response)
}

fn scope_symbols(value: &Value) -> Option<Vec<String>> {
    let scope = value.get("scope")?;
    let scope = scope
        .as_array()
        .or_else(|| scope.get("symbols").and_then(Value::as_array))?;
    Some(
        scope
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect(),
    )
}

fn filtered_message(mut message: Value, symbols: &BTreeSet<String>) -> Option<Value> {
    if symbols.is_empty() {
        return Some(message);
    }
    let topic = message["topic"].as_str().unwrap_or("");
    let data = &message["data"];
    let symbol = data["symbol"]
        .as_str()
        .or_else(|| topic.strip_prefix("/quote/"))
        .or_else(|| topic.strip_prefix("/market_trade/"));
    if symbol.is_some_and(|symbol| !symbols.contains(symbol)) {
        return None;
    }
    if matches!(topic, "/account_observation" | "/order_set" | "/connection") {
        clip_scoped_data(&mut message["data"], symbols);
    }
    Some(message)
}

fn clip_scoped_data(data: &mut Value, symbols: &BTreeSet<String>) {
    let original = if data["scope"]["kind"] == "account" {
        Some(symbols.iter().cloned().collect())
    } else {
        scope_symbols(data)
    };
    if let Some(original) = original {
        data["scope"] = json!({"kind":"symbols","symbols":original
            .into_iter()
            .filter(|symbol| symbols.contains(symbol))
            .collect::<Vec<_>>()});
    } else {
        data["scope"] = json!({"kind":"symbols","symbols":symbols});
        data["scope_complete"] = Value::Bool(false);
    }
    for field in ["positions", "orders"] {
        if let Some(rows) = data.get_mut(field).and_then(Value::as_array_mut) {
            rows.retain(|row| {
                row["symbol"]
                    .as_str()
                    .is_some_and(|symbol| symbols.contains(symbol))
            });
        }
    }
}

fn filtered_context(mut context: Value, symbols: &BTreeSet<String>) -> Value {
    if symbols.is_empty() {
        return context;
    }
    for field in ["instrument", "streams"] {
        if let Some(map) = context.get_mut(field).and_then(Value::as_object_mut) {
            map.retain(|symbol, _| symbols.contains(symbol));
        }
    }
    context
}

fn row_symbol(key: &str, row: &Value) -> Option<String> {
    row["symbol"]
        .as_str()
        .or_else(|| row["data"]["symbol"].as_str())
        .or_else(|| row["observation"]["data"]["symbol"].as_str())
        .or_else(|| row["observation"]["symbol"].as_str())
        .map(str::to_owned)
        .or_else(|| {
            serde_json::from_str::<Vec<Value>>(key)
                .ok()?
                .first()?
                .as_str()
                .map(str::to_owned)
        })
}

fn filtered_projection(mut state: Value, symbols: &BTreeSet<String>) -> Value {
    if symbols.is_empty() {
        return state;
    }
    for field in [
        "quotes",
        "orders",
        "observed_positions",
        "derived_positions",
        "position_basis",
        "position_scopes",
        "order_change_seq",
        "last_set_revision",
        "sync_generation",
        "synchronized",
    ] {
        if let Some(rows) = state.get_mut(field).and_then(Value::as_object_mut) {
            rows.retain(|key, row| {
                symbols.contains(key)
                    || row_symbol(key, row).is_some_and(|symbol| symbols.contains(&symbol))
            });
            for row in rows.values_mut() {
                if row.get("scope").is_some() {
                    clip_scoped_data(row, symbols);
                }
            }
        }
    }
    for field in ["connections", "commands"] {
        if let Some(rows) = state.get_mut(field).and_then(Value::as_object_mut) {
            rows.retain(|_, row| {
                row["symbol"]
                    .as_str()
                    .is_none_or(|symbol| symbols.contains(symbol))
            });
            for row in rows.values_mut() {
                if row.get("scope").is_some() {
                    clip_scoped_data(row, symbols);
                }
            }
        }
    }
    if let Some(accounting) = state.get_mut("trade_accounting") {
        for field in ["own_records", "market_records", "pending"] {
            if let Some(records) = accounting.get_mut(field).and_then(Value::as_object_mut) {
                records.retain(|key, row| {
                    row_symbol(key, row).is_some_and(|symbol| symbols.contains(&symbol))
                });
            }
        }
        for (keys, records) in [
            ("own_seen_keys", "own_records"),
            ("market_seen_keys", "market_records"),
        ] {
            let retained: BTreeSet<String> = accounting[records]
                .as_object()
                .map(|rows| rows.keys().cloned().collect())
                .unwrap_or_default();
            if let Some(keys) = accounting.get_mut(keys).and_then(Value::as_array_mut) {
                keys.retain(|key| key.as_str().is_some_and(|key| retained.contains(key)));
            }
        }
        if let Some(totals) = accounting
            .get_mut("execution_totals")
            .and_then(Value::as_object_mut)
        {
            for total in totals.values_mut() {
                if let Some(by_symbol) = total.get_mut("by_symbol").and_then(Value::as_object_mut) {
                    by_symbol.retain(|symbol, _| symbols.contains(symbol));
                }
                total["scope"] = json!({"kind":"symbols","symbols":symbols});
            }
        }
        accounting["scope"] = json!({"kind":"symbols","symbols":symbols});
        accounting["scope_filtered"] = Value::Bool(true);
    }
    state["scope"] = json!({"kind":"symbols","symbols":symbols});
    state["scope_filtered"] = Value::Bool(true);
    state
}

#[cfg(test)]
mod tests {
    use super::*;

    fn part(
        number: u32,
        through: u64,
        first: u64,
        last: u64,
        start: u64,
        checkpoint: Option<u64>,
    ) -> PartDescriptor {
        PartDescriptor {
            recording_id: "A".into(),
            part: number,
            day: "2026-10-31".into(),
            sha256: "0".repeat(64),
            size_bytes: 100,
            first_seq: first,
            last_seq: last,
            start_us: start,
            end_us: start + 10,
            checkpoint_through_seq: through,
            checkpoint_time_us: checkpoint,
            executions: BTreeSet::from(["7".into()]),
            symbols: BTreeSet::new(),
        }
    }

    fn metadata() -> CatalogRecording {
        CatalogRecording {
            context: json!({"schema_version":1}),
            previous_recording_id: None,
            executions: BTreeSet::from(["7".into()]),
            symbols: BTreeSet::new(),
            state: "closed".into(),
            unsealed_start_us: None,
            error: None,
        }
    }

    #[test]
    fn same_microsecond_part_checkpoint_does_not_absorb_start_events() {
        let view = RecordingView {
            metadata: Some(metadata()),
            parts: vec![
                part(1, 0, 1, 2, 100, None),
                part(2, 2, 3, 4, 100, Some(100)),
            ],
        };
        let request = WindowRequest {
            recording_id: "A".into(),
            start_us: 100,
            end_us: 110,
            symbols: vec![],
            cursor: None,
        };
        let cursor = select_cursor(&request, 1, "7", &view).unwrap();
        assert_eq!(cursor.parts.len(), 2);
        assert_eq!(cursor.parts[0].checkpoint_through_seq, 0);
    }

    #[test]
    fn cursor_binds_recording_and_freezes_part_set() {
        let mut view = RecordingView {
            metadata: Some(metadata()),
            parts: vec![part(1, 0, 1, 2, 100, None)],
        };
        let mut request = WindowRequest {
            recording_id: "A".into(),
            start_us: 100,
            end_us: 200,
            symbols: vec!["X".into()],
            cursor: None,
        };
        let mut cursor = select_cursor(&request, 1, "7", &view).unwrap();
        cursor.after_seq = 1;
        request.cursor = Some(cursor.encode().unwrap());
        view.parts.push(part(2, 2, 3, 4, 150, Some(109)));
        assert_eq!(
            select_cursor(&request, 1, "7", &view).unwrap().parts.len(),
            1
        );
        request.recording_id = "B".into();
        assert!(select_cursor(&request, 1, "7", &view).is_err());
    }

    #[test]
    fn recording_order_keeps_evidence_through_an_unselected_recording() {
        let first = metadata();
        let mut middle = metadata();
        middle.previous_recording_id = Some("z".into());
        let mut last = metadata();
        last.previous_recording_id = Some("m".into());
        let views = BTreeMap::from([
            (
                "z".into(),
                RecordingView {
                    metadata: Some(first),
                    parts: Vec::new(),
                },
            ),
            (
                "m".into(),
                RecordingView {
                    metadata: Some(middle),
                    parts: Vec::new(),
                },
            ),
            (
                "a".into(),
                RecordingView {
                    metadata: Some(last),
                    parts: Vec::new(),
                },
            ),
        ]);
        assert_eq!(
            recording_order(&views, &BTreeSet::from(["z".into(), "a".into()])),
            vec!["z", "a"]
        );
    }

    #[test]
    fn symbol_crop_preserves_narrow_scope_and_independent_totals() {
        let symbols = BTreeSet::from(["A".into()]);
        let message = json!({"topic":"/account_observation","data":{"scope":["A","B"],"scope_complete":true,
            "positions":[{"symbol":"A","quantity":4},{"symbol":"B","quantity":9}]}});
        let message = filtered_message(message, &symbols).unwrap();
        assert_eq!(
            message["data"]["scope"],
            json!({"kind":"symbols","symbols":["A"]})
        );
        assert_eq!(message["data"]["scope_complete"], true);
        assert_eq!(
            message["data"]["positions"],
            json!([{"symbol":"A","quantity":4}])
        );
        let state = json!({"trade_accounting":{"own_seen_keys":["a","b"],"market_seen_keys":[],
            "own_records":{"a":{"symbol":"A","quantity":4},"b":{"symbol":"B","quantity":9}},
            "market_records":{},"pending":{},"execution_totals":{"7":{"complete":true,"by_symbol":{"A":{"quantity":4},"B":{"quantity":9}}}}}});
        let state = filtered_projection(state, &symbols);
        assert_eq!(state["trade_accounting"]["own_seen_keys"], json!(["a"]));
        assert_eq!(
            state["trade_accounting"]["execution_totals"]["7"]["by_symbol"],
            json!({"A":{"quantity":4}})
        );
    }

    #[test]
    fn account_scope_is_narrowed_to_the_selected_symbol_without_losing_completeness() {
        let symbols = BTreeSet::from(["A".into()]);
        for topic in ["/connection", "/order_set", "/account_observation"] {
            let message = json!({"topic":topic,"data":{"scope":{"kind":"account","symbols":[]},
                "scope_complete":true,"connected":false,"orders":[{"symbol":"A"},{"symbol":"B"}],
                "positions":[{"symbol":"A"},{"symbol":"B"}]}});
            let message = filtered_message(message, &symbols).unwrap();
            assert_eq!(
                message["data"]["scope"],
                json!({"kind":"symbols","symbols":["A"]})
            );
            assert_eq!(message["data"]["scope_complete"], true);
            assert_eq!(message["data"]["orders"], json!([{"symbol":"A"}]));
            assert_eq!(message["data"]["positions"], json!([{"symbol":"A"}]));
        }
    }

    #[test]
    fn real_zstd_mcap_requires_checkpoint_hash_and_end_magic() {
        let mut writer = mcap::WriteOptions::new()
            .compression(Some(mcap::Compression::Zstd))
            .chunk_size(Some(1024))
            .create(Cursor::new(Vec::new()))
            .unwrap();
        let schema = writer
            .add_schema("axile.execution_replay.v1", "jsonschema", SCHEMA.as_bytes())
            .unwrap();
        let snapshot_channel = writer
            .add_channel(schema, "/snapshot", "json", &BTreeMap::new())
            .unwrap();
        let event_channel = writer
            .add_channel(schema, "/execution", "json", &BTreeMap::new())
            .unwrap();
        let checkpoint = json!({"schema_version":1,"account_id":1,"recording_id":"A",
            "context":{"schema_version":1},"projection":Projection::new("A").snapshot().unwrap()});
        let message = json!({"schema_version":1,"recording_id":"A","capture_seq":1,"time_us":100,
            "topic":"/execution","data":{"execution_id":"7","phase":"started","target_quantity":10}});
        for (channel_id, sequence, payload) in [
            (snapshot_channel, 0, &checkpoint),
            (event_channel, 1, &message),
        ] {
            writer
                .write_to_known_channel(
                    &mcap::records::MessageHeader {
                        channel_id,
                        sequence,
                        log_time: 100_000,
                        publish_time: 100_000,
                    },
                    &serde_json::to_vec(payload).unwrap(),
                )
                .unwrap();
        }
        writer.finish().unwrap();
        let mut bytes = writer.into_inner().into_inner();
        let mut descriptor = part(1, 0, 1, 1, 100, None);
        descriptor.size_bytes = bytes.len() as u64;
        descriptor.sha256 = digest(&bytes);
        let data = validate_part_bytes(&bytes, &descriptor, 1).unwrap();
        assert_eq!(data.messages, vec![message]);
        assert_eq!(data.projection["state_through_seq"], 0);
        assert!(validate_part_bytes(&bytes, &descriptor, 2).is_err());
        *bytes.last_mut().unwrap() = 0;
        assert!(validate_part_bytes(&bytes, &descriptor, 1).is_err());
        descriptor.sha256 = digest(&bytes);
        assert!(validate_part_bytes(&bytes, &descriptor, 1).is_err());
    }

    #[test]
    fn optional_legacy_symbol_scan_budget_does_not_hide_manifest_metadata() {
        let account =
            std::env::temp_dir().join(format!("axile-legacy-symbol-budget-{}", std::process::id()));
        let directory = account.join("2026-10-31");
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("recording-A-000001.mcap"), [0_u8; 100]).unwrap();
        let views = BTreeMap::from([(
            "A".into(),
            RecordingView {
                metadata: Some(metadata()),
                parts: vec![part(1, 0, 1, 1, 100, None)],
            },
        )]);
        let mut recordings = vec![json!({"recording_id":"A","symbols":[],
            "symbols_complete":false,"availability":"available"})];
        let mut budget = Budget {
            bytes: SCAN_BYTES,
            ..Budget::default()
        };
        fill_legacy_symbol_directories(&account, &views, &mut recordings, &mut budget).unwrap();
        assert_eq!(recordings[0]["availability"], "available");
        assert_eq!(recordings[0]["symbols_complete"], false);
        assert_eq!(recordings[0]["symbols"], json!([]));
        fs::remove_dir_all(account).unwrap();
    }

    #[test]
    fn legacy_symbol_directory_keeps_missing_prefix_and_later_window_readable() {
        use crate::execution_replay::ExecutionReplayWriter;
        let root = std::env::temp_dir().join(format!(
            "axile-legacy-symbol-missing-prefix-{}",
            std::process::id()
        ));
        fs::create_dir(&root).unwrap();
        let mut writer =
            ExecutionReplayWriter::new(&root, 1, "legacy", r#"{"schema_version":1}"#).unwrap();
        for (seq, time, topic, data) in [
            (
                1,
                100,
                "/execution",
                json!({"execution_id":"E","phase":"start","symbols":["A"]}),
            ),
            (2, 101, "/quote/A", json!({"symbol":"A","book_valid":false})),
            (3, 200, "/quote/A", json!({"symbol":"A","book_valid":false})),
        ] {
            if seq == 3 {
                writer.seal().unwrap();
            }
            writer
                .append(
                    &json!({"schema_version":1,"recording_id":"legacy",
                "capture_seq":seq,"time_us":time,"topic":topic,"data":data})
                    .to_string(),
                )
                .unwrap();
        }
        writer.finish().unwrap();
        let catalog_path = root.join("account-1/1970-01-01/catalog.json");
        let mut catalog: Value = serde_json::from_slice(&fs::read(&catalog_path).unwrap()).unwrap();
        catalog["recordings"]["legacy"]
            .as_object_mut()
            .unwrap()
            .remove("symbols");
        for part in catalog["parts"].as_array_mut().unwrap() {
            part.as_object_mut().unwrap().remove("symbols");
        }
        let bytes = serde_json::to_vec(&catalog).unwrap();
        fs::write(&catalog_path, &bytes).unwrap();
        fs::remove_file(root.join("account-1/1970-01-01/recording-legacy-000001.mcap")).unwrap();
        let result = manifest(&root, 1, "E").unwrap();
        assert_eq!(result["availability"], "partial");
        assert_eq!(result["recordings"][0]["symbols"], json!(["A"]));
        assert_eq!(result["recordings"][0]["symbols_complete"], false);
        let later = window(
            &root,
            1,
            "E",
            &json!({"recording_id":"legacy","start_us":200,"end_us":201}).to_string(),
        )
        .unwrap();
        assert_eq!(later["state_before_start"]["state_through_seq"], 2);
        assert_eq!(later["messages"][0]["capture_seq"], 3);
        assert_eq!(fs::read(catalog_path).unwrap(), bytes);
        fs::remove_dir_all(root).unwrap();
    }
}
