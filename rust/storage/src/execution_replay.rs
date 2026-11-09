//! Account-local execution observations, persisted as sealed, indexed MCAP parts.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::thread::{self, JoinHandle};
use std::time::Instant;

use chrono::{DateTime, Utc};
use chrono_tz::Asia::Shanghai;
use mcap::records::MessageHeader;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::AnalysisError;
use crate::execution_replay_block::BlockRecord;
use crate::execution_replay_index::{BlockIndex, FactVersion, IndexedCheckpoint};
use crate::execution_replay_projection::Projection;

pub(crate) const JS_SAFE: u64 = 9_007_199_254_740_991;
pub(crate) const CONTEXT_LIMIT: usize = 256 * 1024;
pub(crate) const CHECKPOINT_LIMIT: usize = 3 * 1024 * 1024;
pub(crate) const PAGE_LIMIT: usize = 4 * 1024 * 1024;
pub(crate) const PART_LIMIT: u64 = 32 * 1024 * 1024;
pub(crate) const PART_FILE_LIMIT: u64 = 50 * 1024 * 1024;
pub(crate) const CATALOG_LIMIT: usize = 8 * 1024 * 1024;
pub(crate) const SCAN_BYTES: u64 = 128 * 1024 * 1024;
pub(crate) const SCAN_EVENTS: usize = 100_000;
pub(crate) const SCAN_FILES: usize = 4096;
pub(crate) const SCAN_DAYS: usize = 366;
pub(crate) const SCAN_SECONDS: u64 = 5;
pub(crate) const RECOVERY_METADATA_LIMIT: usize = 64 * 1024;
pub(crate) const RECOVERY_METADATA_NAME: &str = "axile.execution_replay.part.v1";
const CHECKPOINT_EVENTS: u64 = 10_000;
const CHECKPOINT_BYTES: u64 = 8 * 1024 * 1024;
const MARKET_FACTS_PER_BLOCK: usize = 250;
pub(crate) const SCHEMA: &str =
    include_str!("../../../axile/server/execution_replay/schemas/replay-v1.json");
static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub(crate) type ReplayResult<T> = Result<T, AnalysisError>;

pub(crate) fn failure(code: &str, message: impl Into<String>) -> AnalysisError {
    AnalysisError::new(format!("REPLAY_{code}"), message)
}

pub(crate) fn io_error(error: impl std::fmt::Display) -> AnalysisError {
    failure("IO", error.to_string())
}

pub(crate) fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub(crate) fn checked_us(value: u64) -> ReplayResult<u64> {
    if value > JS_SAFE {
        return Err(failure(
            "INVALID_REQUEST",
            "microseconds exceed JS safe integers",
        ));
    }
    value
        .checked_mul(1000)
        .ok_or_else(|| failure("INVALID_REQUEST", "MCAP nanoseconds overflow"))
}

pub(crate) fn identifier(value: &str) -> ReplayResult<()> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(failure("INVALID_REQUEST", "invalid recording identity"));
    }
    Ok(())
}

pub(crate) fn account_directory(
    root: &Path,
    account_id: u64,
    create: bool,
) -> ReplayResult<PathBuf> {
    if account_id == 0 || account_id > JS_SAFE {
        return Err(failure("INVALID_REQUEST", "invalid account identity"));
    }
    if create {
        fs::create_dir_all(root).map_err(io_error)?;
    }
    let root = root.canonicalize().map_err(io_error)?;
    let account = root.join(format!("account-{account_id}"));
    if create && !account.exists() {
        fs::create_dir(&account).map_err(io_error)?;
    }
    if let Ok(metadata) = fs::symlink_metadata(&account) {
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(failure(
                "PATH",
                "account replay directory must not be a symlink",
            ));
        }
    }
    Ok(account)
}

pub(crate) fn child_file(directory: &Path, name: &str) -> ReplayResult<PathBuf> {
    if name.is_empty() || name.contains(['/', '\\']) || matches!(name, "." | "..") {
        return Err(failure("PATH", "invalid replay file name"));
    }
    let path = directory.join(name);
    if fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err(failure("PATH", "replay files must not be symbolic links"));
    }
    Ok(path)
}

pub(crate) fn read_bounded(path: &Path, limit: usize) -> ReplayResult<Vec<u8>> {
    if fs::symlink_metadata(path)
        .map_err(io_error)?
        .file_type()
        .is_symlink()
    {
        return Err(failure("PATH", "replay files must not be symbolic links"));
    }
    let file = File::open(path).map_err(io_error)?;
    if file.metadata().map_err(io_error)?.len() > limit as u64 {
        return Err(failure("BUDGET", "replay file exceeds read budget"));
    }
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() > limit {
        return Err(failure("BUDGET", "replay file exceeds read budget"));
    }
    Ok(bytes)
}

fn digest_file(path: &Path, limit: u64) -> ReplayResult<(String, u64)> {
    let file = File::open(path).map_err(io_error)?;
    let mut reader = file.take(limit.saturating_add(1));
    let mut hash = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = vec![0; 1024 * 1024];
    loop {
        let size = reader.read(&mut buffer).map_err(io_error)?;
        if size == 0 {
            break;
        }
        total += size as u64;
        if total > limit {
            return Err(failure("BUDGET", "replay file exceeds its size limit"));
        }
        hash.update(&buffer[..size]);
    }
    Ok((format!("{:x}", hash.finalize()), total))
}

pub(crate) struct ReplaySummary {
    pub stats: Option<mcap::records::Statistics>,
    pub schemas: BTreeMap<u16, (mcap::records::SchemaHeader, Vec<u8>)>,
    pub channels: BTreeMap<u16, mcap::records::Channel>,
    pub metadata_indexes: Vec<mcap::records::MetadataIndex>,
    pub chunks: u64,
}

pub(crate) fn read_mcap_summary(path: &Path, limit_time: bool) -> ReplayResult<ReplaySummary> {
    use mcap::records::Record;
    use mcap::sans_io::{LinearReadEvent, LinearReader, LinearReaderOptions};
    let mut file = File::open(path).map_err(io_error)?;
    let file_size = file.metadata().map_err(io_error)?.len();
    if file_size < 45 {
        return Err(failure("CORRUPT", "replay footer is truncated"));
    }
    file.seek(SeekFrom::End(-37)).map_err(io_error)?;
    let mut footer = [0_u8; 37];
    file.read_exact(&mut footer).map_err(io_error)?;
    let length = u64::from_le_bytes(footer[1..9].try_into().unwrap());
    let start = u64::from_le_bytes(footer[9..17].try_into().unwrap());
    if footer[0] != mcap::records::op::FOOTER
        || length != 20
        || &footer[29..] != mcap::MAGIC
        || start < 13
        || start > file_size - 37
    {
        return Err(failure("CORRUPT", "invalid replay summary"));
    }
    file.seek(SeekFrom::Start(start - 13)).map_err(io_error)?;
    let options = LinearReaderOptions::default()
        .with_skip_start_magic(true)
        .with_validate_summary_section_crc(true)
        .with_check_finishes_after_end_magic(true)
        .with_record_length_limit(SCAN_BYTES as usize);
    let mut reader = LinearReader::new_with_options(options);
    let mut result = ReplaySummary {
        stats: None,
        schemas: BTreeMap::new(),
        channels: BTreeMap::new(),
        metadata_indexes: Vec::new(),
        chunks: 0,
    };
    let mut first = true;
    let started = Instant::now();
    while let Some(event) = reader.next_event() {
        if limit_time && started.elapsed().as_secs() >= SCAN_SECONDS {
            return Err(failure("BUDGET", "replay summary scan exceeds time budget"));
        }
        match event.map_err(|error| failure("CORRUPT", error.to_string()))? {
            LinearReadEvent::ReadRequest(size) => {
                let read = file
                    .read(reader.insert(size.min(64 * 1024)))
                    .map_err(io_error)?;
                reader.notify_read(read);
            }
            LinearReadEvent::Record { opcode, data } => {
                if first && opcode != mcap::records::op::DATA_END {
                    return Err(failure("CORRUPT", "replay data end is missing"));
                }
                first = false;
                match mcap::parse_record(opcode, data)
                    .map_err(|error| failure("CORRUPT", error.to_string()))?
                {
                    Record::Schema { header, data } => {
                        if result
                            .schemas
                            .insert(header.id, (header, data.into_owned()))
                            .is_some()
                        {
                            return Err(failure(
                                "CORRUPT",
                                "duplicate or excessive replay schemas",
                            ));
                        }
                    }
                    Record::Channel(channel) => {
                        if result.channels.insert(channel.id, channel).is_some() {
                            return Err(failure(
                                "CORRUPT",
                                "duplicate or excessive replay channels",
                            ));
                        }
                    }
                    Record::Statistics(stats) => {
                        if result.stats.replace(stats).is_some() {
                            return Err(failure("CORRUPT", "duplicate replay statistics"));
                        }
                    }
                    Record::ChunkIndex(_) => result.chunks += 1,
                    Record::MetadataIndex(metadata) => {
                        result.metadata_indexes.push(metadata);
                    }
                    _ => (),
                }
            }
        }
    }
    if first {
        return Err(failure("CORRUPT", "replay summary is missing"));
    }
    Ok(result)
}

struct ExpectedSummary {
    count: u64,
    chunks: u64,
    schema: u16,
    channels: BTreeMap<String, u16>,
}

fn validate_finished_summary(path: &Path, expected: &ExpectedSummary) -> ReplayResult<()> {
    let summary = read_mcap_summary(path, false)?;
    let count = expected.count;
    if summary
        .stats
        .as_ref()
        .is_none_or(|stats| stats.message_count != count)
        || summary.chunks != expected.chunks
        || summary
            .schemas
            .get(&expected.schema)
            .is_none_or(|(header, data)| {
                header.encoding != "jsonschema" || data != SCHEMA.as_bytes()
            })
        || expected.channels.iter().any(|(topic, id)| {
            summary
                .channels
                .get(id)
                .is_none_or(|channel| channel.topic != *topic || channel.message_encoding != "json")
        })
        || summary.channels.len() != expected.channels.len()
    {
        return Err(failure(
            "CORRUPT",
            "replay summary does not match committed data",
        ));
    }
    Ok(())
}

