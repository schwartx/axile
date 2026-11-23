//! 待传批次、已收回执与可重复完成的历史清理。
use super::files::{self, invalid, Result};
use super::package::{self, Manifest, Plan};
use serde_json::{json, Value};
use std::fs;
use std::path::Path;

pub(super) fn cancel(database: &str, request: &Value) -> Result<Value> {
    let batch = super::text(request, "batch")?;
    files::identity(batch)?;
    let root = files::root(database)?;
    let directory = root.join("outbox").join(batch);
    files::reject_links(&directory)?;
    // A cancellation removes the pending copy only; confirmed retention evidence remains available.
    files::remove_file(&directory.join("data.axile"))?;
    let pending = root.join("pending.json");
    if pending.exists() && files::read_json(&pending)?["batch"] == batch {
        files::remove_file(&pending)?;
    }
    Ok(json!({"canceled":true}))
}

pub(super) fn prepare(database: &str, request: &Value) -> Result<Value> {
    let root = files::root(database)?;
    let pending = root.join("pending.json");
    let key = super::text(request, "key")?;
    if key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid("无效传送规则摘要"));
    }
    if pending.exists() {
        let previous = files::read_json(&pending)?;
        if previous["key"] == key {
            let batch = super::text(&previous, "batch")?;
            files::identity(batch)?;
            let directory = root.join("outbox").join(batch);
            let manifest = files::read_json(&directory.join("manifest.json"))?;
            Manifest::parse(&manifest)?;
            let receipt_path = directory.join("receipt.json");
            let receipt = if receipt_path.exists() {
                files::read_json(&receipt_path)?
            } else {
                Value::Null
            };
            return Ok(
                json!({"package":directory.join("data.axile"),"batch":batch,"receipt":receipt}),
            );
        }
    }
    let result = package::export(database, request)?;
    if let Err(error) = files::write_json(
        &pending,
        &json!({"key":key,"batch":result["manifest"]["batch"]}),
    ) {
        if let Some(path) = result["package"]
            .as_str()
            .and_then(|p| Path::new(p).parent())
        {
            let _ = fs::remove_dir_all(path);
        }
        return Err(error);
    }
    Ok(json!({"package":result["package"],"batch":result["manifest"]["batch"],"receipt":null}))
}
fn confirmed_manifest(directory: &Path, receipt: &Value, source_id: &Value) -> Result<Manifest> {
    let value = files::read_json(&directory.join("manifest.json"))?;
    let manifest = Manifest::parse(&value)?;
    if receipt["batch"] != manifest.batch
        || receipt["manifest_sha256"] != files::manifest_hash(&value)?
        || receipt["source_id"] != manifest.source_id
        || source_id != &receipt["source_id"]
    {
        return Err(invalid("接收确认与本机数据包不符"));
    }
    Ok(manifest)
}

pub(super) type Confirmed = std::collections::BTreeMap<String, std::collections::BTreeSet<String>>;

pub(super) fn confirmed_direct(
    database: &str,
    source: &str,
    requester: &str,
    plan: &Plan,
) -> Result<(Confirmed, Confirmed)> {
    let mut recordings = Confirmed::new();
    let mut executions = Confirmed::new();
    let outbox = files::root(database)?.join("outbox");
    if !outbox.exists() {
        return Ok((recordings, executions));
    }
    for entry in fs::read_dir(outbox).map_err(invalid)? {
        let directory = entry.map_err(invalid)?.path();
        let receipt = directory.join("receipt.json");
        if !receipt.is_file() {
            continue;
        }
        let manifest =
            confirmed_manifest(&directory, &files::read_json(&receipt)?, &json!(source))?;
        if manifest.format != 3 || manifest.requester != requester {
            continue;
        }
        let queue = directory.join("cleanup-pending.json");
        let saved = if queue.exists() {
            queue
        } else {
            directory.join("cleanup-done.json")
        };
        if !saved.is_file() {
            continue;
        }
        let previous = Plan::parse(&files::read_json(&saved)?)?;
        if previous.replay_days == plan.replay_days {
            for (name, stamp) in manifest.recording_stamps {
                recordings.entry(name).or_default().insert(stamp);
            }
        }
        if previous.execution_days == plan.execution_days {
            for (id, digest) in manifest.execution_digests {
                executions.entry(id).or_default().insert(digest);
            }
        }
    }
    Ok((recordings, executions))
}

