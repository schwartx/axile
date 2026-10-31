//! Account-local execution observations, persisted as sealed, indexed MCAP parts.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

use chrono::{DateTime, Utc};
use chrono_tz::Asia::Shanghai;
use mcap::records::MessageHeader;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::AnalysisError;
use crate::execution_replay_projection::Projection;

pub(crate) const JS_SAFE: u64 = 9_007_199_254_740_991;
pub(crate) const MESSAGE_LIMIT: usize = 256 * 1024;
pub(crate) const CONTEXT_LIMIT: usize = 256 * 1024;
pub(crate) const CHECKPOINT_LIMIT: usize = 3 * 1024 * 1024;
pub(crate) const PAGE_LIMIT: usize = 4 * 1024 * 1024;
pub(crate) const PART_LIMIT: u64 = 32 * 1024 * 1024;
pub(crate) const PART_FILE_LIMIT: u64 = PART_LIMIT + 4 * 1024 * 1024;
pub(crate) const CATALOG_LIMIT: usize = 8 * 1024 * 1024;
pub(crate) const SCAN_BYTES: u64 = 128 * 1024 * 1024;
pub(crate) const SCAN_EVENTS: usize = 100_000;
pub(crate) const SCAN_FILES: usize = 4096;
pub(crate) const SCAN_DAYS: usize = 366;
pub(crate) const SCAN_SECONDS: u64 = 5;
pub(crate) const RECOVERY_METADATA_LIMIT: usize = 64 * 1024;
pub(crate) const RECOVERY_METADATA_NAME: &str = "axile.execution_replay.part.v1";
const ACCOUNT_LIMIT: u64 = 1024 * 1024 * 1024;
const PART_DURATION_US: u64 = 60_000_000;
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

