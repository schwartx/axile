//! 在线迁移直接读取已封口的原始录制，发送端只落盘回执清单。
use super::files::{self, invalid, Result};
use super::package::{self, Entry, Manifest, Plan};
use base64::{engine::general_purpose::STANDARD, Engine};
use rusqlite::{params, Connection, DatabaseName, OpenFlags};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

fn limits(plan: &Plan, days: Option<u32>, now: i64) -> (i64, i64) {
    let start = plan.start.map(|time| time.timestamp_micros()).unwrap_or(0);
    let end = days
        .map(|days| now - i64::from(days) * 86_400_000_000)
        .unwrap_or(now);
    (
        start,
        end.min(plan.end.map(|time| time.timestamp_micros()).unwrap_or(now)),
    )
}

const RECORD_TABLES: &[&str] = &[
    "business_asset_binding",
    "business_asset_snapshot",
    "business_trade",
    "business_trade_collection",
    "business_trade_conversion",
    "execution_extension",
    "execution_order",
    "execution_order_collection",
    "execution_symbol_collection",
    "execution_symbol_conversion",
    "execution_symbol_result",
    "execution_symbol_sizing",
    "execution_symbol_target",
    "execution_symbol_tick",
    "execution_termination_cancel_failure",
];

pub(super) fn execution_digest(connection: &Connection, schema: &str, id: &str) -> Result<String> {
    use rusqlite::types::ValueRef;
    let mut hash = Sha256::new();
    for (table, filter) in RECORD_TABLES
        .iter()
        .map(|table| (*table, "record_id IN (SELECT id FROM owner)"))
        .chain(
            [
                "executerecord",
                "execution_event",
                "execution_artifact",
                "execution_intent",
            ]
            .map(|table| (table, "execution_id=?1")),
        )
        .chain([(
            "business_asset_position",
            "asset_id IN (SELECT asset_id FROM assets)",
        )])
    {
        hash.update(table.as_bytes());
        let sql = format!("WITH owner AS (SELECT id FROM {schema}.executerecord WHERE execution_id=?1),
                           assets AS (SELECT asset_id FROM {schema}.business_asset_snapshot WHERE record_id IN (SELECT id FROM owner))
                           SELECT * FROM {schema}.{table} WHERE {filter}");
        let statement = connection.prepare(&sql).map_err(invalid)?;
        let columns = statement.column_count();
        let order = (1..=columns)
            .map(|column| column.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let mut statement = connection
            .prepare(&format!("{sql} ORDER BY {order}"))
            .map_err(invalid)?;
        let mut rows = statement.query([id]).map_err(invalid)?;
        while let Some(row) = rows.next().map_err(invalid)? {
            for column in 0..columns {
                match row.get_ref(column).map_err(invalid)? {
                    ValueRef::Null => hash.update([0]),
                    ValueRef::Integer(value) => {
                        hash.update([1]);
                        hash.update(value.to_le_bytes());
                    }
                    ValueRef::Real(value) => {
                        hash.update([2]);
                        hash.update(value.to_le_bytes());
                    }
                    value @ (ValueRef::Text(_) | ValueRef::Blob(_)) => {
                        let bytes = value.as_bytes().map_err(invalid)?;
                        hash.update([if matches!(value, ValueRef::Text(_)) {
                            3
                        } else {
                            4
                        }]);
                        hash.update((bytes.len() as u64).to_le_bytes());
                        hash.update(bytes);
                    }
                }
            }
        }
        hash.update([255]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn recordings(
    base: &Path,
    start: i64,
    end: i64,
    confirmed: &BTreeMap<String, BTreeSet<String>>,
) -> Result<(BTreeMap<String, Entry>, BTreeMap<String, String>)> {
    let mut paths = BTreeMap::new();
    let mut stamps = BTreeMap::new();
    let replay = base.join("replay");
    if start >= end || !replay.is_dir() {
        return Ok((paths, stamps));
    }
    for folder in std::fs::read_dir(&replay).map_err(invalid)? {
        let folder = folder.map_err(invalid)?;
        let account = folder.file_name().to_string_lossy().into_owned();
        if account
            .strip_prefix("account-")
            .and_then(|id| id.parse::<u64>().ok())
            .is_none()
            || !folder.file_type().map_err(invalid)?.is_dir()
        {
            continue;
        }
        let index = folder.path().join(".replay-v2.sqlite");
        if !index.is_file() {
            continue;
        }
        let connection = Connection::open_with_flags(index, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(invalid)?;
        let mut statement = connection.prepare(
            "SELECT id FROM recordings r WHERE state='closed' AND seq>0 AND watermark>=?1 AND watermark<?2
             AND NOT EXISTS(SELECT 1 FROM content_refs WHERE recording<>target AND (recording=r.id OR target=r.id))
             ORDER BY watermark,id"
        ).map_err(invalid)?;
        for id in statement
            .query_map(params![start, end], |row| row.get::<_, String>(0))
            .map_err(invalid)?
        {
            let id = id.map_err(invalid)?;
            let name = format!("replay/{account}/raw-{id}.mcap");
            package::validate_name(&name)?;
            let path = base.join(&name);
            files::reject_links(&path)?;
            let metadata = path.metadata().map_err(invalid)?;
            let size = metadata.len();
            let modified = metadata
                .modified()
                .map_err(invalid)?
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(invalid)?;
            let stamp = format!("{size}:{}", modified.as_nanos());
            if confirmed
                .get(&name)
                .is_some_and(|stamps| stamps.contains(&stamp))
            {
                continue;
            }
            let sha256 = files::digest(&path)?;
            stamps.insert(name.clone(), stamp);
            paths.insert(name, Entry { size, sha256 });
            break;
        }
    }
    Ok((paths, stamps))
}

fn payload(
    database: &str,
    start: i64,
    end: i64,
    retention: bool,
    confirmed: &BTreeMap<String, BTreeSet<String>>,
) -> Result<(Vec<u8>, BTreeMap<String, String>)> {
    let connection = Connection::open_in_memory().map_err(invalid)?;
    connection
        .execute("ATTACH DATABASE ?1 AS source", [database])
        .map_err(invalid)?;
    connection.execute_batch("PRAGMA foreign_keys=OFF; PRAGMA max_page_count=8192; BEGIN; CREATE TEMP TABLE selected(id INTEGER PRIMARY KEY, execution_id TEXT);
        CREATE TEMP TABLE inputs(id TEXT PRIMARY KEY)").map_err(invalid)?;
    let mut candidates = connection.prepare(
        "SELECT id,execution_id FROM source.executerecord r
         WHERE execution_id IS NOT NULL AND created_at_us>=?1 AND created_at_us<?2
         AND (NOT ?3 OR id<(SELECT max(id) FROM source.executerecord))
         AND NOT EXISTS(SELECT 1 FROM source.execution_intent i WHERE i.execution_id=r.execution_id AND i.finished_at_us IS NULL)
         ORDER BY id"
    ).map_err(invalid)?;
    let mut revisions = BTreeMap::new();
    for candidate in candidates
        .query_map(params![start, end, retention], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(invalid)?
    {
        let (row, id) = candidate.map_err(invalid)?;
        let digest = execution_digest(&connection, "source", &id)?;
        if confirmed
            .get(&id)
            .is_some_and(|digests| digests.contains(&digest))
        {
            continue;
        }
        connection
            .execute("INSERT INTO selected VALUES (?1,?2)", params![row, id])
            .map_err(invalid)?;
        revisions.insert(id, digest);
        if revisions.len() == 32 {
            break;
        }
    }
    drop(candidates);
    connection.execute_batch(
        "INSERT OR IGNORE INTO inputs SELECT input_id FROM source.executerecord JOIN selected USING(id) WHERE input_id IS NOT NULL;
         INSERT OR IGNORE INTO inputs SELECT result_input_id FROM source.executerecord JOIN selected USING(id) WHERE result_input_id IS NOT NULL;
         INSERT OR IGNORE INTO inputs SELECT id FROM source.execution_input WHERE owner_execution_id IN (SELECT execution_id FROM selected)"
    ).map_err(invalid)?;
    let tables = super::tables(&connection, "source")?;
    for table in &tables {
        let schema: String = connection
            .query_row(
                "SELECT sql FROM source.sqlite_master WHERE type='table' AND name=?1",
                [table],
                |row| row.get(0),
            )
            .map_err(invalid)?;
        connection.execute_batch(&schema).map_err(invalid)?;
    }
    for table in tables {
        let filter = match table.as_str() {
            "account" | "account_settings" | "portfolio" | "portfolioaccount" => "1",
            "executerecord" => "id IN (SELECT id FROM selected)",
            "execution_intent" | "execution_event" | "execution_artifact" | "target_weight_snapshot" =>
                "execution_id IN (SELECT execution_id FROM selected)",
            "execution_input" => "id IN (SELECT id FROM inputs)",
            "execution_input_plan" | "execution_input_symbol" | "execution_input_target" =>
                "input_id IN (SELECT id FROM inputs)",
            "target_weight_snapshot_weight" =>
                "snapshot_id IN (SELECT id FROM source.target_weight_snapshot WHERE execution_id IN (SELECT execution_id FROM selected))",
            "account_asset_snapshot" =>
                "execution_id IN (SELECT execution_id FROM selected)",
            "business_asset_snapshot" =>
                "record_id IN (SELECT id FROM selected) OR input_id IN (SELECT id FROM inputs)
                 OR account_snapshot_id IN (SELECT id FROM source.account_asset_snapshot WHERE execution_id IN (SELECT execution_id FROM selected))",
            "business_asset_position" =>
                "asset_id IN (SELECT asset_id FROM source.business_asset_snapshot
                 WHERE record_id IN (SELECT id FROM selected) OR input_id IN (SELECT id FROM inputs)
                 OR account_snapshot_id IN (SELECT id FROM source.account_asset_snapshot WHERE execution_id IN (SELECT execution_id FROM selected)))",
            table if RECORD_TABLES.contains(&table) => "record_id IN (SELECT id FROM selected)",
            _ => continue,
        };
        let table = super::quote(&table);
        connection
            .execute_batch(&format!(
                "INSERT INTO main.{table} SELECT * FROM source.{table} WHERE {filter}"
            ))
            .map_err(invalid)?;
    }
    connection.execute_batch("COMMIT").map_err(invalid)?;
    let missing: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_foreign_key_check)",
            [],
            |row| row.get(0),
        )
        .map_err(invalid)?;
    if missing {
        return Err(invalid("迁移记录缺少关联数据"));
    }
    let data = connection
        .serialize(DatabaseName::Main)
        .map_err(invalid)?
        .to_vec();
    Ok((data, revisions))
}

pub(super) fn prepare(database: &str, request: &Value) -> Result<Value> {
    let plan = Plan::parse(request)?;
    let now = package::now_us()?;
    let source = super::text(request, "source_id")?;
    let base = files::base(database)?;
    let requester = request["requester"].as_str().unwrap_or_default();
    if !requester.is_empty() {
        files::identity(requester)?;
    }
    let (confirmed_recordings, confirmed_executions) =
        super::state::confirmed_direct(database, source, requester, &plan)?;
    let (start, end) = limits(&plan, plan.replay_days, now);
    let (mut entries, recording_stamps) = recordings(&base, start, end, &confirmed_recordings)?;
    let (start, end) = limits(&plan, plan.execution_days, now);
    let (data, execution_digests) = payload(
        database,
        start,
        end,
        plan.execution_days.is_some(),
        &confirmed_executions,
    )?;
    if entries.is_empty() && execution_digests.is_empty() {
        return Ok(json!({"manifest":null}));
    }
    entries.insert(
        "database.sqlite3".to_owned(),
        Entry {
            size: data.len() as u64,
            sha256: format!("{:x}", Sha256::digest(&data)),
        },
    );
    let batch = uuid::Uuid::new_v4().simple().to_string();
    let manifest = json!(Manifest {
        format: 3,
        batch: batch.clone(),
        source_id: source.to_owned(),
        start: None,
        end: chrono::DateTime::from_timestamp_micros(now)
            .ok_or_else(|| invalid("无效迁移时间"))?
            .to_rfc3339(),
        executions: execution_digests.keys().cloned().collect(),
        execution_digests,
        recording_stamps,
        requester: requester.to_owned(),
        files: entries
    });
    let directory = files::root(database)?.join("outbox").join(batch);
    files::mkdir(&directory)?;
    files::write_json(&directory.join("manifest.json"), &manifest)?;
    Ok(json!({"manifest":manifest,"database":STANDARD.encode(data)}))
}

pub(super) fn file(database: &str, request: &Value) -> Result<Value> {
    let batch = super::text(request, "batch")?;
    let name = super::text(request, "name")?;
    files::identity(batch)?;
    package::validate_name(name)?;
    let value = files::read_json(
        &files::root(database)?
            .join("outbox")
            .join(batch)
            .join("manifest.json"),
    )?;
    let manifest = Manifest::parse(&value)?;
    if manifest.format != 3 || !name.starts_with("replay/") || !manifest.files.contains_key(name) {
        return Err(invalid("录制未列在迁移清单"));
    }
    let path = files::base(database)?.join(name);
    files::reject_links(&path)?;
    if path.metadata().map_err(invalid)?.len() != manifest.files[name].size {
        return Err(invalid("源录制大小已变化"));
    }
    Ok(json!({"path":path}))
}

pub(super) fn receive(database: &str, request: &Value) -> Result<Value> {
    let value = request["manifest"].clone();
    let manifest = Manifest::parse(&value)?;
    if manifest.format != 3
        || manifest
            .files
            .keys()
            .any(|name| name != "database.sqlite3" && !name.starts_with("replay/"))
    {
        return Err(invalid("无效在线迁移格式"));
    }
    let staging = Path::new(super::text(request, "staging")?);
    for (name, entry) in &manifest.files {
        let path = staging.join(name);
        files::reject_links(&path)?;
        if path.metadata().map_err(invalid)?.len() != entry.size
            || files::digest(&path)? != entry.sha256
        {
            if name.starts_with("replay/") {
                return Ok(json!({"corrupt_recording":name}));
            }
            return Err(invalid("迁移文件校验失败"));
        }
        std::fs::File::open(&path)
            .and_then(|file| file.sync_all())
            .map_err(invalid)?;
        files::sync_directory(path.parent().unwrap())?;
    }
    if request["rebuild"] == true {
        return package::install(database, staging, value, manifest);
    }
    if !super::raw::install_direct(database, &files::base(database)?, staging, &manifest)? {
        return Ok(json!({"rebuild_required":true}));
    }
    let receipt = package::record_receipt(&files::root(database)?, &value, &manifest)?;
    Ok(json!({"receipt":receipt}))
}