pub(super) fn acknowledge(database: &str, request: &Value) -> Result<Value> {
    let receipt = &request["receipt"];
    let batch = super::text(receipt, "batch")?;
    files::identity(batch)?;
    let root = files::root(database)?;
    let directory = root.join("outbox").join(batch);
    let manifest = confirmed_manifest(&directory, receipt, &request["source_id"])?;
    let plan = Plan::parse(request)?;
    let pending = root.join("pending.json");
    let online = pending.exists() && files::read_json(&pending)?["batch"] == batch;
    let queued = directory.join("cleanup-pending.json");
    let done = directory.join("cleanup-done.json");
    files::write_json(&directory.join("receipt.json"), receipt)?;
    let result = if online || manifest.format == 3 || queued.exists() || done.exists() {
        // Persist retention before releasing the outbox slot; confirmation retries reuse this plan.
        if !queued.exists() && !done.exists() {
            files::write_json(
                &queued,
                &json!({"plan": {
                    "execution_days": plan.execution_days, "replay_days": plan.replay_days
                }}),
            )?;
        }
        json!({"removed_executions":0,"removed_recordings":0,"cleanup_pending":queued.exists()})
    } else {
        cleanup(database, &manifest, &plan)?
    };
    files::remove_file(&directory.join("data.axile"))?;
    if online {
        files::remove_file(&pending)?;
    }
    Ok(result)
}

pub(super) fn cleanup_acknowledged(database: &str, request: &Value) -> Result<Value> {
    let batch = super::text(request, "batch")?;
    files::identity(batch)?;
    let directory = files::root(database)?.join("outbox").join(batch);
    let queued = directory.join("cleanup-pending.json");
    if !queued.exists() {
        return Ok(json!({"cleanup_pending":false}));
    }
    let connection = rusqlite::Connection::open(database).map_err(invalid)?;
    if super::busy(&connection)? {
        return Ok(json!({"cleanup_pending":true}));
    }
    let receipt = files::read_json(&directory.join("receipt.json"))?;
    let manifest = confirmed_manifest(&directory, &receipt, &request["source_id"])?;
    let plan = Plan::parse(&files::read_json(&queued)?)?;
    let mut removed = cleanup(database, &manifest, &plan)?;
    if removed["cleanup_pending"] == true {
        return Ok(removed);
    }
    fs::rename(&queued, directory.join("cleanup-done.json")).map_err(invalid)?;
    files::sync_directory(&directory)?;
    removed["cleanup_pending"] = json!(false);
    Ok(removed)
}

fn cleanup(database: &str, manifest: &Manifest, plan: &Plan) -> Result<Value> {
    let now = package::now_us()?;
    let mut removed = json!({"removed_executions":0,"removed_recordings":0});
    if let Some(days) = plan.execution_days {
        let cutoff = now
            .checked_sub(i64::from(days) * 86_400_000_000)
            .ok_or_else(|| invalid("保留范围无效"))?;
        let mut request = json!({"cutoff_us":cutoff,"executions":manifest.executions});
        if manifest.format == 3 {
            request["execution_digests"] = json!(manifest.execution_digests);
        }
        removed["removed_executions"] =
            super::prune_history(database, &request)?["removed_executions"].clone();
    }
    if let Some(days) = plan.replay_days {
        let cutoff = now
            .checked_sub(i64::from(days) * 86_400_000_000)
            .ok_or_else(|| invalid("保留范围无效"))?;
        let (count, pending) = prune_recordings(database, manifest, cutoff)?;
        removed["removed_recordings"] = json!(count);
        removed["cleanup_pending"] = json!(pending);
    }
    Ok(removed)
}
pub(super) fn prune_recordings_request(database: &str, request: &Value) -> Result<Value> {
    let manifest = Manifest::parse(&request["manifest"])?;
    let directory = files::root(database)?.join("outbox").join(&manifest.batch);
    let receipt = files::read_json(&directory.join("receipt.json"))?;
    if receipt["manifest_sha256"] != files::manifest_hash(&request["manifest"])?
        || receipt["source_id"] != manifest.source_id
    {
        return Err(invalid("清理需要已持久化的接收确认"));
    }
    let cutoff = request["cutoff_us"]
        .as_i64()
        .ok_or_else(|| invalid("缺少保留时间"))?;
    Ok(json!({"removed_recordings":prune_recordings(database, &manifest, cutoff)?.0}))
}
fn prune_recordings(database: &str, manifest: &Manifest, cutoff: i64) -> Result<(u64, bool)> {
    let base = files::base(database)?;
    let mut removed = 0;
    let mut pending = false;
    for (name, entry) in &manifest.files {
        if !name.starts_with("replay/") {
            continue;
        }
        if manifest.format == 3 {
            let (deleted, more) = super::raw::prune_live(&base, name, entry, cutoff)?;
            removed += u64::from(deleted);
            pending |= more;
        } else {
            removed += u64::from(super::raw::prune(&base, name, entry, cutoff)?);
        }
    }
    Ok((removed, pending))
}