fn sync_directory(path: &Path) -> ReplayResult<()> {
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

pub(crate) fn read_catalogs(account: &Path) -> ReplayResult<Vec<Catalog>> {
    if !account.exists() {
        return Ok(Vec::new());
    }
    let deadline = Instant::now();
    let mut result = Vec::new();
    let mut entries = 0;
    let mut bytes = 0;
    for entry in fs::read_dir(account).map_err(io_error)? {
        entries += 1;
        if entries > SCAN_FILES || deadline.elapsed().as_secs() >= SCAN_SECONDS {
            return Err(failure("BUDGET", "catalog directory scan exceeds budget"));
        }
        let entry = entry.map_err(io_error)?;
        let day = entry.file_name().to_string_lossy().into_owned();
        if day.len() != 10 || chrono::NaiveDate::parse_from_str(&day, "%Y-%m-%d").is_err() {
            continue;
        }
        let directory = day_directory(account, &day, false)?;
        let path = child_file(&directory, "catalog.json")?;
        if !path.exists() {
            continue;
        }
        let input = read_bounded(&path, CATALOG_LIMIT)?;
        bytes += input.len();
        if bytes > 32 * 1024 * 1024 || result.len() >= SCAN_DAYS {
            return Err(failure("BUDGET", "catalog scan exceeds budget"));
        }
        let catalog: Catalog = serde_json::from_slice(&input)
            .map_err(|error| failure("CORRUPT", error.to_string()))?;
        if catalog.schema_version != 1 || catalog.day != day {
            return Err(failure("CORRUPT", "invalid replay catalog version or day"));
        }
        if catalog.parts.len() > SCAN_FILES {
            return Err(failure("BUDGET", "catalog contains too many parts"));
        }
        result.push(catalog);
    }
    result.sort_by(|left, right| left.day.cmp(&right.day));
    Ok(result)
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

fn validate_json(value: &Value, depth: usize, remaining: &mut usize) -> ReplayResult<()> {
    if *remaining == 0 || depth > 12 {
        return Err(failure("BUDGET", "JSON structure exceeds budget"));
    }
    *remaining -= 1;
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
        Value::String(text) if text.len() > 8192 => {
            return Err(failure("BUDGET", "JSON string exceeds budget"))
        }
        Value::Array(items) => {
            if items.len() > 2048 {
                return Err(failure("BUDGET", "JSON array exceeds budget"));
            }
            for item in items {
                validate_json(item, depth + 1, remaining)?;
            }
        }
        Value::Object(fields) => {
            if fields.len() > 2048 {
                return Err(failure("BUDGET", "JSON object exceeds budget"));
            }
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
                validate_json(value, depth + 1, remaining)?;
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
    let mut remaining = 20_000;
    validate_json(message, 0, &mut remaining)?;
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

struct ActivePart {
    writer: mcap::Writer<File>,
    path: PathBuf,
    descriptor: PartDescriptor,
    channels: BTreeMap<String, u16>,
    schema: u16,
    uncompressed_bytes: u64,
    opened_at: Instant,
}

/// One account writer; callers own the bounded queue and a single writer thread.
pub(crate) struct ExecutionReplayWriter {
    account: PathBuf,
    account_id: u64,
    recording_id: String,
    context: Value,
    previous_recording_id: Option<String>,
    lease: Option<File>,
    projection: Projection,
    active: Option<ActivePart>,
    part_number: u32,
    last_seq: u64,
    last_time_us: Option<u64>,
    metadata_day: Option<String>,
    executions: BTreeSet<String>,
    sealed: Vec<PartDescriptor>,
    account_bytes: u64,
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
        validate_json(&context, 0, &mut 20_000)?;
        let account = account_directory(root, account_id, true)?;
        let lease = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(child_file(&account, ".writer.lock")?)
            .map_err(io_error)?;
        lease
            .try_lock()
            .map_err(|error| failure("BUSY", error.to_string()))?;
        let catalogs = read_catalogs(&account)?;
        if catalogs
            .iter()
            .any(|catalog| catalog.recordings.contains_key(recording_id))
        {
            return Err(failure("IDENTITY", "recording identity already exists"));
        }
        let account_bytes = account_usage(&account)?;
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
        atomic_json(&previous_path, &json!({"recording_id":recording_id}), 1024)?;
        Ok(Self {
            account,
            account_id,
            recording_id: recording_id.into(),
            context,
            previous_recording_id,
            lease: Some(lease),
            projection: Projection::new(recording_id),
            active: None,
            part_number: 0,
            last_seq: 0,
            last_time_us: None,
            metadata_day: None,
            executions: BTreeSet::new(),
            sealed: Vec::new(),
            account_bytes,
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
        if message_json.len() > MESSAGE_LIMIT {
            return Err(failure("BUDGET", "event exceeds message budget"));
        }
        let message: Value = serde_json::from_str(message_json)
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
        if self.last_time_us.is_some_and(|previous| time_us < previous) {
            return Err(failure(
                "CLOCK_ROLLBACK",
                "local observation clock moved backwards",
            ));
        }
        let day = shanghai_day(time_us)?;
        if self.active.as_ref().is_some_and(|part| {
            part.descriptor.day != day
                || part.uncompressed_bytes
                    + message_json.len() as u64
                    + RECOVERY_METADATA_LIMIT as u64
                    > PART_LIMIT
                || time_us.saturating_sub(part.descriptor.start_us) >= PART_DURATION_US
        }) {
            self.seal_inner(Some(time_us))?;
        }
        if self.active.is_none() {
            self.open_part(&day, time_us, sequence)?;
        }
        // A failed append stops this recording; its unpublished tail is never a checkpoint.
        self.projection.apply(&message)?;
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
            message_json.as_bytes(),
        );
        active.descriptor.last_seq = sequence;
        active.descriptor.end_us = time_us
            .checked_add(1)
            .ok_or_else(|| failure("BUDGET", "time bound overflow"))?;
        active.uncompressed_bytes += message_json.len() as u64;
        self.active = Some(active);
        write_result?;
        self.last_seq = sequence;
        self.last_time_us = Some(time_us);
        let mut changed = false;
        for name in ["execution_id", "associated_execution_id"] {
            if let Some(identity) = message["data"].get(name).and_then(identity_string) {
                changed |= self.executions.insert(identity);
            }
        }
        if changed {
            self.publish_catalog(None)?;
        }
        if should_seal(&message) {
            self.seal_inner(None)?;
        }
        Ok(())
    }

    fn open_part(&mut self, day: &str, time_us: u64, sequence: u64) -> ReplayResult<()> {
        if self.account_bytes >= ACCOUNT_LIMIT - PART_FILE_LIMIT {
            return Err(failure("BUDGET", "account replay storage budget reached"));
        }
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
        };
        let path = child_file(&directory, &format!("{}.partial", descriptor.file_name()))?;
        let file = OpenOptions::new()
            .write(true)
            .read(true)
            .create_new(true)
            .open(&path)
            .map_err(io_error)?;
        let options = mcap::WriteOptions::new()
            .compression(Some(mcap::Compression::Zstd))
            .compression_threads(0)
            .chunk_size(Some(1024 * 1024))
            .profile("axile.execution-replay.v1")
            .library("axile-storage");
        let mut writer = options.create(file).map_err(io_error)?;
        let schema = writer
            .add_schema("AxileExecutionReplay.v1", "jsonschema", SCHEMA.as_bytes())
            .map_err(io_error)?;
        let checkpoint = json!({"schema_version":1,"account_id":self.account_id,"recording_id":self.recording_id,"context":self.context,"projection":self.projection.snapshot()?});
        let bytes = serde_json::to_vec(&checkpoint).map_err(io_error)?;
        if bytes.len() > CHECKPOINT_LIMIT {
            return Err(failure("BUDGET", "checkpoint exceeds message budget"));
        }
        let mut part = ActivePart {
            writer,
            path,
            descriptor,
            channels: BTreeMap::new(),
            schema,
            uncompressed_bytes: bytes.len() as u64,
            opened_at: Instant::now(),
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
        self.metadata_day = Some(day.into());
        self.publish_catalog(None)
    }

    fn publish_catalog(&self, sealed: Option<&PartDescriptor>) -> ReplayResult<()> {
        let Some(day) = &self.metadata_day else {
            return Ok(());
        };
        let directory = day_directory(&self.account, day, true)?;
        let path = child_file(&directory, "catalog.json")?;
        let mut catalog: Catalog = if path.exists() {
            serde_json::from_slice(&read_bounded(&path, CATALOG_LIMIT)?)
                .map_err(|error| failure("CORRUPT", error.to_string()))?
        } else {
            Catalog {
                schema_version: 1,
                day: day.clone(),
                recordings: BTreeMap::new(),
                parts: Vec::new(),
            }
        };
        if catalog.schema_version != 1 || catalog.day != *day {
            return Err(failure("CORRUPT", "catalog identity mismatch"));
        }
        catalog.recordings.insert(
            self.recording_id.clone(),
            CatalogRecording {
                context: self.context.clone(),
                previous_recording_id: self.previous_recording_id.clone(),
                executions: self.executions.clone(),
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
        );
        if let Some(part) = sealed {
            if catalog.parts.len() >= SCAN_FILES {
                return Err(failure("BUDGET", "daily catalog part budget reached"));
            }
            catalog.parts.push(part.clone());
        }
        atomic_json(&path, &catalog, CATALOG_LIMIT)
    }

    fn seal_inner(&mut self, end_us: Option<u64>) -> ReplayResult<()> {
        let Some(mut part) = self.active.take() else {
            return Ok(());
        };
        if part.descriptor.last_seq < part.descriptor.first_seq {
            return Err(failure("STATE", "cannot publish an empty part"));
        }
        if let Some(end) = end_us {
            part.descriptor.end_us = part.descriptor.end_us.max(end);
        }
        part.descriptor.executions = self.executions.clone();
        let recovery = RecoveryPartMeta {
            schema_version: 1,
            account_id: self.account_id,
            previous_recording_id: self.previous_recording_id.clone(),
            descriptor: part.descriptor.clone(),
        };
        let recovery_json = serde_json::to_string(&recovery).map_err(io_error)?;
        let recovery_bytes = recovery_metadata_size(recovery_json.len())?;
        if part.uncompressed_bytes + recovery_bytes as u64 > PART_LIMIT {
            return Err(failure("BUDGET", "part recovery metadata exceeds budget"));
        }
        part.writer
            .write_metadata(&mcap::records::Metadata {
                name: RECOVERY_METADATA_NAME.into(),
                metadata: BTreeMap::from([("json".into(), recovery_json)]),
            })
            .map_err(io_error)?;
        part.writer.finish().map_err(io_error)?;
        let file = part.writer.into_inner();
        file.sync_all().map_err(io_error)?;
        drop(file);
        let bytes = read_bounded(&part.path, PART_FILE_LIMIT as usize)?;
        part.descriptor.sha256 = digest(&bytes);
        part.descriptor.size_bytes = bytes.len() as u64;
        // Validate the actual compressed artifact before exposing the sealed name.
        crate::execution_replay_reader::validate_part_bytes(
            &bytes,
            &part.descriptor,
            self.account_id,
        )?;
        let directory = part
            .path
            .parent()
            .ok_or_else(|| failure("PATH", "part has no parent"))?;
        let sealed = child_file(directory, &part.descriptor.file_name())?;
        if sealed.exists() {
            return Err(failure("IDENTITY", "sealed part already exists"));
        }
        fs::rename(&part.path, &sealed).map_err(io_error)?;
        sync_directory(directory)?;
        self.account_bytes = self
            .account_bytes
            .checked_add(part.descriptor.size_bytes)
            .ok_or_else(|| failure("BUDGET", "account byte counter overflow"))?;
        self.publish_catalog(Some(&part.descriptor))?;
        self.sealed.push(part.descriptor);
        if self.sealed.len() > SCAN_FILES {
            return Err(failure("BUDGET", "recording part budget reached"));
        }
        Ok(())
    }

    pub(crate) fn seal(&mut self) -> ReplayResult<()> {
        self.ensure_open()?;
        let result = self.seal_inner(None);
        if let Err(error) = &result {
            self.error = Some(error.to_string());
        }
        result
    }

    pub(crate) fn poll(&mut self, now_us: u64) -> ReplayResult<()> {
        self.ensure_open()?;
        checked_us(now_us)?;
        if self.last_time_us.is_some_and(|previous| now_us < previous) {
            self.error = Some("local observation clock moved backwards".into());
            return Err(failure(
                "CLOCK_ROLLBACK",
                "local observation clock moved backwards",
            ));
        }
        if self.active.as_ref().is_some_and(|part| {
            part.opened_at.elapsed().as_secs() >= 60
                || now_us.saturating_sub(part.descriptor.start_us) >= PART_DURATION_US
        }) {
            let result = self.seal_inner(Some(now_us));
            if let Err(error) = &result {
                self.error = Some(error.to_string());
            }
            return result;
        }
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> ReplayResult<()> {
        if self.closed {
            return Ok(());
        }
        self.ensure_open()?;
        let result = self.seal_inner(None);
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

fn account_usage(account: &Path) -> ReplayResult<u64> {
    let mut total = 0_u64;
    let mut count = 0;
    let mut directories = vec![account.to_path_buf()];
    while let Some(directory) = directories.pop() {
        for entry in fs::read_dir(&directory).map_err(io_error)? {
            count += 1;
            if count > SCAN_FILES {
                return Err(failure("BUDGET", "account file scan exceeds budget"));
            }
            let entry = entry.map_err(io_error)?;
            let metadata = entry.path().symlink_metadata().map_err(io_error)?;
            if metadata.file_type().is_symlink() {
                return Err(failure("PATH", "replay storage contains symbolic links"));
            }
            if metadata.is_dir() {
                if directory != account {
                    return Err(failure("PATH", "unexpected nested replay directory"));
                }
                directories.push(entry.path());
            } else {
                total = total
                    .checked_add(metadata.len())
                    .ok_or_else(|| failure("BUDGET", "account byte count overflow"))?;
            }
        }
    }
    Ok(total)
}

fn publish_time(message: &Value, default: u64) -> u64 {
    message["data"]
        .get("publish_time_us")
        .and_then(Value::as_u64)
        .unwrap_or(default)
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
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E").unwrap();
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
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E").unwrap();
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
        let manifest = crate::execution_replay_reader::manifest(&directory.0, 7, "E").unwrap();
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
        assert!(validate_json(&unsafe_value, 0, &mut 10).is_err());
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
}
