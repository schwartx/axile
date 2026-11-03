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
    failure, identifier, io_error, read_bounded, stable_market_key, validate_committed_records,
    validate_message, validate_symbols, CatalogRecording, PartDescriptor, ReplayResult,
    CHECKPOINT_LIMIT, CONTEXT_LIMIT, JS_SAFE, PAGE_LIMIT, PART_FILE_LIMIT, PART_LIMIT, SCAN_BYTES,
    SCAN_DAYS, SCAN_EVENTS, SCAN_FILES, SCAN_SECONDS, SCHEMA,
};
use crate::execution_replay_block::{read_block, BlockRecord};
use crate::execution_replay_index::{BlockIndex, IndexedBlock, IndexedCheckpoint};
use crate::execution_replay_projection::Projection;
use crate::execution_replay_recovery::load_read_index;

const PAGE_MESSAGES: usize = 2000;
const STATE_FRAGMENT_BYTES: usize = 1024 * 1024;
const CURSOR_LIMIT: usize = 256 * 1024;

pub(crate) struct PartData {
    pub context: Value,
    pub projection: Value,
    pub messages: Vec<Value>,
    limited: bool,
}

struct PageRead {
    after_seq: u64,
    start_us: u64,
    end_us: u64,
    bytes: usize,
}

#[derive(Default)]
pub(crate) struct Budget {
    pub(crate) indexed: bool,
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
        if !self.indexed
            && (self.bytes > SCAN_BYTES
                || self.events > SCAN_EVENTS
                || started.elapsed().as_secs() >= SCAN_SECONDS)
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
    checkpoint: Option<(&IndexedCheckpoint, &Value)>,
    until_us: Option<u64>,
    page: Option<&mut PageRead>,
) -> ReplayResult<PartData> {
    validate_descriptor(part)?;
    if part.committed_prefix {
        return read_committed_part(account, part, budget, checkpoint, until_us, page);
    }
    let index_path = child_file(account, ".replay-index.sqlite")?;
    if index_path.exists() {
        let index = BlockIndex::open(&index_path)?;
        if let Some((first, last)) =
            index.file_bounds(&part.recording_id, part.part, part.last_seq)?
        {
            let Some((indexed, indexed_context, _)) =
                index.sealed_file(&part.recording_id, part.part)?
            else {
                return Err(failure(
                    "INCOMPLETE",
                    "sealed replay metadata is missing from its index",
                ));
            };
            if indexed != *part {
                return Err(failure(
                    "VIEW_CHANGED",
                    "sealed replay catalog does not match its index",
                ));
            }
            let directory = day_directory(account, &part.day, false)?;
            let path = child_file(&directory, &part.file_name())?;
            if fs::metadata(&path).map_err(io_error)?.len() != part.size_bytes {
                return Err(failure("VIEW_CHANGED", "sealed replay file changed"));
            }
            if first.first_seq != part.first_seq
                || last.last_seq != part.last_seq
                || first.first_us != part.start_us
            {
                return Err(failure(
                    "CORRUPT",
                    "sealed replay index does not match its descriptor",
                ));
            }
            let mut prefix = part.clone();
            prefix.committed_prefix = true;
            prefix.size_bytes = last
                .range
                .offset
                .checked_add(last.range.length)
                .ok_or_else(|| failure("CORRUPT", "block end overflow"))?;
            prefix.sha256 = digest(&serde_json::to_vec(&(&first, &last)).map_err(io_error)?);
            let data = read_committed_part(account, &prefix, budget, checkpoint, until_us, page)?;
            if data.context != indexed_context {
                return Err(failure(
                    "CORRUPT",
                    "sealed replay context does not match its index",
                ));
            }
            return Ok(data);
        }
    }
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

fn read_committed_part(
    account: &Path,
    part: &PartDescriptor,
    budget: &mut Budget,
    checkpoint: Option<(&IndexedCheckpoint, &Value)>,
    until_us: Option<u64>,
    mut page: Option<&mut PageRead>,
) -> ReplayResult<PartData> {
    let index = BlockIndex::open(&child_file(account, ".replay-index.sqlite")?)?;
    let (first, last) = index
        .file_bounds(&part.recording_id, part.part, part.last_seq)?
        .ok_or_else(|| failure("VIEW_CHANGED", "committed replay file disappeared"))?;
    if digest(&serde_json::to_vec(&(&first, &last)).map_err(io_error)?) != part.sha256
        || first.first_seq != part.first_seq
        || last.last_seq != part.last_seq
        || last.range.offset.checked_add(last.range.length) != Some(part.size_bytes)
    {
        return Err(failure("VIEW_CHANGED", "committed replay view changed"));
    }
    let directory = day_directory(account, &part.day, false)?;
    let partial = child_file(&directory, &format!("{}.partial", part.file_name()))?;
    let sealed = child_file(&directory, &part.file_name())?;
    let account_id = account
        .file_name()
        .and_then(|name| name.to_str())
        .and_then(|name| name.strip_prefix("account-"))
        .and_then(|name| name.parse::<u64>().ok())
        .ok_or_else(|| failure("PATH", "invalid account directory"))?;
    let mut schema = checkpoint.and_then(|(checkpoint, _)| checkpoint.schema);
    let mut channels =
        checkpoint.map_or_else(BTreeMap::new, |(checkpoint, _)| checkpoint.channels.clone());
    let mut context: Option<Value> = checkpoint.map(|(_, context)| context.clone());
    let mut projection = checkpoint.map(|(checkpoint, _)| checkpoint.state.clone());
    let mut messages = Vec::new();
    let mut previous: Option<IndexedBlock> = if let Some((checkpoint, _)) = checkpoint {
        if checkpoint.part != part.part
            || checkpoint.seq < part.checkpoint_through_seq
            || checkpoint.seq > part.last_seq
        {
            return Err(failure(
                "CORRUPT",
                "checkpoint does not belong to this replay file",
            ));
        }
        if checkpoint.seq == part.checkpoint_through_seq {
            None
        } else {
            Some(
                index
                    .block_at_end(&part.recording_id, part.part, checkpoint.seq)?
                    .ok_or_else(|| failure("INCOMPLETE", "checkpoint block is missing"))?,
            )
        }
    } else {
        None
    };
    let mut stopped_at_end = false;
    let mut limited = false;
    loop {
        let blocks = index.file_blocks(
            &part.recording_id,
            part.part,
            part.last_seq,
            previous.as_ref().map(|block| block.range.offset),
            256,
        )?;
        if blocks.is_empty() {
            break;
        }
        for block in blocks {
            if until_us.is_some_and(|end| block.first_us >= end)
                && context.is_some()
                && projection.is_some()
            {
                stopped_at_end = true;
                break;
            }
            if let Some(previous) = &previous {
                if previous.last_seq.checked_add(1) != Some(block.first_seq)
                    || previous.range.offset.checked_add(previous.range.length)
                        != Some(block.range.offset)
                    || previous.last_us > block.first_us
                {
                    return Err(failure(
                        "INCOMPLETE",
                        "committed replay blocks are not contiguous",
                    ));
                }
            } else if block != first {
                return Err(failure("VIEW_CHANGED", "first committed block changed"));
            }
            budget.charge(block.range.length, 0)?;
            let path = if partial.exists() { &partial } else { &sealed };
            let records = match read_block(path, &block.range) {
                Err(_) if path == &partial && !partial.exists() && sealed.exists() => {
                    read_block(&sealed, &block.range)?
                }
                result => result?,
            };
            if context.is_none() {
                let snapshot = records
                    .iter()
                    .find_map(|record| match record {
                        BlockRecord::Message(message) => Some(message),
                        _ => None,
                    })
                    .ok_or_else(|| failure("CORRUPT", "first committed block has no checkpoint"))?;
                let value: Value = serde_json::from_slice(&snapshot.data)
                    .map_err(|error| failure("CORRUPT", error.to_string()))?;
                context = Some(value["context"].clone());
            }
            let (snapshot, mut observations) = validate_committed_records(
                &records,
                part,
                account_id,
                context.as_ref().unwrap(),
                block.first_seq,
                block.last_seq,
                block.first_us,
                block.last_us,
                &mut schema,
                &mut channels,
                None,
            )?;
            if let Some(snapshot) = snapshot {
                let state = match projection.take() {
                    Some(state) => state,
                    None => {
                        index
                            .checkpoint_at(
                                &part.recording_id,
                                part.part,
                                part.checkpoint_through_seq,
                            )?
                            .ok_or_else(|| {
                                failure("INCOMPLETE", "indexed replay checkpoint is missing")
                            })?
                            .state
                    }
                };
                let hash = digest(&serde_json::to_vec(&state).map_err(io_error)?);
                if snapshot["projection"]["sha256"].as_str() != Some(hash.as_str()) {
                    return Err(failure(
                        "CORRUPT",
                        "checkpoint disagrees with its replay file",
                    ));
                }
                projection = Some(state);
            }
            for observation in &observations {
                let size = serde_json::to_vec(observation).map_err(io_error)?.len();
                budget.charge(size as u64, 1)?;
                if let Some(page) = page.as_deref_mut() {
                    if integer(observation, "capture_seq")? > page.after_seq
                        && integer(observation, "time_us")? >= page.start_us
                        && integer(observation, "time_us")? < page.end_us
                    {
                        page.bytes = page.bytes.saturating_add(size);
                    }
                }
            }
            messages.append(&mut observations);
            previous = Some(block);
            if page.as_ref().is_some_and(|page| page.bytes >= PAGE_LIMIT) {
                limited = true;
                break;
            }
        }
        if stopped_at_end || limited {
            break;
        }
    }
    if !stopped_at_end && !limited && previous.as_ref() != Some(&last) {
        return Err(failure(
            "VIEW_CHANGED",
            "committed replay prefix is incomplete",
        ));
    }
    Ok(PartData {
        context: context.ok_or_else(|| failure("CORRUPT", "missing committed context"))?,
        projection: projection.ok_or_else(|| failure("CORRUPT", "missing committed checkpoint"))?,
        messages,
        limited,
    })
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
        .with_record_length_limit(SCAN_BYTES as usize);
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
        limited: false,
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
    if first && message.data.len() > CHECKPOINT_LIMIT {
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

fn prefix_descriptor(
    index: &BlockIndex,
    recording: &str,
    day: &str,
    part: u32,
    through_seq: u64,
    _metadata: &CatalogRecording,
) -> ReplayResult<Option<PartDescriptor>> {
    let Some((first, last)) = index.file_bounds(recording, part, through_seq)? else {
        return Ok(None);
    };
    let checkpoint_through_seq = first.first_seq - 1;
    let checkpoint_time_us = if checkpoint_through_seq == 0 {
        None
    } else {
        Some(
            index
                .time_at_block_end(recording, checkpoint_through_seq)?
                .ok_or_else(|| failure("INCOMPLETE", "missing previous committed block"))?,
        )
    };
    let size_bytes = last
        .range
        .offset
        .checked_add(last.range.length)
        .ok_or_else(|| failure("CORRUPT", "committed file size overflow"))?;
    let observed_end = last
        .last_us
        .checked_add(1)
        .filter(|end| *end <= JS_SAFE)
        .ok_or_else(|| failure("BUDGET", "committed time bound overflow"))?;
    let end_us = observed_end.max(
        index
            .watermark(recording, part, through_seq)?
            .unwrap_or(observed_end),
    );
    let sha256 = digest(&serde_json::to_vec(&(&first, &last)).map_err(io_error)?);
    Ok(Some(PartDescriptor {
        recording_id: recording.to_owned(),
        part,
        day: day.to_owned(),
        sha256,
        size_bytes,
        first_seq: first.first_seq,
        last_seq: last.last_seq,
        start_us: first.first_us,
        end_us,
        checkpoint_through_seq,
        checkpoint_time_us,
        executions: BTreeSet::new(),
        symbols: BTreeSet::new(),
        committed_prefix: true,
    }))
}

fn recording_views(
    account: &Path,
    execution: &str,
    budget: &mut Budget,
) -> ReplayResult<RecordingIndex> {
    let index = load_read_index(account, execution, budget)?;
    let mut recordings: BTreeMap<String, RecordingView> = BTreeMap::new();
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
    let index_path = child_file(account, ".replay-index.sqlite")?;
    if index_path.exists() {
        let committed = BlockIndex::open(&index_path)?;
        for (recording, day, part) in &index.partial_parts {
            budget.charge(0, 0)?;
            let Some(view) = recordings.get_mut(recording) else {
                continue;
            };
            let Some(metadata) = view.metadata.as_mut() else {
                continue;
            };
            let Some(prefix) =
                prefix_descriptor(&committed, recording, day, *part, JS_SAFE, metadata)?
            else {
                continue;
            };
            metadata.unsealed_start_us = Some(
                metadata
                    .unsealed_start_us
                    .map_or(prefix.end_us, |start| start.max(prefix.end_us)),
            );
            view.parts.push(prefix);
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
    let mut available = Vec::new();
    let mut end_limit = JS_SAFE;
    let mut next: Option<&PartDescriptor> = None;
    for part in view.parts.iter().rev() {
        if next.is_some_and(|next| {
            part.last_seq != next.checkpoint_through_seq
                || part.part.checked_add(1) != Some(next.part)
        }) {
            end_limit = end_limit.min(next.unwrap().start_us);
        }
        next = Some(part);
        let exists = day_directory(account, &part.day, false)
            .and_then(|directory| {
                let sealed = child_file(&directory, &part.file_name())?;
                if part.committed_prefix {
                    let partial = child_file(&directory, &format!("{}.partial", part.file_name()))?;
                    if partial.exists() {
                        return Ok(partial);
                    }
                }
                Ok(sealed)
            })
            .and_then(|path| fs::metadata(path).map_err(io_error))
            .is_ok_and(|metadata| {
                if part.committed_prefix {
                    metadata.len() >= part.size_bytes
                } else {
                    metadata.len() == part.size_bytes
                }
            });
        if exists {
            let mut interval = part.clone();
            interval.end_us = interval.end_us.min(end_limit);
            available.push(interval);
        } else {
            // No window ending after this time may silently skip the missing part.
            end_limit = part.start_us;
        }
    }
    available.reverse();
    let missing = available.len() != view.parts.len();
    if missing {
        gaps.push(json!({"reason":"file_missing_or_changed"}));
    }
    let mut start_limit = 0;
    for index in 0..available.len() {
        let part = &available[index];
        let prefix_available = index > 0
            && available[index - 1].last_seq == part.checkpoint_through_seq
            && available[index - 1].part.checked_add(1) == Some(part.part);
        if !prefix_available {
            // A checkpoint containing @start facts cannot replace a missing same-time prefix.
            start_limit = if part.checkpoint_through_seq > 0 {
                part.checkpoint_time_us.unwrap_or(0).saturating_add(1)
            } else {
                0
            };
        }
        available[index].start_us = part.start_us.max(start_limit);
    }
    let previous = metadata.previous_recording_id.as_ref();
    let order_known = previous.is_some_and(|previous| views.contains_key(previous));
    json!({"recording_id":identity,"coverage":coverage(&available,None,None),
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
    let index = recording_views(&account, execution, &mut budget)?;
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
    fill_legacy_symbol_directories(&account, &views, &mut result, &mut budget)?;
    let partial = result
        .iter()
        .any(|recording| recording["availability"] == "partial");
    let response = json!({"schema_version":1,"account_id":account_id,"execution_id":execution,
        "availability":if result.is_empty() {"not_recorded"} else if partial {"partial"} else {"available"},
        "reason":if result.is_empty() {Some("not_recorded")} else {None},"recordings":result});
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
            let scan = read_part_budget(account, part, budget, None, None, None)
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
    #[serde(skip)]
    parts: Vec<PartDescriptor>,
    first_part: PartDescriptor,
    last_part: PartDescriptor,
    parts_hash: String,
    context_hash: String,
    gaps: Vec<Value>,
    window_id: String,
    after_seq: u64,
    #[serde(default)]
    state_offset: Option<usize>,
    #[serde(default)]
    message_offset: Option<usize>,
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
        view.state_offset = None;
        view.message_offset = None;
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
    account: &Path,
    account_id: u64,
    execution: &str,
    view: &RecordingView,
) -> ReplayResult<WindowCursor> {
    let metadata = view.metadata.as_ref().unwrap();
    let context_hash = digest(&serde_json::to_vec(&metadata.context).map_err(io_error)?);
    if let Some(encoded) = &request.cursor {
        return resume_cursor(
            encoded,
            request,
            account,
            account_id,
            execution,
            view,
            &context_hash,
        );
    }
    let first_window_part = view
        .parts
        .iter()
        .position(|part| part.start_us < request.end_us && part.end_us > request.start_us)
        .map_or(view.parts.len(), |index| index + 1);
    let checkpoint = view.parts[..first_window_part]
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
    }
    validate_part_chain(&parts)?;
    let mut gaps = recorded_gaps(&parts);
    gaps.extend(metadata_gaps(metadata));
    let first_part = parts
        .first()
        .ok_or_else(|| failure("INCOMPLETE", "window has no parts"))?
        .clone();
    let last_part = parts.last().unwrap().clone();
    let parts_hash = digest(&serde_json::to_vec(&parts).map_err(io_error)?);
    let mut cursor = WindowCursor {
        schema_version: 1,
        account_id,
        execution_id: execution.to_owned(),
        recording_id: request.recording_id.clone(),
        start_us: request.start_us,
        end_us: request.end_us,
        symbols: request.symbols.clone(),
        parts,
        first_part,
        last_part,
        parts_hash,
        context_hash,
        gaps,
        window_id: String::new(),
        after_seq: 0,
        state_offset: None,
        message_offset: None,
    };
    cursor.window_id = cursor.view_id()?;
    Ok(cursor)
}

fn resume_cursor(
    encoded: &str,
    request: &WindowRequest,
    account: &Path,
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
    let mut cursor: WindowCursor = serde_json::from_slice(&bytes)
        .map_err(|_| failure("INVALID_CURSOR", "invalid cursor payload"))?;
    if !cursor.matches_request(request, account_id, execution)
        || cursor.context_hash != context_hash
        || cursor.view_id()? != cursor.window_id
    {
        return Err(failure(
            "INVALID_CURSOR",
            "cursor does not match this fixed window",
        ));
    }
    if cursor.first_part.part > cursor.last_part.part {
        return Err(failure("INVALID_CURSOR", "invalid replay part range"));
    }
    cursor.parts = view
        .parts
        .iter()
        .filter(|part| part.part >= cursor.first_part.part && part.part <= cursor.last_part.part)
        .cloned()
        .collect();
    if cursor.last_part.committed_prefix {
        let position = cursor
            .parts
            .iter()
            .position(|part| part.part == cursor.last_part.part)
            .ok_or_else(|| failure("INVALID_CURSOR", "committed replay prefix disappeared"))?;
        let current = &cursor.parts[position];
        if current.recording_id != cursor.last_part.recording_id
            || current.day != cursor.last_part.day
            || current.first_seq != cursor.last_part.first_seq
            || current.last_seq < cursor.last_part.last_seq
        {
            return Err(failure("INVALID_CURSOR", "committed replay prefix changed"));
        }
        cursor.parts[position] = cursor.last_part.clone();
    }
    if cursor.parts.first() != Some(&cursor.first_part)
        || cursor.parts.last() != Some(&cursor.last_part)
        || digest(&serde_json::to_vec(&cursor.parts).map_err(io_error)?) != cursor.parts_hash
    {
        return Err(failure("INVALID_CURSOR", "fixed replay parts changed"));
    }
    for part in &cursor.parts {
        if !part.committed_prefix {
            continue;
        }
        let exists = view.parts.iter().any(|current| {
            current.recording_id == part.recording_id
                && current.part == part.part
                && current.day == part.day
                && current.first_seq == part.first_seq
                && current.last_seq >= part.last_seq
        });
        let index = BlockIndex::open(&child_file(account, ".replay-index.sqlite")?)?;
        let frozen = prefix_descriptor(
            &index,
            &part.recording_id,
            &part.day,
            part.part,
            part.last_seq,
            view.metadata.as_ref().unwrap(),
        )?;
        if !exists
            || frozen.as_ref().is_none_or(|frozen| {
                frozen.sha256 != part.sha256
                    || frozen.size_bytes != part.size_bytes
                    || frozen.first_seq != part.first_seq
                    || frozen.last_seq != part.last_seq
                    || frozen.start_us != part.start_us
                    || frozen.end_us < part.end_us
                    || frozen.checkpoint_through_seq != part.checkpoint_through_seq
                    || frozen.checkpoint_time_us != part.checkpoint_time_us
            })
        {
            return Err(failure("INVALID_CURSOR", "committed replay prefix changed"));
        }
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
    large_message: Option<(u64, String)>,
    last_scanned: u64,
    sparse_market: bool,
    market_keys: BTreeSet<String>,
    market_observations: BTreeSet<u64>,
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

    fn consume(&mut self, mut message: Value) -> ReplayResult<bool> {
        let seq = integer(&message, "capture_seq")?;
        let time = integer(&message, "time_us")?;
        let market = message["topic"]
            .as_str()
            .is_some_and(|topic| topic.starts_with("/market_trade/"));
        if time < self.request.start_us {
            let projection = self.projection.as_mut().unwrap();
            let relevant = stable_market_key(&message["data"])
                .is_some_and(|key| self.market_keys.contains(key))
                || self.market_observations.contains(&seq);
            if self.sparse_market && market && !relevant {
                projection.advance_market_observation(&message)?;
            } else {
                projection.apply(&message)?;
            }
            return Ok(false);
        }
        self.ensure_baseline()?;
        if time >= self.request.end_us {
            return Ok(false);
        }
        if !self.symbols.is_empty() {
            let projection = self.projection.as_mut().unwrap();
            if self.sparse_market && market {
                projection.advance_market_observation(&message)?;
            } else {
                projection.apply(&message)?;
            }
            if message["topic"] == "/order_set"
                && message["data"]["scope_complete"] == true
                && message["data"]["synchronized"] == true
                && message["data"]["query_basis"].is_object()
            {
                let accepted = projection.observed_set_scope(seq);
                if !accepted.contains("") {
                    message["data"]["scope"] = json!({"kind":"symbols","symbols":accepted});
                    if let Some(rows) = message["data"]["orders"].as_array_mut() {
                        rows.retain(|row| {
                            row["symbol"]
                                .as_str()
                                .is_some_and(|symbol| accepted.contains(symbol))
                        });
                    }
                }
            }
        }
        if seq <= self.after_seq {
            return Ok(false);
        }
        let Some(message) = filtered_message(message, self.symbols) else {
            self.last_scanned = seq;
            return Ok(false);
        };
        let encoded = serde_json::to_string(&message).map_err(io_error)?;
        let bytes = encoded.len() + 1;
        if self.messages.len() >= PAGE_MESSAGES
            || self.page_bytes + bytes + self.cursor_reserve > PAGE_LIMIT
        {
            if self.messages.is_empty() {
                if self.first_page {
                    return Ok(true);
                }
                self.large_message = Some((seq, encoded));
                return Ok(true);
            }
            return Ok(true);
        }
        self.page_bytes += bytes;
        self.last_scanned = seq;
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
    let index = recording_views(&account, execution, &mut budget)?;
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
    let mut cursor = select_cursor(&request, &account, account_id, execution, view)?;
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
        large_message: None,
        last_scanned: cursor.after_seq,
        sparse_market: false,
        market_keys: BTreeSet::new(),
        market_observations: BTreeSet::new(),
    };
    let index_path = child_file(&account, ".replay-index.sqlite")?;
    let mut paged_read = !first_page && cursor.state_offset.is_none() && index_path.exists();
    let checkpoint = if index_path.exists() {
        let index = BlockIndex::open(&index_path)?;
        let through_seq = if paged_read {
            cursor.after_seq
        } else {
            cursor.parts.last().unwrap().last_seq
        };
        let checkpoint_time = if paged_read {
            request.end_us
        } else {
            request.start_us
        };
        index
            .checkpoint_before(&request.recording_id, checkpoint_time, through_seq)?
            .filter(|checkpoint| {
                checkpoint.seq >= cursor.parts[0].checkpoint_through_seq
                    && cursor.parts.iter().any(|part| {
                        part.part == checkpoint.part
                            && checkpoint.seq >= part.checkpoint_through_seq
                            && checkpoint.seq <= part.last_seq
                    })
            })
    } else {
        None
    };
    paged_read &= checkpoint.is_some();
    if let Some(checkpoint) = &checkpoint {
        page.projection = Some(Projection::restore(checkpoint.state.clone())?);
    }
    if paged_read {
        page.baseline = Some(json!({}));
        page.sparse_market = true;
    }
    if let Some(checkpoint) = checkpoint.as_ref().filter(|_| !paged_read) {
        let index = BlockIndex::open(&index_path)?;
        let references = index.market_references(
            &request.recording_id,
            checkpoint.seq,
            cursor.parts.last().unwrap().last_seq,
            request.end_us,
        )?;
        for reference in &references {
            let time = integer(reference, "time_us")?;
            if time < request.start_us || time >= request.end_us {
                continue;
            }
            if !symbols.is_empty()
                && reference["symbol"]
                    .as_str()
                    .is_none_or(|symbol| !symbols.contains(symbol))
            {
                continue;
            }
            if let Some(key) = reference["trade_key"].as_str() {
                page.market_keys.insert(key.to_owned());
            }
        }
        for reference in &references {
            if reference["trade_key"]
                .as_str()
                .is_some_and(|key| page.market_keys.contains(key))
            {
                if let Some(seq) = reference["related_capture_seq"].as_u64() {
                    page.market_observations.insert(seq);
                }
            }
        }
    }
    let mut loaded_parts = Vec::new();
    let mut page_read = PageRead {
        after_seq: cursor.after_seq,
        start_us: request.start_us,
        end_us: request.end_us,
        bytes: 0,
    };
    let mut read_limited = false;
    for part in &cursor.parts {
        if checkpoint
            .as_ref()
            .is_some_and(|checkpoint| part.part < checkpoint.part)
        {
            continue;
        }
        let resume = checkpoint
            .as_ref()
            .filter(|checkpoint| part.part == checkpoint.part)
            .map(|checkpoint| (checkpoint, &context));
        let data = read_part_budget(
            &account,
            part,
            &mut budget,
            resume,
            Some(request.end_us),
            checkpoint.is_some().then_some(&mut page_read),
        )?;
        if data.context != context {
            return Err(failure(
                "CORRUPT",
                "part context differs from the fixed catalog view",
            ));
        }
        read_limited = data.limited;
        loaded_parts.push((part, data));
        if read_limited {
            break;
        }
    }
    if let Some((part, data)) = loaded_parts.first() {
        page.begin_part(data, part)?;
    }
    if checkpoint.is_some() && !paged_read {
        let index = BlockIndex::open(&index_path)?;
        let projection = page
            .projection
            .as_mut()
            .ok_or_else(|| failure("INCOMPLETE", "missing indexed checkpoint"))?;
        let through_seq = projection.state_through_seq;
        for key in &page.market_keys {
            if let Some((_, Some(value))) =
                index.fact_at(&request.recording_id, "market_trade", key, through_seq)?
            {
                budget.charge(
                    serde_json::to_vec(&value).map_err(io_error)?.len() as u64,
                    0,
                )?;
                projection.hydrate_market_fact(key, &value)?;
            }
        }
        for seq in &page.market_observations {
            if let Some((_, Some(value))) = index.fact_at(
                &request.recording_id,
                "market_observation",
                &seq.to_string(),
                through_seq,
            )? {
                budget.charge(
                    serde_json::to_vec(&value).map_err(io_error)?.len() as u64,
                    0,
                )?;
                projection.hydrate_market_observation(*seq, &value)?;
            }
        }
        page.sparse_market = true;
    }
    for (part, data) in &loaded_parts {
        page.begin_part(data, part)?;
        for message in &data.messages {
            if integer(message, "time_us")? >= request.start_us {
                continue;
            }
            page.consume(message.clone())?;
            budget.charge(0, 0)?;
        }
    }
    page.ensure_baseline()?;
    let state_json = serde_json::to_string(page.baseline.as_ref().unwrap()).map_err(io_error)?;
    let needs_state_pages = first_page
        && page
            .page_bytes
            .saturating_add(page.cursor_reserve)
            .saturating_add(128)
            > PAGE_LIMIT;
    if cursor.state_offset.is_some() || needs_state_pages {
        let start = cursor.state_offset.unwrap_or(0);
        if start > state_json.len() || !state_json.is_char_boundary(start) {
            return Err(failure("INVALID_CURSOR", "invalid replay state offset"));
        }
        cursor.state_offset = None;
        if start < state_json.len() {
            let mut end = start
                .saturating_add(STATE_FRAGMENT_BYTES)
                .min(state_json.len());
            while !state_json.is_char_boundary(end) {
                end -= 1;
            }
            response["state_before_start"] = Value::Null;
            response["state_fragment"] = Value::String(state_json[start..end].to_owned());
            response["state_fragment_end"] = Value::Bool(end == state_json.len());
            cursor.state_offset = Some(end);
            cursor.after_seq = cursor.after_seq.max(cursor.parts[0].checkpoint_through_seq);
            response["next_cursor"] = Value::String(cursor.encode()?);
            response["complete"] = Value::Bool(false);
            return Ok(response);
        }
    }
    if first_page {
        response["state_before_start"] = page.baseline.clone().unwrap();
    }
    for (part, data) in loaded_parts {
        page.begin_part(&data, part)?;
        for message in data.messages {
            if integer(&message, "time_us")? < request.start_us {
                continue;
            }
            let page_full = page.consume(message)?;
            budget.charge(0, 0)?;
            if page_full {
                more = true;
                break;
            }
        }
        if more {
            break;
        }
    }
    more |= read_limited;
    if let Some((seq, encoded)) = page.large_message {
        let start = cursor.message_offset.unwrap_or(0);
        if start >= encoded.len() || !encoded.is_char_boundary(start) {
            return Err(failure("INVALID_CURSOR", "invalid replay message offset"));
        }
        let mut end = start
            .saturating_add(STATE_FRAGMENT_BYTES)
            .min(encoded.len());
        while !encoded.is_char_boundary(end) {
            end -= 1;
        }
        response["message_fragment"] = Value::String(encoded[start..end].to_owned());
        response["message_fragment_end"] = Value::Bool(end == encoded.len());
        cursor.message_offset = (end < encoded.len()).then_some(end);
        if cursor.message_offset.is_none() {
            cursor.after_seq = seq;
        }
        response["next_cursor"] = Value::String(cursor.encode()?);
        response["complete"] = Value::Bool(false);
        return Ok(response);
    }
    if cursor.message_offset.is_some() {
        return Err(failure(
            "INVALID_CURSOR",
            "fragmented replay message changed",
        ));
    }
    response["messages"] = Value::Array(page.messages);
    response["complete"] = Value::Bool(!more);
    if more {
        cursor.after_seq = page
            .last_scanned
            .max(cursor.parts[0].checkpoint_through_seq);
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
    if data["scope"]["kind"] == "account" {
        data["scope"] = json!({"kind":"account","symbols":[]});
    } else if let Some(original) = scope_symbols(data) {
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
        "last_set_observation",
        "sync_generation",
        "synchronized",
    ] {
        if let Some(rows) = state.get_mut(field).and_then(Value::as_object_mut) {
            rows.retain(|key, row| {
                symbols.contains(key)
                    || (field == "last_set_observation" && key.is_empty())
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

    #[test]
    fn filtered_pages_keep_all_matching_events_across_indexed_blocks() {
        use crate::execution_replay::ExecutionReplayWriter;
        let root =
            std::env::temp_dir().join(format!("axile-filtered-pages-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let mut writer =
            ExecutionReplayWriter::new(&root, 1, "pages", r#"{"schema_version":1}"#).unwrap();
        let payload = "x".repeat(128 * 1024);
        for seq in 1..=90 {
            let (topic, data) = if seq == 1 {
                (
                    "/execution",
                    json!({"execution_id":"E","phase":"started","symbol":"X"}),
                )
            } else if seq == 2 || seq == 90 {
                (
                    "/market_trade/X",
                    json!({"symbol":"X","trade_key":"T", "identity_quality":"stable", "price":100,"quantity":3}),
                )
            } else if seq % 2 == 0 {
                ("/quote/X", json!({"symbol":"X","source":payload}))
            } else {
                ("/quote/Y", json!({"symbol":"Y","source":payload}))
            };
            writer
                .append(
                    &json!({"schema_version":1,"recording_id":"pages","capture_seq":seq,
                "time_us":100+seq,"topic":topic,"data":data})
                    .to_string(),
                )
                .unwrap();
            if seq % 6 == 0 {
                writer.seal().unwrap();
            }
        }
        writer.finish().unwrap();
        let mut request =
            json!({"recording_id":"pages","start_us":103,"end_us":200,"symbols":["X"]});
        let mut sequences = Vec::new();
        let mut pages = 0;
        loop {
            let response = window(&root, 1, "E", &request.to_string()).unwrap();
            pages += 1;
            if pages == 1 {
                assert!(
                    response["state_before_start"]["trade_accounting"]["market_records"]["T"]
                        .is_object()
                );
            }
            assert!(pages < 10, "pagination must make progress");
            sequences.extend(
                response["messages"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|item| item["capture_seq"].as_u64().unwrap()),
            );
            if response["complete"] == true {
                break;
            }
            request["cursor"] = response["next_cursor"].clone();
        }
        let expected: Vec<u64> = (4..=90).step_by(2).collect();
        assert_eq!(sequences, expected);
        assert!(pages > 1);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn late_window_uses_previous_checkpoint_with_a_small_read_budget() {
        use crate::execution_replay::ExecutionReplayWriter;
        let root =
            std::env::temp_dir().join(format!("axile-checkpoint-window-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let mut writer =
            ExecutionReplayWriter::new(&root, 1, "checkpoint", r#"{"schema_version":1}"#).unwrap();
        let base = 1_793_318_400_000_000_u64;
        for seq in 1..=10_002 {
            let (topic, data) = if seq == 1 {
                (
                    "/execution",
                    json!({"execution_id":"E","phase":"started","symbol":"X"}),
                )
            } else {
                ("/quote/X", json!({"symbol":"X","last_price":100}))
            };
            writer
                .append(
                    &json!({"schema_version":1,"recording_id":"checkpoint","capture_seq":seq,
                "time_us":base+seq,"topic":topic,"data":data})
                    .to_string(),
                )
                .unwrap();
        }
        writer.seal().unwrap();
        let account = root.join("account-1");
        let index = BlockIndex::open(&account.join(".replay-index.sqlite")).unwrap();
        let start = base + 10_001;
        let checkpoint = index
            .checkpoint_before("checkpoint", start, 10_002)
            .unwrap()
            .unwrap();
        assert_eq!(checkpoint.seq, 10_000);
        let index = recording_views(&account, "E", &mut Budget::default()).unwrap();
        let view = &index.views["checkpoint"];
        let part = &view.parts[0];
        let context = &view.metadata.as_ref().unwrap().context;
        let mut budget = Budget {
            bytes: SCAN_BYTES - 4096,
            ..Budget::default()
        };
        let data = read_part_budget(
            &account,
            part,
            &mut budget,
            Some((&checkpoint, context)),
            None,
            None,
        )
        .unwrap();
        assert_eq!(
            data.messages
                .iter()
                .map(|item| item["capture_seq"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![10_001, 10_002]
        );
        let page = window(
            &root,
            1,
            "E",
            &json!({"recording_id":"checkpoint","start_us":start,"end_us":start+1}).to_string(),
        )
        .unwrap();
        assert_eq!(page["state_before_start"]["state_through_seq"], 10_000);
        assert_eq!(page["messages"][0]["capture_seq"], 10_001);
        writer.finish().unwrap();
        drop(writer);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn checkpoint_at_window_microsecond_does_not_absorb_boundary_events() {
        use crate::execution_replay::ExecutionReplayWriter;
        let root =
            std::env::temp_dir().join(format!("axile-checkpoint-boundary-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let mut writer =
            ExecutionReplayWriter::new(&root, 1, "same-time", r#"{"schema_version":1}"#).unwrap();
        let time = 1_793_318_400_000_000_u64;
        for seq in 1..=10_001 {
            let (topic, data) = if seq == 1 {
                (
                    "/execution",
                    json!({"execution_id":"E","phase":"started","symbol":"X"}),
                )
            } else {
                ("/quote/X", json!({"symbol":"X","last_price":100}))
            };
            writer
                .append(
                    &json!({"schema_version":1,"recording_id":"same-time","capture_seq":seq,
                "time_us":time,"topic":topic,"data":data})
                    .to_string(),
                )
                .unwrap();
        }
        writer.seal().unwrap();
        let index = BlockIndex::open(&root.join("account-1/.replay-index.sqlite")).unwrap();
        assert_eq!(
            index
                .checkpoint_before("same-time", time, 10_001)
                .unwrap()
                .unwrap()
                .seq,
            0
        );
        let page = window(
            &root,
            1,
            "E",
            &json!({"recording_id":"same-time","start_us":time,"end_us":time+1}).to_string(),
        )
        .unwrap();
        assert_eq!(page["state_before_start"]["state_through_seq"], 0);
        assert_eq!(page["messages"][0]["capture_seq"], 1);
        assert_eq!(page["messages"].as_array().unwrap().len(), 2000);
        assert_eq!(page["complete"], false);
        writer.finish().unwrap();
        drop(index);
        drop(writer);
        fs::remove_dir_all(root).unwrap();
    }

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
            committed_prefix: false,
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
        let cursor = select_cursor(&request, Path::new("."), 1, "7", &view).unwrap();
        assert_eq!(cursor.parts.len(), 2);
        assert_eq!(cursor.parts[0].checkpoint_through_seq, 0);
    }

    #[test]
    fn quiet_window_does_not_depend_on_a_future_part_checkpoint() {
        use crate::execution_replay::ExecutionReplayWriter;
        let root = std::env::temp_dir().join(format!(
            "axile-quiet-window-checkpoint-{}",
            std::process::id()
        ));
        fs::create_dir(&root).unwrap();
        let mut writer =
            ExecutionReplayWriter::new(&root, 1, "quiet", r#"{"schema_version":1}"#).unwrap();
        for (seq, time) in [(1, 100), (2, 120_000_100)] {
            if seq == 2 {
                writer.poll(60_000_100).unwrap();
            }
            writer
                .append(
                    &json!({"schema_version":1,"recording_id":"quiet","capture_seq":seq,
                        "time_us":time,"topic":"/execution","data":{"execution_id":"7",
                            "phase":"started","symbols":["A"]}})
                    .to_string(),
                )
                .unwrap();
        }
        writer.finish().unwrap();
        let request = r#"{"recording_id":"quiet","start_us":101,"end_us":102}"#;
        let before = window(&root, 1, "7", request).unwrap();
        assert_eq!(before["coverage"], json!([{"start_us":101,"end_us":102}]));
        assert_eq!(before["state_before_start"]["state_through_seq"], 1);
        assert_eq!(before["messages"], json!([]));
        assert_eq!(before["complete"], true);

        let advertised = manifest(&root, 1, "7").unwrap();
        assert_eq!(
            advertised["recordings"][0]["coverage"],
            json!([{"start_us":100,"end_us":120_000_101}])
        );
        assert_eq!(
            advertised["recordings"][0]["parts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let after = window(&root, 1, "7", request).unwrap();
        assert_eq!(after["coverage"], before["coverage"]);
        assert_eq!(after["state_before_start"], before["state_before_start"]);
        assert_eq!(after["messages"], json!([]));
        assert_eq!(after["complete"], true);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn indexed_recordings_can_be_read_beyond_old_scan_thresholds() {
        let mut budget = Budget {
            indexed: true,
            bytes: SCAN_BYTES,
            events: SCAN_EVENTS,
            ..Budget::default()
        };
        budget.charge(1, 1).unwrap();
    }

    #[test]
    fn cursor_spans_many_files_without_growing_with_the_file_count() {
        let mut view = RecordingView {
            metadata: Some(metadata()),
            parts: (1..=200)
                .map(|n| {
                    part(
                        n,
                        u64::from(n - 1),
                        u64::from(n),
                        u64::from(n),
                        100,
                        (n > 1).then_some(100),
                    )
                })
                .collect(),
        };
        let mut request = WindowRequest {
            recording_id: "A".into(),
            start_us: 100,
            end_us: 101,
            symbols: vec![],
            cursor: None,
        };
        let mut cursor = select_cursor(&request, Path::new("."), 1, "7", &view).unwrap();
        assert_eq!(cursor.parts.len(), 200);
        cursor.after_seq = 1;
        let encoded = cursor.encode().unwrap();
        assert!(encoded.len() < 4_096);
        request.cursor = Some(encoded);
        assert_eq!(
            select_cursor(&request, Path::new("."), 1, "7", &view)
                .unwrap()
                .parts
                .len(),
            200
        );
        view.parts[150].sha256 = "1".repeat(64);
        assert!(select_cursor(&request, Path::new("."), 1, "7", &view).is_err());
    }

    #[test]
    fn checkpoint_before_a_window_remains_usable_before_its_part_starts() {
        let view = RecordingView {
            metadata: Some(metadata()),
            parts: vec![part(2, 2, 3, 4, 200, Some(100))],
        };
        for end_us in [180, 210] {
            let request = WindowRequest {
                recording_id: "A".into(),
                start_us: 150,
                end_us,
                symbols: vec![],
                cursor: None,
            };
            let cursor = select_cursor(&request, Path::new("."), 1, "7", &view).unwrap();
            assert_eq!(cursor.parts.len(), 1);
            assert_eq!(cursor.parts[0].part, 2);
        }
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
        let mut cursor = select_cursor(&request, Path::new("."), 1, "7", &view).unwrap();
        cursor.after_seq = 1;
        request.cursor = Some(cursor.encode().unwrap());
        view.parts.push(part(2, 2, 3, 4, 150, Some(109)));
        assert_eq!(
            select_cursor(&request, Path::new("."), 1, "7", &view)
                .unwrap()
                .parts
                .len(),
            1
        );
        request.recording_id = "B".into();
        assert!(select_cursor(&request, Path::new("."), 1, "7", &view).is_err());
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
        let mut state = state;
        state["last_set_observation"] = json!({"": [2, 3, 7], "A": [2, 4, 8], "B": [2, 5, 9]});
        let state = filtered_projection(state, &symbols);
        assert_eq!(
            state["last_set_observation"],
            json!({"": [2, 3, 7], "A": [2, 4, 8]})
        );
        assert_eq!(state["trade_accounting"]["own_seen_keys"], json!(["a"]));
        assert_eq!(
            state["trade_accounting"]["execution_totals"]["7"]["by_symbol"],
            json!({"A":{"quantity":4}})
        );
    }

    #[test]
    fn account_scope_keeps_global_meaning_while_rows_are_filtered() {
        let symbols = BTreeSet::from(["A".into()]);
        for topic in ["/connection", "/order_set", "/account_observation"] {
            let message = json!({"topic":topic,"data":{"scope":{"kind":"account","symbols":[]},
                "scope_complete":true,"connected":false,"orders":[{"symbol":"A"},{"symbol":"B"}],
                "positions":[{"symbol":"A"},{"symbol":"B"}]}});
            let message = filtered_message(message, &symbols).unwrap();
            assert_eq!(
                message["data"]["scope"],
                json!({"kind":"account","symbols":[]})
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
    fn later_file_remains_readable_when_an_earlier_file_is_missing() {
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
                writer.rotate_for_test(200).unwrap();
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
        fs::remove_file(root.join("account-1/1970-01-01/recording-legacy-000001.mcap")).unwrap();
        let result = manifest(&root, 1, "E").unwrap();
        assert_eq!(result["availability"], "partial");
        assert_eq!(result["recordings"][0]["symbols"], json!(["A"]));
        assert_eq!(result["recordings"][0]["symbols_complete"], true);
        let later = window(
            &root,
            1,
            "E",
            &json!({"recording_id":"legacy","start_us":200,"end_us":201}).to_string(),
        )
        .unwrap();
        assert_eq!(later["state_before_start"]["state_through_seq"], 2);
        assert_eq!(later["messages"][0]["capture_seq"], 3);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn filtered_queries_replay_like_the_original_account_projection() {
        use crate::execution_replay::ExecutionReplayWriter;
        for (name, listed, paged) in [
            ("empty", false, false),
            ("listed", true, false),
            ("paged", false, true),
        ] {
            let root = std::env::temp_dir()
                .join(format!("axile-filter-query-{}-{name}", std::process::id()));
            fs::create_dir(&root).unwrap();
            let mut writer =
                ExecutionReplayWriter::new(&root, 7, "filtered", r#"{"schema_version":1}"#)
                    .unwrap();
            let order = |symbol: &str, price: u64| {
                json!({"symbol":symbol,"order_id":"O","status":"working",
                "confirmed_book":true,"quantity":1,"remaining_quantity":1,"price":price,"sync_generation":1})
            };
            let basis = |revision: u64, start: u64| json!({"recording_id":"filtered","request_revision":revision,"start_seq":start});
            let mut events = vec![
                (
                    "/execution".to_owned(),
                    json!({"execution_id":"E","phase":"start","symbols":["A"]}),
                ),
                ("/order".to_owned(), order("A", 100)),
                (
                    "/order_set".to_owned(),
                    json!({"scope":{"kind":"symbols","symbols":["A"]},"scope_complete":true,
                    "synchronized":true,"sync_generation":1,"query_basis":basis(2,2),"orders":[order("A",101)]}),
                ),
                (
                    "/order_set".to_owned(),
                    json!({"scope":{"kind":"account","symbols":[]},"scope_complete":true,
                    "synchronized":true,"sync_generation":1,"query_basis":basis(1,2),
                    "orders":if listed {vec![order("B",101)]} else {vec![]}}),
                ),
                ("/order".to_owned(), {
                    let mut row = order("B", 100);
                    row["query_basis"] = basis(3, 2);
                    row
                }),
            ];
            if paged {
                events.extend((0..PAGE_MESSAGES).map(|_| {
                    (
                        "/quote/B".to_owned(),
                        json!({"symbol":"B","book_valid":false}),
                    )
                }));
                let start = events.len() as u64;
                events.push(("/order_set".to_owned(), json!({"scope":{"kind":"account","symbols":[]},"scope_complete":true,
                    "synchronized":true,"sync_generation":1,"query_basis":basis(4,start),"orders":[]})));
            }
            for (index, (topic, data)) in events.into_iter().enumerate() {
                let seq = index as u64 + 1;
                writer
                    .append(
                        &json!({"schema_version":1,"recording_id":"filtered","capture_seq":seq,
                    "time_us":100+seq,"topic":topic,"data":data})
                        .to_string(),
                    )
                    .unwrap();
            }
            writer.finish().unwrap();
            let mut request =
                json!({"recording_id":"filtered","start_us":100,"end_us":10_000,"symbols":["B"]});
            let first = window(&root, 7, "E", &request.to_string()).unwrap();
            assert_eq!(first["state_before_start"]["state_through_seq"], 0);
            let mut replayed = Projection::new("filtered");
            let mut page = first;
            loop {
                for message in page["messages"].as_array().unwrap() {
                    replayed.apply(message).unwrap();
                }
                if page["complete"] == true {
                    break;
                }
                request["cursor"] = page["next_cursor"].clone();
                page = window(&root, 7, "E", &request.to_string()).unwrap();
            }
            let restored = window(
                &root,
                7,
                "E",
                r#"{"recording_id":"filtered","start_us":10000,"end_us":10001,"symbols":["B"]}"#,
            )
            .unwrap();
            let actual =
                filtered_projection(replayed.snapshot().unwrap(), &BTreeSet::from(["B".into()]));
            for field in [
                "orders",
                "last_set_observation",
                "last_set_revision",
                "order_change_seq",
                "sync_generation",
                "synchronized",
            ] {
                assert_eq!(
                    actual[field], restored["state_before_start"][field],
                    "{name}: {field}"
                );
            }
            let order = &actual["orders"][r#"["B","O"]"#];
            assert_eq!(order["confirmed_book"], !paged);
            assert_eq!(order["price"], if listed { 101 } else { 100 });
            fs::remove_dir_all(root).unwrap();
        }
    }
}