pub(crate) fn atomic_json(path: &Path, value: &impl Serialize, limit: usize) -> ReplayResult<()> {
    let bytes = serde_json::to_vec(value).map_err(io_error)?;
    if bytes.len() > limit {
        return Err(failure("BUDGET", "replay sidecar exceeds budget"));
    }
    let name = format!(
        ".replay-{}-{}.tmp",
        std::process::id(),
        TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    );
    let temporary = child_file(
        path.parent()
            .ok_or_else(|| failure("PATH", "missing parent directory"))?,
        &name,
    )?;
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(io_error)?;
        file.write_all(&bytes).map_err(io_error)?;
        file.sync_all().map_err(io_error)?;
        fs::rename(&temporary, path).map_err(io_error)?;
        sync_directory(path.parent().unwrap())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

pub(crate) fn sync_directory(path: &Path) -> ReplayResult<()> {
    #[cfg(unix)]
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(io_error)?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct PartDescriptor {
    pub recording_id: String,
    pub part: u32,
    pub day: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub first_seq: u64,
    pub last_seq: u64,
    pub start_us: u64,
    pub end_us: u64,
    pub checkpoint_through_seq: u64,
    pub checkpoint_time_us: Option<u64>,
    pub executions: BTreeSet<String>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub symbols: BTreeSet<String>,
    #[serde(default)]
    pub committed_prefix: bool,
}

impl PartDescriptor {
    pub(crate) fn file_name(&self) -> String {
        format!("recording-{}-{:06}.mcap", self.recording_id, self.part)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecoveryPartMeta {
    pub schema_version: u32,
    pub account_id: u64,
    pub previous_recording_id: Option<String>,
    pub descriptor: PartDescriptor,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CatalogRecording {
    pub context: Value,
    pub previous_recording_id: Option<String>,
    pub executions: BTreeSet<String>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub symbols: BTreeSet<String>,
    pub state: String,
    pub unsealed_start_us: Option<u64>,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Catalog {
    pub schema_version: u32,
    pub day: String,
    pub recordings: BTreeMap<String, CatalogRecording>,
    pub parts: Vec<PartDescriptor>,
}

pub(crate) fn day_directory(account: &Path, day: &str, create: bool) -> ReplayResult<PathBuf> {
    if day.len() != 10 || chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d").is_err() {
        return Err(failure("PATH", "invalid replay day"));
    }
    let directory = child_file(account, day)?;
    if create && !directory.exists() {
        fs::create_dir(&directory).map_err(io_error)?;
    }
    if directory.exists() && !directory.is_dir() {
        return Err(failure("PATH", "replay day is not a directory"));
    }
    Ok(directory)
}

const FORBIDDEN: &str = "password passwd secret api_key apikey api_secret apisecret access_token refresh_token authorization credentials account_password broker_password private_key privatekey signing_key auth_token cookie cookies session_token account_config raw raw_data raw_trade_data raw_order_data raw_position_data extra";

fn data_fields() -> &'static BTreeSet<String> {
    static FIELDS: OnceLock<BTreeSet<String>> = OnceLock::new();
    FIELDS.get_or_init(|| {
        let schema: Value =
            serde_json::from_str(SCHEMA).expect("embedded replay schema is valid JSON");
        schema["$defs"]["data"]["properties"]
            .as_object()
            .expect("embedded replay schema declares its fields")
            .keys()
            .cloned()
            .collect()
    })
}

fn validate_json(value: &Value, depth: usize) -> ReplayResult<()> {
    if depth > 12 {
        return Err(failure("BUDGET", "JSON structure exceeds budget"));
    }
    match value {
        Value::Number(number) => {
            let literal = number.to_string();
            let integer = !literal.contains(['.', 'e', 'E']);
            if number
                .as_i64()
                .is_some_and(|value| value.unsigned_abs() > JS_SAFE)
                || number.as_u64().is_some_and(|value| value > JS_SAFE)
                || (integer && number.as_i64().is_none() && number.as_u64().is_none())
                || number.as_f64().is_none_or(|value| !value.is_finite())
            {
                return Err(failure(
                    "INVALID_REQUEST",
                    "JSON number is not a finite safe scalar",
                ));
            }
        }
        Value::Array(items) => {
            for item in items {
                validate_json(item, depth + 1)?;
            }
        }
        Value::Object(fields) => {
            for (name, value) in fields {
                if name.len() > 128
                    || FORBIDDEN
                        .split_whitespace()
                        .any(|forbidden| forbidden.eq_ignore_ascii_case(name))
                {
                    return Err(failure(
                        "INVALID_REQUEST",
                        "forbidden or oversized JSON field",
                    ));
                }
                validate_json(value, depth + 1)?;
            }
        }
        _ => (),
    }
    Ok(())
}

fn unsigned(value: &Value, name: &str) -> ReplayResult<u64> {
    value
        .get(name)
        .and_then(Value::as_u64)
        .filter(|value| *value <= JS_SAFE)
        .ok_or_else(|| {
            failure(
                "INVALID_REQUEST",
                format!("{name} must be a nonnegative safe integer"),
            )
        })
}

pub(crate) fn validate_message(message: &Value, context: &Value) -> ReplayResult<()> {
    let object = message
        .as_object()
        .ok_or_else(|| failure("INVALID_REQUEST", "message must be an object"))?;
    if object.keys().any(|name| {
        ![
            "schema_version",
            "recording_id",
            "capture_seq",
            "time_us",
            "topic",
            "data",
        ]
        .contains(&name.as_str())
    }) || unsigned(message, "schema_version")? != 1
    {
        return Err(failure("SCHEMA", "unsupported event envelope"));
    }
    identifier(
        message
            .get("recording_id")
            .and_then(Value::as_str)
            .unwrap_or(""),
    )?;
    let sequence = unsigned(message, "capture_seq")?;
    if sequence == 0 {
        return Err(failure("INVALID_REQUEST", "event sequence starts at one"));
    }
    checked_us(unsigned(message, "time_us")?)?;
    let topic = message.get("topic").and_then(Value::as_str).unwrap_or("");
    let known = [
        "/execution",
        "/command",
        "/order",
        "/order_set",
        "/trade",
        "/account_observation",
        "/connection",
    ]
    .contains(&topic);
    let symbol_topic = topic
        .strip_prefix("/quote/")
        .or_else(|| topic.strip_prefix("/market_trade/"));
    if !known
        && symbol_topic
            .is_none_or(|symbol| symbol.is_empty() || symbol.len() > 256 || symbol.contains('/'))
    {
        return Err(failure("SCHEMA", "unsupported replay topic"));
    }
    let data = message
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| failure("SCHEMA", "event data must be an object"))?;
    if data.keys().any(|name| !data_fields().contains(name)) {
        return Err(failure("SCHEMA", "event contains undeclared common fields"));
    }
    if symbol_topic.is_some_and(|symbol| data.get("symbol").and_then(Value::as_str) != Some(symbol))
    {
        return Err(failure("SCHEMA", "topic and payload symbols differ"));
    }
    validate_extensions(topic, data, context)?;
    validate_quality(data)?;
    validate_shapes(data, message["recording_id"].as_str().unwrap_or_default())?;
    validate_rows(
        data,
        context,
        message["recording_id"].as_str().unwrap_or_default(),
    )?;
    validate_json(message, 0)?;
    Ok(())
}

fn validate_extensions(
    topic: &str,
    data: &serde_json::Map<String, Value>,
    context: &Value,
) -> ReplayResult<()> {
    for (field, declaration) in [
        ("attributes", "attribute_fields"),
        ("native_identity", "native_identity_fields"),
    ] {
        let Some(values) = data.get(field) else {
            continue;
        };
        let values = values
            .as_object()
            .ok_or_else(|| failure("SCHEMA", "extension values must be objects"))?;
        let declarations = context.get(field).or_else(|| context.get(declaration));
        let kind = topic
            .trim_matches('/')
            .split('/')
            .next()
            .unwrap_or_default();
        let allowed = declarations
            .and_then(|value| {
                value.get(topic).or_else(|| value.get(kind)).or_else(|| {
                    topic
                        .rsplit_once('/')
                        .and_then(|(prefix, _)| value.get(prefix))
                })
            })
            .and_then(Value::as_array);
        if values.keys().any(|key| {
            !allowed.is_some_and(|names| names.iter().any(|name| name.as_str() == Some(key)))
        }) {
            return Err(failure("SCHEMA", "undeclared replay extension field"));
        }
    }
    Ok(())
}

fn validate_quality(data: &serde_json::Map<String, Value>) -> ReplayResult<()> {
    if let Some(value) = data.get("field_quality") {
        let quality = value
            .as_object()
            .ok_or_else(|| failure("SCHEMA", "field_quality must be an object"))?;
        if quality
            .values()
            .any(|value| !matches!(value.as_str(), Some("known" | "missing" | "invalid")))
        {
            return Err(failure("SCHEMA", "invalid field quality"));
        }
    }
    for field in [
        "source_time_us",
        "publish_time_us",
        "valid_until_us",
        "query_started_us",
        "query_finished_us",
    ] {
        if let Some(value) = data.get(field).filter(|value| !value.is_null()) {
            checked_us(value.as_u64().ok_or_else(|| {
                failure("SCHEMA", "time fields must be integer microseconds or null")
            })?)?;
        }
    }
    if let Some(identity) = data.get("identity_quality") {
        match identity.as_str() {
            Some("stable")
                if data
                    .get("trade_key")
                    .and_then(Value::as_str)
                    .is_some_and(|key| !key.is_empty()) =>
            {
                ()
            }
            Some("weak" | "unknown") if data.get("trade_key").is_none_or(Value::is_null) => (),
            _ => return Err(failure("SCHEMA", "trade identity quality and key disagree")),
        }
    }
    Ok(())
}

fn validate_shapes(data: &serde_json::Map<String, Value>, recording_id: &str) -> ReplayResult<()> {
    validate_scalars(data)?;
    validate_collections(data)?;
    validate_query_basis(data, recording_id)
}

fn validate_scalars(data: &serde_json::Map<String, Value>) -> ReplayResult<()> {
    for field in [
        "symbol",
        "source",
        "source_precision",
        "origin",
        "operation",
        "command_id",
        "order_id",
        "phase",
        "kind",
        "direction",
        "position_side",
        "offset",
        "status",
        "trade_key",
        "identity_quality",
    ] {
        if data
            .get(field)
            .is_some_and(|value| !value.is_null() && !value.is_string())
        {
            return Err(failure("SCHEMA", format!("{field} must be string or null")));
        }
    }
    for field in [
        "price",
        "quantity",
        "filled_quantity",
        "remaining_quantity",
        "average_price",
        "target_quantity",
        "trigger_price",
        "last_price",
        "fee",
        "volume",
        "turnover",
    ] {
        if data
            .get(field)
            .is_some_and(|value| !value.is_null() && !value.is_number())
        {
            return Err(failure("SCHEMA", format!("{field} must be number or null")));
        }
    }
    for field in [
        "success",
        "accepted",
        "confirmed_book",
        "on_book",
        "book_valid",
        "reduce_only",
        "close_position",
        "is_maker",
        "connected",
        "ready",
        "available",
    ] {
        if data
            .get(field)
            .is_some_and(|value| !value.is_null() && !value.is_boolean())
        {
            return Err(failure(
                "SCHEMA",
                format!("{field} must be boolean or null"),
            ));
        }
    }
    for field in ["scope_complete", "synchronized", "complete"] {
        if data.get(field).is_some_and(|value| !value.is_boolean()) {
            return Err(failure("SCHEMA", format!("{field} must be boolean")));
        }
    }
    Ok(())
}

fn validate_collections(data: &serde_json::Map<String, Value>) -> ReplayResult<()> {
    if let Some(scope) = data.get("scope") {
        validate_scope(scope)?;
    }
    for field in ["symbols"] {
        if let Some(value) = data.get(field) {
            if !value
                .as_array()
                .is_some_and(|items| items.iter().all(Value::is_string))
            {
                return Err(failure("SCHEMA", format!("{field} must be a symbol array")));
            }
        }
    }
    for field in ["orders", "positions"] {
        if let Some(value) = data.get(field) {
            if !value
                .as_array()
                .is_some_and(|items| items.iter().all(Value::is_object))
            {
                return Err(failure("SCHEMA", format!("{field} must contain objects")));
            }
        }
    }
    for field in ["bids", "asks"] {
        if let Some(value) = data.get(field) {
            let levels = value
                .as_array()
                .ok_or_else(|| failure("SCHEMA", "depth must be an array"))?;
            for level in levels {
                if !level.as_object().is_some_and(|value| {
                    value.len() == 2
                        && value.get("price").is_some_and(Value::is_number)
                        && value
                            .get("quantity")
                            .and_then(Value::as_f64)
                            .is_some_and(|quantity| quantity >= 0.0)
                }) {
                    return Err(failure(
                        "SCHEMA",
                        "depth levels require real price and nonnegative quantity",
                    ));
                }
            }
        }
    }
    Ok(())
}

fn validate_scope(scope: &Value) -> ReplayResult<()> {
    if scope
        .as_array()
        .is_some_and(|symbols| symbols.iter().all(Value::is_string))
    {
        return Ok(());
    }
    let valid = scope.as_object().is_some_and(|fields| {
        fields.len() == 2
            && matches!(scope["kind"].as_str(), Some("account" | "symbols"))
            && scope["symbols"]
                .as_array()
                .is_some_and(|symbols| symbols.iter().all(Value::is_string))
    });
    if !valid {
        return Err(failure(
            "SCHEMA",
            "scope must declare account or symbols coverage",
        ));
    }
    Ok(())
}

fn validate_rows(
    data: &serde_json::Map<String, Value>,
    context: &Value,
    recording_id: &str,
) -> ReplayResult<()> {
    for (field, kind) in [("orders", "/order"), ("positions", "/position")] {
        let Some(rows) = data.get(field).and_then(Value::as_array) else {
            continue;
        };
        for row in rows {
            let fields = row
                .as_object()
                .ok_or_else(|| failure("SCHEMA", "observation rows must be objects"))?;
            if fields.keys().any(|name| {
                !data_fields().contains(name)
                    && !(kind == "/position"
                        && ["long_frozen", "short_frozen"].contains(&name.as_str()))
            }) {
                return Err(failure("SCHEMA", "undeclared observation row field"));
            }
            if fields.contains_key("orders") || fields.contains_key("positions") {
                return Err(failure(
                    "SCHEMA",
                    "nested observation collections are not allowed",
                ));
            }
            validate_extensions(kind, fields, context)?;
            validate_quality(fields)?;
            validate_shapes(fields, recording_id)?;
            validate_source_fields(fields)?;
        }
    }
    validate_source_fields(data)
}

fn validate_source_fields(data: &serde_json::Map<String, Value>) -> ReplayResult<()> {
    if let Some(times) = data.get("source_times") {
        let fields = times
            .as_object()
            .ok_or_else(|| failure("SCHEMA", "source_times must be a scalar object"))?;
        if fields.len() > 32
            || fields
                .values()
                .any(|value| value.is_array() || value.is_object())
        {
            return Err(failure(
                "SCHEMA",
                "source_times must contain bounded source scalars",
            ));
        }
    }
    if let Some(assets) = data.get("assets") {
        let fields = assets
            .as_object()
            .ok_or_else(|| failure("SCHEMA", "assets must be an object"))?;
        if fields.iter().any(|(name, value)| {
            ![
                "balance",
                "available_cash",
                "frozen_margin",
                "equity",
                "margin",
                "available_margin",
                "unrealized_pnl",
                "realized_pnl",
                "currency",
            ]
            .contains(&name.as_str())
                || !(value.is_null()
                    || value.is_number()
                    || (name == "currency" && value.is_string()))
        }) {
            return Err(failure("SCHEMA", "undeclared or invalid asset observation"));
        }
    }
    Ok(())
}

fn validate_query_basis(
    data: &serde_json::Map<String, Value>,
    recording_id: &str,
) -> ReplayResult<()> {
    if let Some(basis) = data.get("query_basis").filter(|value| !value.is_null()) {
        let object = basis
            .as_object()
            .ok_or_else(|| failure("SCHEMA", "query_basis must be an object"))?;
        if object.keys().any(|name| {
            ![
                "start_seq",
                "request_revision",
                "recording_id",
                "sync_generation",
            ]
            .contains(&name.as_str())
        }) {
            return Err(failure("SCHEMA", "unknown query basis field"));
        }
        unsigned(basis, "start_seq")?;
        unsigned(basis, "request_revision")?;
        if basis
            .get("recording_id")
            .is_some_and(|value| value.as_str() != Some(recording_id))
        {
            return Err(failure(
                "SCHEMA",
                "query basis belongs to another recording",
            ));
        }
        if basis
            .get("sync_generation")
            .is_some_and(|value| Some(value) != data.get("sync_generation"))
        {
            return Err(failure(
                "SCHEMA",
                "query basis belongs to another synchronization generation",
            ));
        }
    }
    Ok(())
}

pub(crate) struct CappedFile {
    pub(crate) file: File,
    pub(crate) limit: u64,
}

fn estimated_message_size(bytes: usize, topic_bytes: usize) -> u64 {
    // Reserve MCAP overhead and more than zstd's worst-case expansion.
    let raw = (bytes as u64)
        .saturating_add(topic_bytes as u64)
        .saturating_add(2048);
    raw.saturating_add(raw / 128).saturating_add(128)
}

fn would_exceed_file_limit(part: &ActivePart, limit: u64, next_message: u64) -> bool {
    let file_bytes = part
        .sync_file
        .metadata()
        .map_or(u64::MAX, |metadata| metadata.len());
    let channels = part
        .channels
        .keys()
        .map(|topic| topic.len() as u64 + 128)
        .sum::<u64>();
    let chunks = part
        .summary_chunks
        .saturating_add(part.pending_messages)
        .saturating_add(2);
    let footer = (SCHEMA.len() as u64)
        .saturating_add(2 * RECOVERY_METADATA_LIMIT as u64)
        .saturating_add(64 * 1024)
        .saturating_add(channels)
        .saturating_add(chunks.saturating_mul(128));
    file_bytes
        .saturating_add(part.pending_bound)
        .saturating_add(next_message)
        .saturating_add(footer)
        > limit
}

impl Write for CappedFile {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let position = self.file.stream_position()?;
        if position
            .checked_add(bytes.len() as u64)
            .is_none_or(|end| end > self.limit)
        {
            return Err(io::Error::other("replay file exceeds physical size limit"));
        }
        self.file.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

impl Seek for CappedFile {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        self.file.seek(position)
    }
}

struct ActivePart {
    writer: mcap::Writer<CappedFile>,
    sync_file: File,
    path: PathBuf,
    descriptor: PartDescriptor,
    channels: BTreeMap<String, u16>,
    schema: u16,
    pending_first: Option<(u64, u64)>,
    last_commit: Instant,
    last_watermark: Instant,
    committed_schema: Option<u16>,
    committed_channels: BTreeMap<u16, String>,
    pending_bound: u64,
    pending_messages: u64,
    summary_chunks: u64,
    initial_projection: Option<Value>,
}

struct PendingFact {
    kind: &'static str,
    key: String,
    seq: u64,
    value: Option<Value>,
}

struct PendingSeal {
    descriptor: PartDescriptor,
    path: PathBuf,
    hash: JoinHandle<ReplayResult<(String, u64)>>,
}

/// One account writer, owned by a single writer thread.
pub(crate) struct ExecutionReplayWriter {
    account: PathBuf,
    account_id: u64,
    recording_id: String,
    context: Value,
    previous_recording_id: Option<String>,
    lease: Option<File>,
    projection: Projection,
    index: BlockIndex,
    active: Option<ActivePart>,
    part_number: u32,
    last_seq: u64,
    last_time_us: Option<u64>,
    last_poll_us: Option<u64>,
    metadata_day: Option<String>,
    executions: BTreeSet<String>,
    symbols: BTreeSet<String>,
    sealed: Vec<PartDescriptor>,
    pending_seals: Vec<PendingSeal>,
    file_limit: u64,
    last_checkpoint_seq: u64,
    checkpoint_bytes: u64,
    pending_facts: Vec<PendingFact>,
    pending_fact_bytes: u64,
    market_loaded: BTreeSet<String>,
    market_observations_loaded: BTreeSet<u64>,
    capture_cache: BTreeMap<(String, String), Value>,
    closed: bool,
    error: Option<String>,
}

impl ExecutionReplayWriter {
    pub(crate) fn new(
        root: &Path,
        account_id: u64,
        recording_id: &str,
        context_json: &str,
    ) -> ReplayResult<Self> {
        identifier(recording_id)?;
        if context_json.len() > CONTEXT_LIMIT {
            return Err(failure("BUDGET", "recording context exceeds budget"));
        }
        let context: Value = serde_json::from_str(context_json)
            .map_err(|error| failure("SCHEMA", error.to_string()))?;
        if !context.is_object() || context.get("schema_version").and_then(Value::as_u64) != Some(1)
        {
            return Err(failure(
                "SCHEMA",
                "recording context requires schema_version=1",
            ));
        }
        validate_json(&context, 0)?;
        let account = account_directory(root, account_id, true)?;
        let lease = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(child_file(&account, ".writer.lock")?)
            .map_err(io_error)?;
        lease.try_lock().map_err(|error| match error {
            std::fs::TryLockError::WouldBlock => failure("BUSY", "account writer is still active"),
            std::fs::TryLockError::Error(error) => io_error(error),
        })?;
        let index = BlockIndex::writer(&child_file(&account, ".replay-index.sqlite")?)?;
        let previous_path = child_file(&account, ".latest-recording.json")?;
        let previous_recording_id = if previous_path.exists() {
            let previous: Value = serde_json::from_slice(&read_bounded(&previous_path, 1024)?)
                .map_err(|error| failure("CORRUPT", error.to_string()))?;
            previous
                .get("recording_id")
                .and_then(Value::as_str)
                .map(str::to_owned)
        } else {
            None
        };
        if previous_recording_id.as_deref() == Some(recording_id) {
            return Err(failure("IDENTITY", "recording identity already exists"));
        }
        index.register_recording(recording_id)?;
        atomic_json(&previous_path, &json!({"recording_id":recording_id}), 1024)?;
        Ok(Self {
            account,
            account_id,
            recording_id: recording_id.into(),
            context,
            previous_recording_id,
            lease: Some(lease),
            projection: Projection::new(recording_id),
            index,
            active: None,
            part_number: 0,
            last_seq: 0,
            last_time_us: None,
            last_poll_us: None,
            metadata_day: None,
            executions: BTreeSet::new(),
            symbols: BTreeSet::new(),
            sealed: Vec::new(),
            pending_seals: Vec::new(),
            file_limit: PART_FILE_LIMIT,
            last_checkpoint_seq: 0,
            checkpoint_bytes: 0,
            pending_facts: Vec::new(),
            pending_fact_bytes: 0,
            market_loaded: BTreeSet::new(),
            market_observations_loaded: BTreeSet::new(),
            capture_cache: BTreeMap::new(),
            closed: false,
            error: None,
        })
    }

    fn ensure_open(&self) -> ReplayResult<()> {
        if self.closed {
            return Err(failure("CLOSED", "recording is already closed"));
        }
        if let Some(error) = &self.error {
            return Err(failure("STOPPED", error));
        }
        Ok(())
    }

    pub(crate) fn append(&mut self, message_json: &str) -> ReplayResult<()> {
        self.ensure_open()?;
        let result = self.append_inner(message_json);
        if let Err(error) = &result {
            self.error = Some(error.to_string());
        }
        result
    }

    fn append_inner(&mut self, message_json: &str) -> ReplayResult<()> {
        self.drain_seals(false)?;
        let mut message: Value = serde_json::from_str(message_json)
            .map_err(|error| failure("SCHEMA", error.to_string()))?;
        validate_message(&message, &self.context)?;
        let sequence = unsigned(&message, "capture_seq")?;
        let time_us = unsigned(&message, "time_us")?;
        if message["recording_id"].as_str() != Some(&self.recording_id)
            || sequence != self.last_seq + 1
        {
            return Err(failure(
                "SEQUENCE",
                "recording or consecutive capture sequence mismatch",
            ));
        }
        if self.last_time_us.is_some_and(|previous| time_us < previous)
            || self.last_poll_us.is_some_and(|previous| time_us < previous)
        {
            return Err(failure(
                "CLOCK_ROLLBACK",
                "local observation clock moved backwards",
            ));
        }
        let pending_before = self.pending_facts.len();
        let bytes_before = self.pending_fact_bytes;
        self.enrich_associations(&mut message, sequence)?;
        let staged = self.pending_facts.split_off(pending_before);
        self.pending_fact_bytes = bytes_before;
        validate_message(&message, &self.context)?;
        let message_json = serde_json::to_string(&message).map_err(io_error)?;
        let day = shanghai_day(time_us)?;
        let topic = message["topic"].as_str().unwrap_or_default();
        let mut cost = estimated_message_size(message_json.len(), topic.len());
        if cost > self.file_limit / 2 {
            self.commit_active()?;
            let compressed = zstd::bulk::compress(message_json.as_bytes(), 0).map_err(io_error)?;
            cost = (compressed.len() as u64)
                .saturating_add(topic.len() as u64)
                .saturating_add(4096);
        }
        if self
            .active
            .as_ref()
            .is_some_and(|part| would_exceed_file_limit(part, self.file_limit, cost))
        {
            self.commit_active()?;
            if self
                .active
                .as_ref()
                .is_some_and(|part| would_exceed_file_limit(part, self.file_limit, cost))
            {
                self.seal_inner(Some(time_us))?;
            }
        }
        if self.active.is_none() {
            self.open_part(&day, time_us, sequence)?;
        }
        let mut stored_message = None;
        if self
            .active
            .as_ref()
            .is_some_and(|part| would_exceed_file_limit(part, self.file_limit, cost))
        {
            let fragment_day = self.active.as_ref().unwrap().descriptor.day.clone();
            let directory = self
                .active
                .as_ref()
                .and_then(|part| part.path.parent())
                .ok_or_else(|| failure("PATH", "replay part has no directory"))?;
            let reference = crate::execution_replay_fragments::write(
                directory,
                &self.recording_id,
                sequence,
                message_json.as_bytes(),
                self.file_limit,
            )?;
            cost = estimated_message_size(reference.len(), topic.len());
            if self
                .active
                .as_ref()
                .is_some_and(|part| would_exceed_file_limit(part, self.file_limit, cost))
            {
                return Err(failure(
                    "BUDGET",
                    "fragment reference cannot fit in a replay file",
                ));
            }
            let reference_value: Value = serde_json::from_slice(&reference).map_err(io_error)?;
            self.push_fact(
                "fragment_ref",
                sequence.to_string(),
                sequence,
                Some(json!({"day":fragment_day,"reference":reference_value})),
            )?;
            stored_message = Some(reference);
        }
        for fact in staged {
            let value = fact
                .value
                .ok_or_else(|| failure("STATE", "capture association cannot be empty"))?;
            self.save_capture_fact(fact.kind, fact.key, fact.seq, value)?;
        }
        // A failed append stops this recording; its unpublished tail is never a checkpoint.
        self.hydrate_market_for_message(&message)?;
        self.projection.apply(&message)?;
        self.index_own_message(&message, sequence)?;
        self.index_market_message(&message, sequence)?;
        let mut active = self
            .active
            .take()
            .ok_or_else(|| failure("STATE", "missing active part"))?;
        let write_result = write_payload(
            &mut active,
            &message["topic"].as_str().unwrap_or_default().to_owned(),
            sequence,
            time_us,
            publish_time(&message, time_us),
            stored_message.as_deref().unwrap_or(message_json.as_bytes()),
        );
        active.descriptor.last_seq = sequence;
        active.descriptor.end_us = time_us
            .checked_add(1)
            .ok_or_else(|| failure("BUDGET", "time bound overflow"))?;
        active.pending_bound = active.pending_bound.saturating_add(cost);
        active.pending_messages += 1;
        active.pending_first.get_or_insert((sequence, time_us));
        self.active = Some(active);
        write_result?;
        self.last_seq = sequence;
        self.last_time_us = Some(time_us);
        self.checkpoint_bytes = self
            .checkpoint_bytes
            .saturating_add(message_json.len() as u64);
        let mut members = BTreeSet::new();
        for name in ["execution_id", "associated_execution_id"] {
            if let Some(identity) = message["data"].get(name).and_then(identity_string) {
                members.insert(identity);
            }
        }
        for identity in members {
            if self.executions.insert(identity.clone()) {
                self.index
                    .register_execution(&self.recording_id, &identity)?;
            }
        }
        if collect_observed_symbols(&message["data"], &mut self.symbols)? {
            self.publish_catalog(None)?;
        }
        if should_seal(&message)
            || self.needs_checkpoint()
            || self.pending_facts.len() >= MARKET_FACTS_PER_BLOCK
            || self.pending_fact_bytes >= CHECKPOINT_BYTES
        {
            self.commit_active()?;
        }
        Ok(())
    }

    fn open_part(&mut self, day: &str, time_us: u64, sequence: u64) -> ReplayResult<()> {
        let directory = day_directory(&self.account, day, true)?;
        self.part_number = self
            .part_number
            .checked_add(1)
            .ok_or_else(|| failure("BUDGET", "part identity overflow"))?;
        let descriptor = PartDescriptor {
            recording_id: self.recording_id.clone(),
            part: self.part_number,
            day: day.into(),
            sha256: String::new(),
            size_bytes: 0,
            first_seq: sequence,
            last_seq: self.last_seq,
            start_us: time_us,
            end_us: time_us,
            checkpoint_through_seq: self.last_seq,
            checkpoint_time_us: self.last_time_us,
            executions: BTreeSet::new(),
            symbols: BTreeSet::new(),
            committed_prefix: false,
        };
        let path = child_file(&directory, &format!("{}.partial", descriptor.file_name()))?;
        self.index
            .register_file(&self.recording_id, self.part_number, day)?;
        let file = OpenOptions::new()
            .write(true)
            .read(true)
            .create_new(true)
            .open(&path)
            .map_err(io_error)?;
        let sync_file = file.try_clone().map_err(io_error)?;
        let options = mcap::WriteOptions::new()
            .compression(Some(mcap::Compression::Zstd))
            .compression_threads(0)
            .chunk_size(Some(1024 * 1024))
            .emit_message_indexes(false)
            .profile("axile.execution-replay.v1")
            .library("axile-storage");
        let mut writer = options
            .create(CappedFile {
                file,
                limit: self.file_limit,
            })
            .map_err(io_error)?;
        let schema = writer
            .add_schema("AxileExecutionReplay.v1", "jsonschema", SCHEMA.as_bytes())
            .map_err(io_error)?;
        let initial_projection = self.projection.snapshot()?;
        let projection_hash = digest(&serde_json::to_vec(&initial_projection).map_err(io_error)?);
        let checkpoint = json!({"schema_version":1,"account_id":self.account_id,"recording_id":self.recording_id,"context":self.context,
            "projection":{"recording_id":self.recording_id,"state_through_seq":self.last_seq,
                "time_us":self.last_time_us,"sha256":projection_hash}});
        let bytes = serde_json::to_vec(&checkpoint).map_err(io_error)?;
        if bytes.len() > CHECKPOINT_LIMIT {
            return Err(failure("BUDGET", "checkpoint exceeds message budget"));
        }
        let mut part = ActivePart {
            writer,
            sync_file,
            path,
            descriptor,
            channels: BTreeMap::new(),
            schema,
            pending_first: None,
            last_commit: Instant::now(),
            last_watermark: Instant::now(),
            committed_schema: None,
            committed_channels: BTreeMap::new(),
            pending_bound: estimated_message_size(bytes.len() + SCHEMA.len(), "/snapshot".len()),
            pending_messages: 1,
            summary_chunks: 0,
            initial_projection: Some(initial_projection),
        };
        write_payload(
            &mut part,
            "/snapshot",
            self.last_seq,
            time_us,
            time_us,
            &bytes,
        )?;
        self.active = Some(part);
        self.last_checkpoint_seq = self.last_seq;
        self.checkpoint_bytes = 0;
        self.metadata_day = Some(day.into());
        self.publish_catalog(None)
    }

    fn publish_catalog(&self, sealed: Option<&PartDescriptor>) -> ReplayResult<()> {
        if sealed.is_none() && self.metadata_day.is_none() {
            return Ok(());
        }
        self.index.set_metadata(
            &self.recording_id,
            &CatalogRecording {
                context: self.context.clone(),
                previous_recording_id: self.previous_recording_id.clone(),
                executions: BTreeSet::new(),
                symbols: self.symbols.clone(),
                state: if self.error.is_some() {
                    "error"
                } else if self.closed {
                    "closed"
                } else {
                    "recording"
                }
                .into(),
                unsealed_start_us: self.active.as_ref().map(|part| part.descriptor.start_us),
                error: self.error.clone(),
            },
        )
    }

    fn needs_checkpoint(&self) -> bool {
        self.last_seq.saturating_sub(self.last_checkpoint_seq) >= CHECKPOINT_EVENTS
            || self.checkpoint_bytes >= CHECKPOINT_BYTES
    }

    fn capture_fact(&mut self, kind: &'static str, key: &str) -> ReplayResult<Value> {
        let cache_key = (kind.to_owned(), key.to_owned());
        if let Some(value) = self.capture_cache.get(&cache_key) {
            return Ok(value.clone());
        }
        let value = self
            .index
            .fact_at(&self.recording_id, kind, key, self.last_seq)?
            .and_then(|(_, value)| value)
            .unwrap_or_else(|| json!({}));
        if !value.is_object() {
            return Err(failure("CORRUPT", "invalid capture identity fact"));
        }
        self.capture_cache.insert(cache_key, value.clone());
        Ok(value)
    }

    fn save_capture_fact(
        &mut self,
        kind: &'static str,
        key: String,
        seq: u64,
        value: Value,
    ) -> ReplayResult<()> {
        self.capture_cache
            .insert((kind.to_owned(), key.clone()), value.clone());
        self.push_fact(kind, key, seq, Some(value))
    }

    fn enrich_associations(&mut self, message: &mut Value, seq: u64) -> ReplayResult<()> {
        let topic = message["topic"].as_str().unwrap_or_default().to_owned();
        let mut data = message["data"]
            .as_object()
            .cloned()
            .ok_or_else(|| failure("SCHEMA", "event data must be an object"))?;
        if let Some(command) = data
            .get("command_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
        {
            let mut fact = self.capture_fact("capture_command", &command)?;
            for field in ["execution_id", "origin"] {
                if data
                    .get(field)
                    .and_then(Value::as_str)
                    .is_none_or(|value| value.is_empty() || value == "unknown")
                {
                    if let Some(value) = fact.get(field) {
                        data.insert(field.into(), value.clone());
                    }
                }
            }
            if topic == "/command" {
                let mut changed = false;
                for field in ["execution_id", "origin"] {
                    if let Some(value) = data
                        .get(field)
                        .and_then(Value::as_str)
                        .filter(|value| !value.is_empty() && *value != "unknown")
                    {
                        if fact
                            .get(field)
                            .and_then(Value::as_str)
                            .is_some_and(|old| old != value)
                        {
                            return Err(failure("IDENTITY", "conflicting command ownership"));
                        }
                        if fact.get(field).and_then(Value::as_str) != Some(value) {
                            fact[field] = json!(value);
                            changed = true;
                        }
                    }
                }
                if changed {
                    self.save_capture_fact("capture_command", command, seq, fact)?;
                }
            }
        }
        if topic != "/command"
            || data.get("operation").and_then(Value::as_str) == Some("place_order")
        {
            if let (Some(symbol), Some(order)) = (
                data.get("symbol").and_then(Value::as_str),
                data.get("order_id").and_then(Value::as_str),
            ) {
                if !symbol.is_empty() && !order.is_empty() {
                    let key = serde_json::to_string(&[symbol, order]).map_err(io_error)?;
                    let mut fact = self.capture_fact("capture_order", &key)?;
                    let mut changed = false;
                    for field in ["execution_id", "origin"] {
                        let supplied = data.get(field).and_then(Value::as_str).filter(|value| {
                            !value.is_empty() && (field != "origin" || *value != "unknown")
                        });
                        if let Some(value) = supplied {
                            if fact.get(field).and_then(Value::as_str) != Some(value) {
                                fact[field] = json!(value);
                                changed = true;
                            }
                        } else if let Some(value) = fact.get(field) {
                            data.insert(field.into(), value.clone());
                        }
                    }
                    if changed {
                        self.save_capture_fact("capture_order", key, seq, fact)?;
                    }
                }
            }
        }
        message["data"] = Value::Object(data);
        Ok(())
    }

    fn hydrate_market_for_message(&mut self, message: &Value) -> ReplayResult<()> {
        if !message["topic"]
            .as_str()
            .is_some_and(|topic| topic.starts_with("/market_trade/"))
        {
            return Ok(());
        }
        let data = &message["data"];
        if let Some(key) = stable_market_key(data) {
            if self.market_loaded.insert(key.to_owned()) {
                if let Some((_, Some(value))) =
                    self.index
                        .fact_at(&self.recording_id, "market_trade", key, self.last_seq)?
                {
                    self.projection.hydrate_market_fact(key, &value)?;
                }
            }
            if let Some(seq) = data["related_capture_seq"].as_u64() {
                if self.market_observations_loaded.insert(seq) {
                    if let Some((_, Some(value))) = self.index.fact_at(
                        &self.recording_id,
                        "market_observation",
                        &seq.to_string(),
                        self.last_seq,
                    )? {
                        self.projection.hydrate_market_observation(seq, &value)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn push_fact(
        &mut self,
        kind: &'static str,
        key: String,
        seq: u64,
        value: Option<Value>,
    ) -> ReplayResult<()> {
        let bytes = value
            .as_ref()
            .map(serde_json::to_vec)
            .transpose()
            .map_err(io_error)?
            .map_or(0, |bytes| bytes.len());
        self.pending_fact_bytes = self.pending_fact_bytes.saturating_add(bytes as u64);
        self.pending_facts.push(PendingFact {
            kind,
            key,
            seq,
            value,
        });
        Ok(())
    }

    fn index_own_message(&mut self, message: &Value, seq: u64) -> ReplayResult<()> {
        match message["topic"].as_str() {
            Some("/trade") => {
                if let Some(key) = stable_market_key(&message["data"]) {
                    self.push_fact(
                        "own_trade",
                        key.to_owned(),
                        seq,
                        self.projection.own_fact(key),
                    )?;
                } else {
                    self.push_fact(
                        "own_observation",
                        seq.to_string(),
                        seq,
                        self.projection.own_observation(seq),
                    )?;
                }
            }
            Some("/command") => {
                for (key, value) in self.projection.own_associations(seq) {
                    self.push_fact("own_trade", key, seq, Some(value))?;
                }
                for (key, value) in self.projection.own_observation_associations(seq) {
                    self.push_fact("own_observation", key, seq, Some(value))?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn index_market_message(&mut self, message: &Value, seq: u64) -> ReplayResult<()> {
        if !message["topic"]
            .as_str()
            .is_some_and(|topic| topic.starts_with("/market_trade/"))
        {
            return Ok(());
        }
        let data = &message["data"];
        if let Some(key) = stable_market_key(data) {
            self.push_fact(
                "market_ref",
                seq.to_string(),
                seq,
                Some(
                    json!({"time_us":message["time_us"], "symbol":data["symbol"],
                    "trade_key":key, "related_capture_seq":data["related_capture_seq"]}),
                ),
            )?;
            self.push_fact(
                "market_trade",
                key.to_owned(),
                seq,
                self.projection.market_fact(key),
            )?;
            if let Some(related) = data["related_capture_seq"].as_u64() {
                self.push_fact(
                    "market_observation",
                    related.to_string(),
                    seq,
                    self.projection.market_observation(related),
                )?;
            }
        } else {
            self.market_observations_loaded.insert(seq);
            self.push_fact(
                "market_observation",
                seq.to_string(),
                seq,
                self.projection.market_observation(seq),
            )?;
        }
        Ok(())
    }

    fn commit_active(&mut self) -> ReplayResult<()> {
        let checkpoint_after = self.needs_checkpoint();
        let Some(part) = self.active.as_mut() else {
            return Ok(());
        };
        let Some((first_seq, first_us)) = part.pending_first else {
            return Ok(());
        };
        part.writer.flush().map_err(io_error)?;
        let mut schema = part.committed_schema;
        let mut channels = part.committed_channels.clone();
        self.projection.clear_market_history();
        let projection_snapshot = if checkpoint_after {
            Some(self.projection.snapshot()?)
        } else {
            None
        };
        let mut checkpoints = Vec::new();
        if let Some(state) = &part.initial_projection {
            checkpoints.push(IndexedCheckpoint {
                part: part.descriptor.part,
                seq: part.descriptor.checkpoint_through_seq,
                time_us: part.descriptor.checkpoint_time_us,
                state: state.clone(),
                schema: None,
                channels: BTreeMap::new(),
            });
        }
        if checkpoint_after {
            checkpoints.push(IndexedCheckpoint {
                part: part.descriptor.part,
                seq: part.descriptor.last_seq,
                time_us: Some(part.descriptor.end_us - 1),
                state: projection_snapshot.unwrap(),
                schema: Some(part.schema),
                channels: part
                    .channels
                    .iter()
                    .map(|(topic, id)| (*id, topic.clone()))
                    .collect(),
            });
        }
        let facts: Vec<_> = self
            .pending_facts
            .iter()
            .map(|fact| FactVersion {
                kind: fact.kind,
                key: &fact.key,
                seq: fact.seq,
                value: fact.value.as_ref(),
            })
            .collect();
        let (_, chunks) = self.index.publish(
            &self.recording_id,
            part.descriptor.part,
            &part.path,
            &part.sync_file,
            first_seq,
            part.descriptor.last_seq,
            first_us,
            part.descriptor.end_us - 1,
            &facts,
            &checkpoints,
            |records| {
                let (snapshot, _) = validate_committed_records(
                    records,
                    &part.descriptor,
                    self.account_id,
                    &self.context,
                    first_seq,
                    part.descriptor.last_seq,
                    first_us,
                    part.descriptor.end_us - 1,
                    &mut schema,
                    &mut channels,
                    Some((part.schema, &part.channels)),
                )?;
                if let Some(state) = &part.initial_projection {
                    let expected = digest(&serde_json::to_vec(state).map_err(io_error)?);
                    if snapshot
                        .as_ref()
                        .and_then(|value| value["projection"]["sha256"].as_str())
                        != Some(expected.as_str())
                    {
                        return Err(failure(
                            "CORRUPT",
                            "replay checkpoint reference does not match its state",
                        ));
                    }
                }
                Ok(())
            },
        )?;
        part.committed_schema = schema;
        part.committed_channels = channels;
        self.pending_facts.clear();
        self.pending_fact_bytes = 0;
        self.market_loaded.clear();
        self.market_observations_loaded.clear();
        self.capture_cache.clear();
        part.initial_projection = None;
        part.pending_first = None;
        part.pending_bound = 0;
        part.pending_messages = 0;
        part.summary_chunks = part.summary_chunks.saturating_add(chunks);
        if checkpoint_after {
            self.last_checkpoint_seq = part.descriptor.last_seq;
            self.checkpoint_bytes = 0;
        }
        part.last_commit = Instant::now();
        Ok(())
    }

    fn seal_inner(&mut self, end_us: Option<u64>) -> ReplayResult<()> {
        self.commit_active()?;
        self.drain_seals(false)?;
        if self.pending_seals.len() >= 2 {
            self.drain_seals(true)?;
        }
        let Some(mut part) = self.active.take() else {
            return Ok(());
        };
        if part.descriptor.last_seq < part.descriptor.first_seq {
            return Err(failure("STATE", "cannot publish an empty part"));
        }
        if let Some(end) = self.index.watermark(
            &self.recording_id,
            part.descriptor.part,
            part.descriptor.last_seq,
        )? {
            part.descriptor.end_us = part.descriptor.end_us.max(end);
        }
        if let Some(end) = end_us {
            part.descriptor.end_us = part.descriptor.end_us.max(end);
        }
        let recovery = RecoveryPartMeta {
            schema_version: 1,
            account_id: self.account_id,
            previous_recording_id: self.previous_recording_id.clone(),
            descriptor: part.descriptor.clone(),
        };
        let recovery_json = serde_json::to_string(&recovery).map_err(io_error)?;
        recovery_metadata_size(recovery_json.len())?;
        part.writer
            .write_metadata(&mcap::records::Metadata {
                name: RECOVERY_METADATA_NAME.into(),
                metadata: BTreeMap::from([("json".into(), recovery_json)]),
            })
            .map_err(io_error)?;
        part.writer.finish().map_err(io_error)?;
        part.sync_file.sync_all().map_err(io_error)?;
        let expected = ExpectedSummary {
            count: part.descriptor.last_seq - part.descriptor.first_seq + 2,
            chunks: part.summary_chunks,
            schema: part.schema,
            channels: part.channels.clone(),
        };
        drop(part.writer.into_inner());
        let hash_path = part.path.clone();
        let limit = self.file_limit;
        let hash = thread::Builder::new()
            .name("replay-hash".into())
            .spawn(move || {
                validate_finished_summary(&hash_path, &expected)?;
                digest_file(&hash_path, limit)
            })
            .map_err(io_error)?;
        self.pending_seals.push(PendingSeal {
            descriptor: part.descriptor,
            path: part.path,
            hash,
        });
        Ok(())
    }

    fn drain_seals(&mut self, wait: bool) -> ReplayResult<()> {
        while self
            .pending_seals
            .first()
            .is_some_and(|part| wait || part.hash.is_finished())
        {
            let pending = self.pending_seals.remove(0);
            let (sha256, size_bytes) = pending
                .hash
                .join()
                .map_err(|_| failure("IO", "replay file hashing failed"))??;
            let mut descriptor = pending.descriptor;
            descriptor.sha256 = sha256;
            descriptor.size_bytes = size_bytes;
            self.index.seal_file(
                &descriptor,
                &self.context,
                self.previous_recording_id.as_deref(),
            )?;
            let directory = pending
                .path
                .parent()
                .ok_or_else(|| failure("PATH", "part has no parent"))?;
            let sealed = child_file(directory, &descriptor.file_name())?;
            if sealed.exists() {
                return Err(failure("IDENTITY", "sealed part already exists"));
            }
            fs::rename(&pending.path, &sealed).map_err(io_error)?;
            sync_directory(directory)?;
            self.publish_catalog(Some(&descriptor))?;
            self.sealed.push(descriptor);
        }
        Ok(())
    }

    pub(crate) fn seal(&mut self) -> ReplayResult<()> {
        self.ensure_open()?;
        let result = self.commit_active();
        if let Err(error) = &result {
            self.error = Some(error.to_string());
        }
        result
    }

    #[cfg(test)]
    pub(crate) fn rotate_for_test(&mut self, time_us: u64) -> ReplayResult<()> {
        self.seal_inner(Some(time_us))?;
        self.drain_seals(true)
    }

    pub(crate) fn poll(&mut self, now_us: u64) -> ReplayResult<()> {
        self.ensure_open()?;
        checked_us(now_us)?;
        if let Err(error) = self.drain_seals(false) {
            self.error = Some(error.to_string());
            return Err(error);
        }
        if self.last_time_us.is_some_and(|previous| now_us < previous)
            || self.last_poll_us.is_some_and(|previous| now_us < previous)
        {
            self.error = Some("local observation clock moved backwards".into());
            return Err(failure(
                "CLOCK_ROLLBACK",
                "local observation clock moved backwards",
            ));
        }
        self.last_poll_us = Some(now_us);
        if self.active.as_ref().is_some_and(|part| {
            part.pending_first.is_some() && part.last_commit.elapsed().as_secs() >= 1
        }) {
            let result = self.commit_active();
            if let Err(error) = &result {
                self.error = Some(error.to_string());
            }
            result?;
        }
        if self
            .active
            .as_ref()
            .is_some_and(|part| part.last_watermark.elapsed().as_secs() >= 1)
        {
            if let Err(error) = self
                .commit_active()
                .and_then(|()| self.publish_watermark(now_us))
            {
                self.error = Some(error.to_string());
                return Err(error);
            }
        }
        Ok(())
    }

    fn publish_watermark(&mut self, now_us: u64) -> ReplayResult<()> {
        let Some(part) = self.active.as_mut() else {
            return Ok(());
        };
        if part.descriptor.last_seq < part.descriptor.first_seq {
            return Ok(());
        }
        let end = now_us.max(part.descriptor.end_us);
        self.index.record_watermark(
            &self.recording_id,
            part.descriptor.part,
            part.descriptor.last_seq,
            end,
        )?;
        part.last_watermark = Instant::now();
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> ReplayResult<()> {
        if self.closed {
            return Ok(());
        }
        self.ensure_open()?;
        let result = self.seal_inner(None).and_then(|()| self.drain_seals(true));
        if let Err(error) = &result {
            self.error = Some(error.to_string());
            return result;
        }
        self.closed = true;
        self.publish_catalog(None)?;
        self.lease.take();
        Ok(())
    }

    pub(crate) fn status(&self) -> Value {
        json!({"account_id":self.account_id,"recording_id":self.recording_id,"state":if self.error.is_some() {"error"} else if self.closed {"closed"} else {"recording"},
            "state_through_seq":self.last_seq,"time_us":self.last_time_us,"sealed_parts":self.sealed.len(),"unsealed":self.active.is_some(),"error":self.error,
            "coverage":self.sealed.iter().map(|part| json!({"start_us":part.start_us,"end_us":part.end_us})).collect::<Vec<_>>()})
    }
}

fn shanghai_day(time_us: u64) -> ReplayResult<String> {
    let time = DateTime::<Utc>::from_timestamp_micros(i64::try_from(time_us).map_err(io_error)?)
        .ok_or_else(|| {
            failure(
                "INVALID_REQUEST",
                "observation time is outside supported dates",
            )
        })?;
    Ok(time.with_timezone(&Shanghai).format("%Y-%m-%d").to_string())
}

fn publish_time(message: &Value, default: u64) -> u64 {
    message["data"]
        .get("publish_time_us")
        .and_then(Value::as_u64)
        .unwrap_or(default)
}

pub(crate) fn stable_market_key(data: &Value) -> Option<&str> {
    (data["identity_quality"] == "stable")
        .then(|| data["trade_key"].as_str())
        .flatten()
        .filter(|key| !key.is_empty())
}

fn recovery_metadata_size(json_bytes: usize) -> ReplayResult<usize> {
    // MCAP body: name length, map length, key/value lengths, and the "json" key.
    let body_bytes = json_bytes
        .checked_add(20 + RECOVERY_METADATA_NAME.len())
        .filter(|size| *size <= RECOVERY_METADATA_LIMIT)
        .ok_or_else(|| failure("BUDGET", "part recovery metadata exceeds budget"))?;
    Ok(body_bytes)
}

pub(crate) fn identity_string(value: &Value) -> Option<String> {
    value
        .as_str()
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .or_else(|| value.as_u64().map(|value| value.to_string()))
}

pub(crate) fn validate_symbols(symbols: &BTreeSet<String>) -> ReplayResult<()> {
    if symbols.iter().any(|symbol| {
        symbol.is_empty() || symbol.len() > 256 || symbol.chars().any(char::is_control)
    }) {
        return Err(failure("CORRUPT", "invalid recording symbol directory"));
    }
    Ok(())
}

/// Names are directory metadata only; prices and states still obey the replay cursor.
pub(crate) fn collect_observed_symbols(
    data: &Value,
    symbols: &mut BTreeSet<String>,
) -> ReplayResult<bool> {
    let previous = symbols.len();
    if let Some(symbol) = data.get("symbol").and_then(Value::as_str) {
        if !symbol.is_empty() && symbol.len() <= 256 && !symbol.chars().any(char::is_control) {
            symbols.insert(symbol.into());
        }
    }
    let scope_symbols = data.get("scope").and_then(|scope| {
        if scope.is_array() {
            Some(scope)
        } else {
            scope.get("symbols")
        }
    });
    for values in [data.get("symbols"), scope_symbols]
        .into_iter()
        .flatten()
        .filter_map(Value::as_array)
    {
        for symbol in values.iter().filter_map(Value::as_str) {
            if !symbol.is_empty() && symbol.len() <= 256 && !symbol.chars().any(char::is_control) {
                symbols.insert(symbol.into());
            }
        }
    }
    for name in ["orders", "positions"] {
        if let Some(rows) = data.get(name).and_then(Value::as_array) {
            for row in rows {
                collect_observed_symbols(row, symbols)?;
            }
        }
    }
    validate_symbols(symbols)?;
    Ok(symbols.len() != previous)
}

fn write_payload(
    part: &mut ActivePart,
    topic: &str,
    sequence: u64,
    time_us: u64,
    publish_us: u64,
    bytes: &[u8],
) -> ReplayResult<()> {
    let channel = if let Some(channel) = part.channels.get(topic) {
        *channel
    } else {
        let channel = part
            .writer
            .add_channel(part.schema, topic, "json", &BTreeMap::new())
            .map_err(io_error)?;
        part.channels.insert(topic.into(), channel);
        channel
    };
    let header = MessageHeader {
        channel_id: channel,
        sequence: (sequence % (u32::MAX as u64 + 1)) as u32,
        log_time: checked_us(time_us)?,
        publish_time: checked_us(publish_us)?,
    };
    part.writer
        .write_to_known_channel(&header, bytes)
        .map_err(io_error)
}

struct CommitValidator<'a> {
    part: &'a PartDescriptor,
    expected: Option<(u16, &'a BTreeMap<String, u16>)>,
    context: &'a Value,
    account_id: u64,
    first_us: u64,
    next_seq: u64,
    last_us: Option<u64>,
    header_seen: bool,
    snapshot_seen: bool,
    schema: &'a mut Option<u16>,
    channels: &'a mut BTreeMap<u16, String>,
    snapshot: Option<Value>,
    messages: Vec<Value>,
}

impl CommitValidator<'_> {
    fn record(&mut self, record: &BlockRecord) -> ReplayResult<()> {
        match record {
            BlockRecord::Header(header) => {
                if self.header_seen
                    || header.profile != "axile.execution-replay.v1"
                    || header.library != "axile-storage"
                {
                    return Err(failure("SCHEMA", "invalid replay file header"));
                }
                self.header_seen = true;
            }
            BlockRecord::Schema(header, bytes) => {
                if self.schema.is_some()
                    || !self.header_seen
                    || self.expected.is_some_and(|(id, _)| header.id != id)
                    || header.name != "AxileExecutionReplay.v1"
                    || header.encoding != "jsonschema"
                    || bytes != SCHEMA.as_bytes()
                {
                    return Err(failure("SCHEMA", "invalid replay schema"));
                }
                *self.schema = Some(header.id);
            }
            BlockRecord::Channel(channel) => {
                if *self.schema != Some(channel.schema_id)
                    || channel.message_encoding != "json"
                    || !channel.metadata.is_empty()
                    || self.expected.is_some_and(|(_, channels)| {
                        channels.get(&channel.topic) != Some(&channel.id)
                    })
                    || self
                        .channels
                        .insert(channel.id, channel.topic.clone())
                        .is_some()
                {
                    return Err(failure("SCHEMA", "invalid replay channel"));
                }
            }
            BlockRecord::Message(message) => self.message(message)?,
        }
        Ok(())
    }

    fn message(
        &mut self,
        message: &crate::execution_replay_block::BlockMessage,
    ) -> ReplayResult<()> {
        let topic = self
            .channels
            .get(&message.header.channel_id)
            .ok_or_else(|| failure("CORRUPT", "message uses an undeclared replay channel"))?;
        let value: Value = serde_json::from_slice(&message.data)
            .map_err(|error| failure("CORRUPT", error.to_string()))?;
        if topic == "/snapshot" {
            return self.snapshot(message, &value);
        }
        if !self.snapshot_seen || value["topic"] != *topic {
            return Err(failure("CORRUPT", "invalid replay observation"));
        }
        validate_message(&value, self.context)?;
        let time = unsigned(&value, "time_us")?;
        let seq = unsigned(&value, "capture_seq")?;
        if value["recording_id"] != self.part.recording_id
            || seq != self.next_seq
            || self.last_us.is_some_and(|previous| time < previous)
            || self.last_us.is_none() && time != self.first_us
            || message.header.sequence != (seq % (u32::MAX as u64 + 1)) as u32
            || message.header.log_time != checked_us(time)?
            || message.header.publish_time != checked_us(publish_time(&value, time))?
        {
            return Err(failure(
                "CORRUPT",
                "replay block observation sequence or time is invalid",
            ));
        }
        self.next_seq = seq
            .checked_add(1)
            .ok_or_else(|| failure("BUDGET", "sequence overflow"))?;
        self.last_us = Some(time);
        self.messages.push(value);
        Ok(())
    }

    fn snapshot(
        &mut self,
        message: &crate::execution_replay_block::BlockMessage,
        value: &Value,
    ) -> ReplayResult<()> {
        let part = &self.part;
        if self.snapshot_seen
            || value["schema_version"] != 1
            || value["account_id"] != self.account_id
            || value["recording_id"] != part.recording_id
            || value["context"] != *self.context
            || value["projection"]["recording_id"] != part.recording_id
            || value["projection"]["state_through_seq"] != part.checkpoint_through_seq
            || value["projection"]["time_us"].as_u64() != part.checkpoint_time_us
            || value["projection"]["sha256"].as_str().is_none_or(|hash| {
                hash.len() != 64 || !hash.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
            || message.header.sequence
                != (part.checkpoint_through_seq % (u32::MAX as u64 + 1)) as u32
            || message.header.log_time != checked_us(self.first_us)?
            || message.header.publish_time != checked_us(self.first_us)?
        {
            return Err(failure("CORRUPT", "invalid replay checkpoint"));
        }
        self.snapshot_seen = true;
        self.snapshot = Some(value.clone());
        Ok(())
    }
}

pub(crate) fn validate_committed_records(
    records: &[BlockRecord],
    part: &PartDescriptor,
    account_id: u64,
    context: &Value,
    first_seq: u64,
    last_seq: u64,
    first_us: u64,
    last_us: u64,
    schema: &mut Option<u16>,
    channels: &mut BTreeMap<u16, String>,
    expected: Option<(u16, &BTreeMap<String, u16>)>,
) -> ReplayResult<(Option<Value>, Vec<Value>)> {
    let initial = first_seq == part.first_seq;
    let mut validator = CommitValidator {
        part,
        expected,
        context,
        account_id,
        first_us,
        next_seq: first_seq,
        last_us: None,
        header_seen: !initial,
        snapshot_seen: !initial,
        schema,
        channels,
        snapshot: None,
        messages: Vec::new(),
    };
    for record in records {
        validator.record(record)?;
    }
    if !validator.header_seen
        || !validator.snapshot_seen
        || validator.schema.is_none()
        || validator.next_seq != last_seq + 1
        || validator.last_us != Some(last_us)
    {
        return Err(failure(
            "CORRUPT",
            "replay block does not match its committed bounds",
        ));
    }
    Ok((validator.snapshot, validator.messages))
}

fn should_seal(message: &Value) -> bool {
    let data = &message["data"];
    message["topic"] == "/execution"
        && [
            "finished",
            "end",
            "ended",
            "completed",
            "terminated",
            "failed",
            "canceled",
            "cancelled",
        ]
        .contains(
            &data
                .get("phase")
                .or_else(|| data.get("kind"))
                .and_then(Value::as_str)
                .unwrap_or(""),
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let directory = std::env::temp_dir().join(format!(
                "axile-replay-writer-{}-{}",
                std::process::id(),
                TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&directory).unwrap();
            Self(directory)
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn context() -> String {
        json!({"schema_version":1,"channel":"external-neutral","instrument":{"X":{"quantity_unit":"lot","tick_size":1}},"streams":{},"attribute_fields":{},"native_identity_fields":{}}).to_string()
    }

    fn event(sequence: u64, time: u64, topic: &str, data: Value) -> String {
        json!({"schema_version":1,"recording_id":"recA","capture_seq":sequence,"time_us":time,"topic":topic,"data":data}).to_string()
    }

    #[test]
    fn active_writer_publishes_only_flushed_complete_blocks() {
        use crate::execution_replay_block::read_block;

        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        let active_path = writer.active.as_ref().unwrap().path.clone();
        let index_path = writer.account.join(".replay-index.sqlite");
        let reader = BlockIndex::open(&index_path).unwrap();
        assert!(reader.blocks("recA", JS_SAFE, None, 8).unwrap().is_empty());
        writer.commit_active().unwrap();
        let first = reader.blocks("recA", JS_SAFE, None, 8).unwrap();
        assert_eq!(first.len(), 1);
        let first_records = read_block(&active_path, &first[0].range).unwrap();
        let first_messages: Vec<_> = first_records
            .iter()
            .filter_map(|record| match record {
                BlockRecord::Message(message) => Some(message),
                _ => None,
            })
            .collect();
        assert_eq!(first_messages.len(), 2); // initial checkpoint and first event
        assert_eq!(
            serde_json::from_slice::<Value>(&first_messages[1].data).unwrap()["capture_seq"],
            1
        );
        writer
            .append(&event(
                2,
                time,
                "/order",
                json!({"symbol":"X","order_id":"A"}),
            ))
            .unwrap();
        assert_eq!(reader.blocks("recA", JS_SAFE, None, 8).unwrap(), first);
        writer.commit_active().unwrap();
        let both = reader.blocks("recA", JS_SAFE, None, 8).unwrap();
        assert_eq!(both.len(), 2);
        assert_eq!(reader.blocks("recA", 1, None, 8).unwrap(), first);
        let second_records = read_block(&active_path, &both[1].range).unwrap();
        let second_message = second_records
            .iter()
            .find_map(|record| match record {
                BlockRecord::Message(message) => Some(message),
                _ => None,
            })
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&second_message.data).unwrap()["capture_seq"],
            2
        );
        writer.finish().unwrap();
        let sealed_path = active_path.with_extension("");
        assert!(read_block(&sealed_path, &both[1].range)
            .unwrap()
            .iter()
            .any(|record| matches!(record, BlockRecord::Message(_))));
    }

    #[test]
    fn execution_end_and_midnight_commit_without_opening_another_file() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E1","phase":"started"}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time + 61_000_000,
                "/execution",
                json!({"execution_id":"E1","phase":"end"}),
            ))
            .unwrap();
        let path = writer.active.as_ref().unwrap().path.clone();
        assert_eq!(writer.part_number, 1);
        writer.seal().unwrap();
        writer.poll(time + 86_400_000_000).unwrap();
        writer
            .append(&event(
                3,
                time + 86_400_000_000,
                "/execution",
                json!({"execution_id":"E2","phase":"started"}),
            ))
            .unwrap();
        assert_eq!(writer.active.as_ref().unwrap().path, path);
        assert_eq!(writer.part_number, 1);
        writer.finish().unwrap();
        assert_eq!(writer.sealed.len(), 1);
    }

    #[test]
    fn replay_reads_committed_prefix_while_file_is_still_open() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time,
                "/order",
                json!({"symbol":"X","order_id":"A"}),
            ))
            .unwrap();
        writer.commit_active().unwrap();
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert_eq!(
            manifest["recordings"][0]["coverage"],
            json!([{"start_us":time,"end_us":time+1}])
        );
        let window = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+1}).to_string(),
        )
        .unwrap();
        assert_eq!(window["state_before_start"]["state_through_seq"], 0);
        assert_eq!(
            window["messages"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item["capture_seq"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![1, 2]
        );
        assert!(writer.active.is_some());
    }

    #[test]
    fn quiet_committed_coverage_survives_sealing_and_excludes_later_events() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer.active.as_mut().unwrap().last_watermark =
            Instant::now() - std::time::Duration::from_secs(2);
        writer.poll(time + 5_000_000).unwrap();
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert_eq!(
            manifest["recordings"][0]["coverage"],
            json!([{"start_us":time,"end_us":time+5_000_000}])
        );
        let request =
            json!({"recording_id":"recA","start_us":time+1,"end_us":time+5_000_000}).to_string();
        let before =
            crate::execution_replay_reader::window(&directory.0, 7, "E", &request).unwrap();
        assert_eq!(before["messages"], json!([]));
        assert_eq!(before["state_before_start"]["state_through_seq"], 1);
        writer
            .append(&event(
                2,
                time + 5_000_000,
                "/order",
                json!({"symbol":"X","order_id":"A"}),
            ))
            .unwrap();
        writer.finish().unwrap();
        let after = crate::execution_replay_reader::window(&directory.0, 7, "E", &request).unwrap();
        assert_eq!(after["coverage"], before["coverage"]);
        assert_eq!(after["messages"], json!([]));
        assert_eq!(after["state_before_start"], before["state_before_start"]);
    }

    #[test]
    fn poll_rejects_later_events_before_its_time_barrier() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started"}),
            ))
            .unwrap();
        writer.poll(time + 10).unwrap();
        let error = writer
            .append(&event(
                2,
                time + 9,
                "/order",
                json!({"symbol":"X","order_id":"A"}),
            ))
            .unwrap_err();
        assert_eq!(error.code, "REPLAY_CLOCK_ROLLBACK");
    }

    #[test]
    fn replay_pagination_stays_frozen_after_append_and_sealing() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        for seq in 2..=2002 {
            writer
                .append(&event(
                    seq,
                    time,
                    "/quote/X",
                    json!({"symbol":"X","last_price":100}),
                ))
                .unwrap();
        }
        writer.commit_active().unwrap();
        let request = json!({"recording_id":"recA","start_us":time,"end_us":time+1});
        let first =
            crate::execution_replay_reader::window(&directory.0, 7, "E", &request.to_string())
                .unwrap();
        assert_eq!(first["messages"].as_array().unwrap().len(), 2000);
        let cursor = first["next_cursor"].as_str().unwrap().to_owned();
        writer.active.as_mut().unwrap().last_watermark =
            Instant::now() - std::time::Duration::from_secs(2);
        writer.poll(time + 5).unwrap();
        writer
            .append(&event(
                2003,
                time + 5,
                "/quote/X",
                json!({"symbol":"X","last_price":101}),
            ))
            .unwrap();
        writer.commit_active().unwrap();
        let mut next_request = request;
        next_request["cursor"] = json!(cursor);
        let second =
            crate::execution_replay_reader::window(&directory.0, 7, "E", &next_request.to_string())
                .unwrap();
        assert_eq!(second["messages"].as_array().unwrap().len(), 2);
        assert_eq!(second["messages"][1]["capture_seq"], 2002);
        writer.finish().unwrap();
        let after_seal =
            crate::execution_replay_reader::window(&directory.0, 7, "E", &next_request.to_string())
                .unwrap();
        assert_eq!(after_seal, second);
    }

    #[test]
    fn market_identity_is_restored_across_a_checkpoint_for_late_correction() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        let trade = json!({"symbol":"X","trade_key":"T","identity_quality":"stable","price":100,"quantity":3});
        writer
            .append(&event(2, time + 1, "/market_trade/X", trade.clone()))
            .unwrap();
        writer.checkpoint_bytes = CHECKPOINT_BYTES;
        writer.commit_active().unwrap();
        assert!(
            writer.projection.snapshot().unwrap()["trade_accounting"]["market_records"]
                .as_object()
                .unwrap()
                .is_empty()
        );
        writer
            .append(&event(3, time + 2, "/market_trade/X", trade))
            .unwrap();
        writer.commit_active().unwrap();
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        let (_, Some(fact)) = index
            .fact_at("recA", "market_trade", "T", 3)
            .unwrap()
            .unwrap()
        else {
            panic!("missing market fact")
        };
        assert_eq!(fact["record"]["first_capture_seq"], 2);
        assert_eq!(fact["record"]["last_observed_seq"], 3);
        assert_ne!(fact["record"]["conflict"], true);
        let replay = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time+2,"end_us":time+3}).to_string(),
        )
        .unwrap();
        assert_eq!(
            replay["state_before_start"]["trade_accounting"]["market_records"]["T"]
                ["first_capture_seq"],
            2
        );
        assert_eq!(replay["messages"][0]["capture_seq"], 3);
        writer.finish().unwrap();
    }

    #[test]
    fn order_ownership_is_restored_after_the_hot_capture_cache_is_cleared() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time + 1,
                "/order",
                json!({"symbol":"X","order_id":"O","execution_id":"E","origin":"execution"}),
            ))
            .unwrap();
        writer.commit_active().unwrap();
        assert!(writer.capture_cache.is_empty());
        writer.append(&event(3, time+2, "/command", json!({"command_id":"C","operation":"cancel_order","execution_id":"OTHER","symbol":"X","order_id":"O"}))).unwrap();
        writer.append(&event(4, time+3, "/trade", json!({"symbol":"X","order_id":"O","trade_key":"T","identity_quality":"stable","price":100,"quantity":3}))).unwrap();
        writer.commit_active().unwrap();
        let replay = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+4}).to_string(),
        )
        .unwrap();
        assert_eq!(replay["messages"][3]["data"]["execution_id"], "E");
        assert_eq!(replay["messages"][3]["data"]["origin"], "execution");
        writer.finish().unwrap();
    }

    #[test]
    fn own_trade_index_keeps_late_ownership_and_corrections_as_of_each_sequence() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer.append(&event(2, time+1, "/trade", json!({"symbol":"X","order_id":"O","trade_key":"T","identity_quality":"stable","price":100,"quantity":3}))).unwrap();
        writer.commit_active().unwrap();
        writer.append(&event(3, time+2, "/command", json!({"command_id":"C","operation":"place_order","phase":"result","execution_id":"E","symbol":"X","order_id":"O"}))).unwrap();
        writer.commit_active().unwrap();
        writer.append(&event(4, time+3, "/trade", json!({"symbol":"X","order_id":"O","trade_key":"T","identity_quality":"stable","price":100,"quantity":3,"direction":"BUY"}))).unwrap();
        writer.commit_active().unwrap();
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        let (_, Some(before)) = index.fact_at("recA", "own_trade", "T", 2).unwrap().unwrap() else {
            panic!("missing original trade")
        };
        let (_, Some(associated)) = index.fact_at("recA", "own_trade", "T", 3).unwrap().unwrap()
        else {
            panic!("missing association")
        };
        let (_, Some(corrected)) = index.fact_at("recA", "own_trade", "T", 4).unwrap().unwrap()
        else {
            panic!("missing correction")
        };
        assert!(before["record"]["data"]["execution_id"].is_null());
        assert_eq!(associated["record"]["data"]["execution_id"], "E");
        assert_eq!(associated["record"]["data"]["association_seq"], 3);
        assert_eq!(corrected["record"]["first_capture_seq"], 2);
        assert_eq!(corrected["record"]["last_observed_seq"], 4);
        assert_eq!(corrected["record"]["data"]["direction"], "BUY");
        writer.finish().unwrap();
    }

    #[test]
    fn weak_own_observation_index_preserves_its_late_order_association() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer.append(&event(2, time+1, "/trade", json!({"symbol":"X","order_id":"O","identity_quality":"weak","price":100,"quantity":3}))).unwrap();
        writer.commit_active().unwrap();
        writer.append(&event(3, time+2, "/command", json!({"command_id":"C","operation":"place_order","phase":"result","execution_id":"E","symbol":"X","order_id":"O"}))).unwrap();
        writer.commit_active().unwrap();
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        let (_, Some(before)) = index
            .fact_at("recA", "own_observation", "2", 2)
            .unwrap()
            .unwrap()
        else {
            panic!("missing original observation")
        };
        let (_, Some(after)) = index
            .fact_at("recA", "own_observation", "2", 3)
            .unwrap()
            .unwrap()
        else {
            panic!("missing associated observation")
        };
        assert!(before["observation"]["execution_id"].is_null());
        assert_eq!(after["observation"]["execution_id"], "E");
        assert_eq!(after["observation"]["association_seq"], 3);
        writer.finish().unwrap();
    }

    #[test]
    fn weak_market_observations_remain_distinct_after_a_checkpoint() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time + 1,
                "/market_trade/X",
                json!({"symbol":"X","identity_quality":"weak","price":100,"quantity":3}),
            ))
            .unwrap();
        writer.checkpoint_bytes = CHECKPOINT_BYTES;
        writer.commit_active().unwrap();
        writer
            .append(&event(
                3,
                time + 2,
                "/market_trade/X",
                json!({"symbol":"X","identity_quality":"weak","price":100,"quantity":3}),
            ))
            .unwrap();
        writer.commit_active().unwrap();
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        assert!(matches!(
            index.fact_at("recA", "market_observation", "2", 3).unwrap(),
            Some((2, Some(_)))
        ));
        assert!(matches!(
            index.fact_at("recA", "market_observation", "3", 3).unwrap(),
            Some((3, Some(_)))
        ));
        let replay = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time+2,"end_us":time+3}).to_string(),
        )
        .unwrap();
        assert!(
            replay["state_before_start"]["trade_accounting"]["market_seen_keys"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert_eq!(replay["messages"][0]["data"]["identity_quality"], "weak");
        writer.finish().unwrap();
    }

    #[test]
    fn many_market_trades_keep_checkpoints_bounded_and_deduplicate_late_observations() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        for seq in 2..=10_020 {
            writer.append(&event(seq, time+seq, "/market_trade/X", json!({"symbol":"X","trade_key":format!("T{seq}"),"identity_quality":"stable","price":100,"quantity":3}))).unwrap();
        }
        writer.append(&event(10_021, time+10_021, "/market_trade/X", json!({"symbol":"X","trade_key":"T2","identity_quality":"stable","price":100,"quantity":3}))).unwrap();
        writer.commit_active().unwrap();
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        let checkpoint = index
            .checkpoint_before("recA", time + 10_021, 10_021)
            .unwrap()
            .unwrap();
        assert!(checkpoint.seq >= 10_000);
        assert!(checkpoint.state["trade_accounting"]["market_records"]
            .as_object()
            .unwrap()
            .is_empty());
        let (_, Some(fact)) = index
            .fact_at("recA", "market_trade", "T2", 10_021)
            .unwrap()
            .unwrap()
        else {
            panic!("missing trade")
        };
        assert_eq!(fact["record"]["first_capture_seq"], 2);
        assert_eq!(fact["record"]["last_observed_seq"], 10_021);
        let replay = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time+10_021,"end_us":time+10_022}).to_string(),
        )
        .unwrap();
        assert_eq!(
            replay["state_before_start"]["trade_accounting"]["market_records"]["T2"]
                ["first_capture_seq"],
            2
        );
        assert_eq!(replay["messages"][0]["capture_seq"], 10_021);
        writer.finish().unwrap();
    }

    #[test]
    fn many_own_trades_remain_recordable() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        for seq in 2..=1000 {
            writer.append(&event(seq, time+seq, "/trade", json!({"symbol":"X","execution_id":"E","trade_key":format!("T{seq}"),"identity_quality":"stable","price":100,"quantity":3}))).unwrap();
        }
        writer.commit_active().unwrap();
        let state = writer.projection.snapshot().unwrap();
        assert_eq!(
            state["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["X"]["quantity"],
            2997.0
        );
    }

    #[test]
    fn large_account_checkpoint_does_not_limit_recording_or_file_rotation() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        for seq in 2..=1200 {
            writer.append(&event(seq, time+seq, "/trade", json!({"symbol":"X","execution_id":"E","order_id":"O",
                "trade_key":format!("T{seq}"),"identity_quality":"stable","price":100,"quantity":3,"source":"S".repeat(4_000)}))).unwrap();
        }
        writer.checkpoint_bytes = CHECKPOINT_BYTES;
        writer.rotate_for_test(time + 1201).unwrap();
        writer
            .append(&event(
                1201,
                time + 1201,
                "/quote/X",
                json!({"symbol":"X","last_price":100}),
            ))
            .unwrap();
        writer.append(&event(1202, time+1202, "/trade", json!({"symbol":"X","order_id":"O",
            "trade_key":"T2","identity_quality":"stable","price":100,"quantity":3,"direction":"BUY"}))).unwrap();
        writer.finish().unwrap();
        assert_eq!(writer.sealed.len(), 2);
        assert!(writer
            .sealed
            .iter()
            .all(|part| part.size_bytes <= PART_FILE_LIMIT));
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        let checkpoint = index.checkpoint_at("recA", 2, 1200).unwrap().unwrap();
        assert!(serde_json::to_vec(&checkpoint.state).unwrap().len() > 3 * 1024 * 1024);
        let db = rusqlite::Connection::open(writer.account.join(".replay-index.sqlite")).unwrap();
        let stored_bytes: usize = db.query_row(
            "SELECT length(payload) FROM checkpoints WHERE recording='recA' AND part=2 AND seq=1200",
            [], |row| row.get(0),
        ).unwrap();
        assert!(stored_bytes < 100_000);
        assert_eq!(
            checkpoint.state["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["X"]
                ["quantity"],
            3597.0
        );
        let mut request = json!({"recording_id":"recA","start_us":time+1201,"end_us":time+1203});
        let mut fragments = String::new();
        let mut messages = Vec::new();
        let mut complete = false;
        for _ in 0..32 {
            let page =
                crate::execution_replay_reader::window(&directory.0, 7, "E", &request.to_string())
                    .unwrap();
            assert!(serde_json::to_vec(&page).unwrap().len() <= PAGE_LIMIT);
            if let Some(fragment) = page["state_fragment"].as_str() {
                fragments.push_str(fragment);
            }
            messages.extend(page["messages"].as_array().unwrap().iter().cloned());
            if page["complete"] == true {
                complete = true;
                break;
            }
            request["cursor"] = page["next_cursor"].clone();
        }
        assert!(complete);
        let restored: Value = serde_json::from_str(&fragments).unwrap();
        assert_eq!(
            restored["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["X"]["quantity"],
            3597.0
        );
        assert_eq!(
            messages
                .iter()
                .map(|message| message["capture_seq"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![1201, 1202]
        );
        db.execute(
            "UPDATE facts SET value='{}' WHERE recording='recA' AND kind='own_trade' AND key='T2'",
            [],
        )
        .unwrap();
        assert!(index.checkpoint_at("recA", 2, 1200).is_err());
    }

    #[test]
    fn compact_checkpoint_keeps_weak_trade_association_consistent() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time + 1,
                "/trade",
                json!({"symbol":"X","order_id":"O",
            "identity_quality":"weak","price":100,"quantity":3}),
            ))
            .unwrap();
        writer
            .append(&event(
                3,
                time + 2,
                "/command",
                json!({"symbol":"X","order_id":"O",
            "execution_id":"E","command_id":"C","operation":"place_order","phase":"result"}),
            ))
            .unwrap();
        let expected = writer.projection.snapshot().unwrap();
        writer.rotate_for_test(time + 3).unwrap();
        writer
            .append(&event(
                4,
                time + 3,
                "/quote/X",
                json!({"symbol":"X","last_price":100}),
            ))
            .unwrap();
        writer.finish().unwrap();
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        assert_eq!(
            index.checkpoint_at("recA", 2, 3).unwrap().unwrap().state,
            expected
        );
    }

    #[test]
    fn a_compressible_event_larger_than_a_file_can_be_recorded() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time + 1,
                "/quote/X",
                json!({"symbol":"X","source":"X".repeat(51 * 1024 * 1024)}),
            ))
            .unwrap();
        writer.finish().unwrap();
        assert!(writer
            .sealed
            .iter()
            .all(|part| part.size_bytes <= PART_FILE_LIMIT));
        assert_eq!(writer.status()["state_through_seq"], 2);
        let index = BlockIndex::open(&writer.account.join(".replay-index.sqlite")).unwrap();
        let blocks = index.blocks("recA", 2, None, 8).unwrap();
        let part = writer.sealed.last().unwrap();
        let path = writer.account.join(&part.day).join(part.file_name());
        let records =
            crate::execution_replay_block::read_block(&path, &blocks.last().unwrap().range)
                .unwrap();
        assert!(records.iter().any(|record| matches!(record, BlockRecord::Message(message) if message.data.len() > PART_FILE_LIMIT as usize)));
    }

    #[test]
    fn rotation_waits_for_hashing_instead_of_stopping_recording() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        let (release, wait) = std::sync::mpsc::channel();
        let mut release = Some(release);
        let mut wait = Some(wait);
        for seq in 1..=3 {
            writer
                .append(&event(
                    seq,
                    time + seq,
                    "/execution",
                    json!({"execution_id":"E","phase":"started","symbol":"X"}),
                ))
                .unwrap();
            if seq == 3 {
                let release = release.take().unwrap();
                thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_millis(100));
                    release.send(()).unwrap();
                });
            }
            writer.seal_inner(Some(time + seq + 1)).unwrap();
            if seq == 1 {
                let mut pending = writer.pending_seals.remove(0);
                let hash = pending.hash;
                let wait = wait.take().unwrap();
                pending.hash = thread::spawn(move || {
                    wait.recv().unwrap();
                    hash.join().unwrap()
                });
                writer.pending_seals.insert(0, pending);
            }
        }
        writer.finish().unwrap();
        assert_eq!(writer.sealed.len(), 3);
    }

    #[test]
    fn oversized_event_spans_bounded_mcap_files_and_replays_intact() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        writer.file_limit = 512 * 1024;
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        let mut random = 1_u64;
        let source: String = (0..6_000_000)
            .map(|_| {
                random ^= random << 13;
                random ^= random >> 7;
                random ^= random << 17;
                (b'!' + (random % 90) as u8) as char
            })
            .collect();
        writer
            .append(&event(
                2,
                time + 1,
                "/quote/X",
                json!({"symbol":"X","source":source}),
            ))
            .unwrap();
        writer.finish().unwrap();
        let day = writer.account.join(shanghai_day(time).unwrap());
        let files: Vec<_> = fs::read_dir(&day)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "mcap"))
            .collect();
        assert!(files.len() > 2);
        assert!(files
            .iter()
            .all(|path| fs::metadata(path).unwrap().len() <= writer.file_limit));
        let mut request = json!({"recording_id":"recA","start_us":time,"end_us":time+2});
        let mut fragments = String::new();
        let mut pages = 0;
        loop {
            let page =
                crate::execution_replay_reader::window(&directory.0, 7, "E", &request.to_string())
                    .unwrap();
            pages += 1;
            assert!(pages < 20);
            if let Some(fragment) = page["message_fragment"].as_str() {
                fragments.push_str(fragment);
            }
            if page["complete"] == true {
                break;
            }
            request["cursor"] = page["next_cursor"].clone();
        }
        let restored: Value = serde_json::from_str(&fragments).unwrap();
        assert_eq!(restored["data"]["source"], source);
        let fragment = files
            .iter()
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("fragment-")
            })
            .unwrap();
        let mut damaged = fs::read(fragment).unwrap();
        let position = damaged.len() / 2;
        damaged[position] ^= 1;
        fs::write(fragment, damaged).unwrap();
        assert!(crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+2}).to_string()
        )
        .is_err());
        fs::remove_file(fragment).unwrap();
        assert_eq!(
            crate::execution_replay_reader::manifest(&directory.0, 7, "E", None)
                .unwrap_err()
                .code,
            "REPLAY_INCOMPLETE"
        );
        assert!(crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+2}).to_string()
        )
        .is_err());
    }

    #[test]
    fn rolls_before_physical_file_cap_including_footer_and_summary() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        writer.file_limit = 512 * 1024;
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        let mut random = 1_u64;
        for seq in 2..=100 {
            let source: String = (0..8000)
                .map(|_| {
                    random ^= random << 13;
                    random ^= random >> 7;
                    random ^= random << 17;
                    (b'A' + (random % 26) as u8) as char
                })
                .collect();
            writer
                .append(&event(
                    seq,
                    time + seq,
                    "/quote/X",
                    json!({"symbol":"X","source":source}),
                ))
                .unwrap();
            writer.commit_active().unwrap();
        }
        writer.finish().unwrap();
        assert!(writer.sealed.len() >= 2);
        for part in &writer.sealed {
            let path = day_directory(&writer.account, &part.day, false)
                .unwrap()
                .join(part.file_name());
            let metadata = fs::metadata(&path).unwrap();
            assert_eq!(metadata.len(), part.size_bytes);
            assert!(metadata.len() <= writer.file_limit);
            assert_eq!(
                digest_file(&path, writer.file_limit).unwrap().0,
                part.sha256
            );
        }
        let replay = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+101}).to_string(),
        )
        .unwrap();
        assert_eq!(replay["messages"].as_array().unwrap().len(), 100);
        assert_eq!(replay["messages"][99]["capture_seq"], 100);
    }

    #[test]
    fn missing_or_truncated_rotated_file_does_not_hide_later_checkpoints() {
        let time = 1_793_404_800_000_000_u64;
        let minute = 60_000_000;
        for damage in ["missing", "truncated", "all"] {
            for damaged in 0..if damage == "all" { 1 } else { 3 } {
                let directory = TestDirectory::new();
                let mut writer =
                    ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
                for (offset, sequence) in [0, minute - 1, minute, 2 * minute - 1, 2 * minute]
                    .into_iter()
                    .zip(1..)
                {
                    writer.append(&event(sequence, time + offset, "/execution", json!({"execution_id":"E","phase":"started","target_quantity":sequence}))).unwrap();
                    if sequence == 2 || sequence == 4 {
                        writer
                            .rotate_for_test(time + if sequence == 2 { minute } else { 2 * minute })
                            .unwrap();
                    }
                }
                writer.finish().unwrap();
                let files: Vec<_> = writer
                    .sealed
                    .iter()
                    .map(|part| {
                        day_directory(&writer.account, &part.day, false)
                            .unwrap()
                            .join(part.file_name())
                    })
                    .collect();
                assert_eq!(files.len(), 3);
                let original =
                    crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
                assert_eq!(
                    original["recordings"][0]["coverage"],
                    json!([{"start_us":time,"end_us":time+2*minute+1}])
                );
                if damage == "all" {
                    for path in &files {
                        fs::remove_file(path).unwrap();
                    }
                } else if damage == "missing" {
                    fs::remove_file(&files[damaged]).unwrap();
                } else {
                    let mut bytes = fs::read(&files[damaged]).unwrap();
                    bytes.pop();
                    fs::write(&files[damaged], bytes).unwrap();
                }
                let manifest =
                    crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
                let recording = &manifest["recordings"][0];
                assert_eq!(manifest["availability"], "partial");
                assert_eq!(recording["availability"], "partial");
                assert!(recording["gaps"]
                    .as_array()
                    .unwrap()
                    .contains(&json!({"reason":"file_missing_or_changed"})));
                assert_eq!(recording["parts"], original["recordings"][0]["parts"]);
                let expected = match (damage, damaged) {
                    ("all", _) => json!([]),
                    (_, 0) => json!([{"start_us":time+minute,"end_us":time+2*minute+1}]),
                    (_, 1) => {
                        json!([{"start_us":time,"end_us":time+minute},{"start_us":time+2*minute,"end_us":time+2*minute+1}])
                    }
                    _ => json!([{"start_us":time,"end_us":time+2*minute}]),
                };
                assert_eq!(recording["coverage"], expected);
                for interval in recording["coverage"].as_array().unwrap() {
                    let start = interval["start_us"].as_u64().unwrap();
                    let end = interval["end_us"].as_u64().unwrap();
                    let page = crate::execution_replay_reader::window(
                        &directory.0,
                        7,
                        "E",
                        &json!({"recording_id":"recA","start_us":start,"end_us":end}).to_string(),
                    )
                    .unwrap();
                    assert_eq!(page["complete"], true);
                    assert!(!page["messages"].as_array().unwrap().is_empty());
                    if start > time {
                        assert_eq!(
                            page["state_before_start"]["state_through_seq"],
                            if damaged == 0 { 2 } else { 4 }
                        );
                    }
                }
                let missing_time = time + damaged as u64 * minute;
                assert!(crate::execution_replay_reader::window(
                    &directory.0,
                    7,
                    "E",
                    &json!({"recording_id":"recA","start_us":missing_time,"end_us":missing_time+1})
                        .to_string()
                )
                .is_err());
            }
        }
    }

    #[test]
    fn missing_rotated_prefix_at_same_microsecond_preserves_only_later_coverage() {
        let time = 1_793_404_800_000_000_u64;
        for same_time_parts in [1, 2] {
            let directory = TestDirectory::new();
            let mut writer =
                ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
            for sequence in 1..=same_time_parts + 2 {
                let offset = if sequence == same_time_parts + 2 {
                    10
                } else {
                    0
                };
                writer
                    .append(&event(
                        sequence,
                        time + offset,
                        "/execution",
                        json!({"execution_id":"E","phase":"started"}),
                    ))
                    .unwrap();
                if sequence <= same_time_parts {
                    writer.rotate_for_test(time).unwrap();
                }
            }
            writer.finish().unwrap();
            let first = &writer.sealed[0];
            fs::remove_file(
                day_directory(&writer.account, &first.day, false)
                    .unwrap()
                    .join(first.file_name()),
            )
            .unwrap();
            let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
            assert_eq!(
                manifest["recordings"][0]["coverage"],
                json!([{"start_us":time+1,"end_us":time+11}])
            );
            let page = crate::execution_replay_reader::window(
                &directory.0,
                7,
                "E",
                &json!({"recording_id":"recA","start_us":time+1,"end_us":time+11}).to_string(),
            )
            .unwrap();
            assert_eq!(
                page["state_before_start"]["state_through_seq"],
                same_time_parts + 1
            );
            assert_eq!(
                page["messages"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|item| item["capture_seq"].as_u64().unwrap())
                    .collect::<Vec<_>>(),
                vec![same_time_parts + 2]
            );
            assert!(crate::execution_replay_reader::window(
                &directory.0,
                7,
                "E",
                &json!({"recording_id":"recA","start_us":time,"end_us":time+11}).to_string()
            )
            .is_err());
        }
    }

    #[test]
    fn missing_rotated_middle_at_same_microsecond_clips_both_sides() {
        let time = 1_793_404_800_000_000_u64;
        for remove_descriptor in [false, true] {
            let directory = TestDirectory::new();
            let mut writer =
                ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
            for (sequence, offset) in [(1, -10_i64), (2, 0), (3, 0), (4, 0), (5, 10)] {
                writer
                    .append(&event(
                        sequence,
                        time.checked_add_signed(offset).unwrap(),
                        "/execution",
                        json!({"execution_id":"E","phase":"started"}),
                    ))
                    .unwrap();
                if sequence == 2 || sequence == 3 {
                    writer.rotate_for_test(time).unwrap();
                }
            }
            writer.finish().unwrap();
            let middle = &writer.sealed[1];
            let day = day_directory(&writer.account, &middle.day, false).unwrap();
            fs::remove_file(day.join(middle.file_name())).unwrap();
            if remove_descriptor {
                let db = rusqlite::Connection::open(writer.account.join(".replay-index.sqlite"))
                    .unwrap();
                db.execute("DELETE FROM files WHERE recording='recA' AND part=2", [])
                    .unwrap();
            }
            let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
            assert_eq!(
                manifest["recordings"][0]["coverage"],
                json!([{"start_us":time-10,"end_us":time},{"start_us":time+1,"end_us":time+11}])
            );
            let before = crate::execution_replay_reader::window(
                &directory.0,
                7,
                "E",
                &json!({"recording_id":"recA","start_us":time-10,"end_us":time}).to_string(),
            )
            .unwrap();
            assert_eq!(before["messages"][0]["capture_seq"], 1);
            let after = crate::execution_replay_reader::window(
                &directory.0,
                7,
                "E",
                &json!({"recording_id":"recA","start_us":time+1,"end_us":time+11}).to_string(),
            )
            .unwrap();
            assert_eq!(after["state_before_start"]["state_through_seq"], 4);
            assert_eq!(after["messages"][0]["capture_seq"], 5);
            assert!(crate::execution_replay_reader::window(
                &directory.0,
                7,
                "E",
                &json!({"recording_id":"recA","start_us":time-10,"end_us":time+1}).to_string()
            )
            .is_err());
        }
    }

    #[test]
    fn same_microsecond_across_parts_has_empty_baseline_and_all_observations() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        writer.append(&event(2, time, "/order", json!({"execution_id":"E","symbol":"X","order_id":"A","status":"working","confirmed_book":true,"price":99,"quantity":10,"filled_quantity":0,"remaining_quantity":10}))).unwrap();
        writer.seal().unwrap();
        writer.append(&event(3, time, "/trade", json!({"execution_id":"E","symbol":"X","order_id":"A","trade_key":"account7:X:T","identity_quality":"stable","direction":"BUY","offset":"OPEN","position_side":"LONG","price":99,"quantity":4}))).unwrap();
        writer.finish().unwrap();
        let result = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+1}).to_string(),
        )
        .unwrap();
        assert_eq!(result["state_before_start"]["state_through_seq"], 0);
        assert_eq!(result["state_before_start"]["time_us"], Value::Null);
        assert_eq!(
            result["messages"]
                .as_array()
                .unwrap()
                .iter()
                .map(|message| message["capture_seq"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![1, 2, 3]
        );
        assert_eq!(result["complete"], true);
    }

    #[test]
    fn execution_directory_can_grow_beyond_part_metadata_limit() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        let execution = |sequence| format!("execution-{sequence:04}-{}", "x".repeat(60));
        for sequence in 1..=1_100 {
            writer
                .append(&event(
                    sequence,
                    time + sequence,
                    "/execution",
                    json!({"execution_id":execution(sequence),"phase":"started","symbol":"X"}),
                ))
                .unwrap();
        }
        writer.finish().unwrap();
        assert!(writer
            .sealed
            .iter()
            .all(|part| part.size_bytes <= PART_FILE_LIMIT));
        for sequence in [1, 1_100] {
            let manifest =
                crate::execution_replay_reader::manifest(&directory.0, 7, &execution(sequence), None)
                    .unwrap();
            assert_eq!(manifest["availability"], "available");
            assert_eq!(manifest["recordings"][0]["recording_id"], "recA");
        }
    }

    #[test]
    fn duplicate_trade_after_checkpoint_keeps_original_execution_quantity() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        let time = 1_793_318_400_000_000_u64;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","phase":"started","symbol":"X"}),
            ))
            .unwrap();
        let trade = json!({"execution_id":"E","symbol":"X","order_id":"A","trade_key":"account7:X:T","identity_quality":"stable","direction":"BUY","offset":"OPEN","position_side":"LONG","price":99,"quantity":4});
        writer
            .append(&event(2, time + 1, "/trade", trade.clone()))
            .unwrap();
        writer.seal().unwrap();
        writer.append(&event(3, time + 2, "/trade", trade)).unwrap();
        writer.finish().unwrap();
        let result = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time+3,"end_us":time+4}).to_string(),
        )
        .unwrap();
        let accounting = &result["state_before_start"]["trade_accounting"];
        assert_eq!(
            accounting["execution_totals"]["E"]["by_symbol"]["X"]["quantity"],
            4.0
        );
        assert_eq!(accounting["own_seen_keys"].as_array().unwrap().len(), 1);
        assert!(result["messages"].as_array().unwrap().is_empty());
    }

    #[test]
    fn unsealed_execution_is_visible_without_reading_partial_mcap() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        writer
            .append(&event(
                1,
                1_793_318_400_000_000,
                "/execution",
                json!({"execution_id":"E","phase":"started"}),
            ))
            .unwrap();
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert_eq!(manifest["availability"], "partial");
        assert_eq!(manifest["recordings"][0]["recording_id"], "recA");
        assert!(manifest["recordings"][0]["parts"]
            .as_array()
            .unwrap()
            .is_empty());
        assert!(manifest["recordings"][0]["gaps"]
            .as_array()
            .unwrap()
            .iter()
            .any(|gap| gap["reason"] == "unsealed"));
        drop(writer);
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert_eq!(manifest["availability"], "partial");
    }

    #[test]
    fn single_account_lease_and_clock_failure_do_not_publish_tail() {
        let directory = TestDirectory::new();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context()).unwrap();
        assert!(ExecutionReplayWriter::new(&directory.0, 7, "recB", &context()).is_err());
        writer
            .append(&event(
                1,
                1_793_318_400_000_000,
                "/execution",
                json!({"execution_id":"E","phase":"started"}),
            ))
            .unwrap();
        assert_eq!(
            writer
                .append(&event(
                    2,
                    1_793_318_399_999_999,
                    "/connection",
                    json!({"connected":false})
                ))
                .unwrap_err()
                .code,
            "REPLAY_CLOCK_ROLLBACK"
        );
        assert_eq!(writer.status()["state"], "error");
        drop(writer);
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert!(manifest["recordings"][0]["parts"]
            .as_array()
            .unwrap()
            .is_empty());
    }

    #[test]
    fn trust_boundary_rejects_unknown_fields_credentials_and_unsafe_integers() {
        let context: Value = serde_json::from_str(&context()).unwrap();
        let mut message: Value = serde_json::from_str(&event(
            1,
            1_793_318_400_000_000,
            "/execution",
            json!({"execution_id":"E"}),
        ))
        .unwrap();
        message["data"]["extra"] = json!({"password":"never-record"});
        assert!(validate_message(&message, &context).is_err());
        message["data"] = json!({"execution_id":"E","details":{"password":"never-record"}});
        assert!(validate_message(&message, &context).is_err());
        message["data"] = json!({"execution_id":"E"});
        message["time_us"] = json!(JS_SAFE + 1);
        assert!(validate_message(&message, &context).is_err());
        let unsafe_value: Value = serde_json::from_str("100000000000000000000000000000").unwrap();
        assert!(validate_json(&unsafe_value, 0).is_err());
    }

    #[test]
    fn real_writer_accepts_canonical_scope_sources_and_declared_nested_extensions() {
        let directory = TestDirectory::new();
        let context = json!({"schema_version":1,"channel":"external-neutral",
            "attributes":{"order":["note"],"position":["cost"]},
            "native_identity":{"order":["source_order_id"]}})
        .to_string();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context).unwrap();
        let time = 1_793_318_400_000_000;
        writer
            .append(&event(
                1,
                time,
                "/execution",
                json!({"execution_id":"E","symbol":"X","phase":"started"}),
            ))
            .unwrap();
        writer.append(&event(2,time+1,"/account_observation",json!({"execution_id":"E",
            "scope":{"kind":"account","symbols":[]},"scope_complete":true,
            "source_times":{"date":"2026-10-30","clock":"09:00:00","millisecond":null},
            "source_precision":"second","assets":{"balance":1000,"available_cash":800,"frozen_margin":200},
            "positions":[{"symbol":"X","position_side":"LONG","quantity":4,"today_quantity":4,
                "long_frozen":0,"short_frozen":0,"attributes":{"cost":99},"field_quality":{"quantity":"known"}}],
            "position_basis":{"status":"unverified","proof":null}}))).unwrap();
        writer.append(&event(3,time+2,"/order",json!({"execution_id":"E","symbol":"X","order_id":"A",
            "scope":{"kind":"symbols","symbols":["X"]},"sync_generation":1,
            "status":"working","confirmed_book":true,"quantity":10,"filled_quantity":0,"remaining_quantity":10,"price":99,
            "attributes":{"note":"observed"},"native_identity":{"source_order_id":"native-A"}}))).unwrap();
        writer.finish().unwrap();
        let result = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+3}).to_string(),
        )
        .unwrap();
        assert_eq!(result["messages"].as_array().unwrap().len(), 3);
        assert_eq!(result["messages"][1]["data"]["scope"]["kind"], "account");
        assert_eq!(
            result["messages"][2]["data"]["attributes"]["note"],
            "observed"
        );
    }

    #[test]
    fn recovery_metadata_budget_includes_the_real_mcap_body_wrapper() {
        let json_limit = RECOVERY_METADATA_LIMIT - 20 - RECOVERY_METADATA_NAME.len();
        let payload = serde_json::to_string(&"x".repeat(json_limit - 2)).unwrap();
        let body_bytes = recovery_metadata_size(payload.len()).unwrap();
        let mut writer = mcap::WriteOptions::new()
            .use_chunks(false)
            .compression(None)
            .create(std::io::Cursor::new(Vec::new()))
            .unwrap();
        writer
            .write_metadata(&mcap::records::Metadata {
                name: RECOVERY_METADATA_NAME.into(),
                metadata: BTreeMap::from([("json".into(), payload)]),
            })
            .unwrap();
        let summary = writer.finish().unwrap();
        assert_eq!(body_bytes, RECOVERY_METADATA_LIMIT);
        assert_eq!(summary.metadata_indexes[0].length, (body_bytes + 9) as u64);
        assert_eq!(
            recovery_metadata_size(json_limit + 1).unwrap_err().code,
            "REPLAY_BUDGET"
        );
        assert!(recovery_metadata_size(usize::MAX).is_err());
    }

    #[test]
    fn manifest_keeps_later_and_nested_symbols_without_future_price_facts() {
        let directory = TestDirectory::new();
        let context = json!({"schema_version":1}).to_string();
        let mut writer = ExecutionReplayWriter::new(&directory.0, 7, "recA", &context).unwrap();
        let time = 1_793_318_400_000_000;
        writer
            .append(&event(
                1,
                time,
                "/quote/A",
                json!({"symbol":"A","book_valid":false}),
            ))
            .unwrap();
        writer
            .append(&event(
                2,
                time + 30_000_000,
                "/execution",
                json!({"execution_id":"E","phase":"start","symbols":["X"]}),
            ))
            .unwrap();
        writer
            .append(&event(3, time + 30_000_001, "/account_observation", json!({"scope":{"kind":"account","symbols":[]},"scope_complete":false,"positions":[{"symbol":"P","position_side":"LONG","quantity":1}]})))
            .unwrap();
        writer
            .append(&event(4, time + 30_000_002, "/order_set", json!({"scope":{"kind":"account","symbols":[]},"scope_complete":false,"synchronized":false,"orders":[{"symbol":"Q","order_id":"O","status":"unknown"}]})))
            .unwrap();
        writer.append(&event(5, time + 30_000_003, "/order_set", json!({
            "scope":{"kind":"symbols","symbols":["SCOPED"]},"scope_complete":true,"synchronized":false,"orders":[]}))).unwrap();
        writer
            .append(&event(
                6,
                time + 30_000_004,
                "/order_set",
                json!({
            "scope":["LEGACY"],"scope_complete":true,"synchronized":false,"orders":[]}),
            ))
            .unwrap();
        writer.finish().unwrap();
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert_eq!(
            manifest["recordings"][0]["symbols"],
            json!(["A", "LEGACY", "P", "Q", "SCOPED", "X"])
        );
        assert_eq!(manifest["recordings"][0]["symbols_complete"], true);
        let window = crate::execution_replay_reader::window(
            &directory.0,
            7,
            "E",
            &json!({"recording_id":"recA","start_us":time,"end_us":time+20_000_000}).to_string(),
        )
        .unwrap();
        assert_eq!(window["messages"].as_array().unwrap().len(), 1);
        assert_eq!(window["messages"][0]["data"]["symbol"], "A");
        let catalog = directory
            .0
            .join("account-7")
            .join(&writer.sealed[0].day)
            .join("catalog.json");
        assert!(!catalog.exists());
        let recovered = crate::execution_replay_reader::manifest(&directory.0, 7, "E", None).unwrap();
        assert_eq!(
            recovered["recordings"][0]["symbols"],
            json!(["A", "LEGACY", "P", "Q", "SCOPED", "X"])
        );
        assert!(!catalog.exists());
    }
}
