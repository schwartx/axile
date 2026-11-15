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
pub(super) fn acknowledge(database: &str, request: &Value) -> Result<Value> {
    let receipt = &request["receipt"];
    let batch = super::text(receipt, "batch")?;
    files::identity(batch)?;
    let root = files::root(database)?;
    let directory = root.join("outbox").join(batch);
    let value = files::read_json(&directory.join("manifest.json"))?;
    let manifest = Manifest::parse(&value)?;
    if receipt["manifest_sha256"] != files::manifest_hash(&value)?
        || receipt["source_id"] != manifest.source_id
        || request["source_id"] != manifest.source_id
    {
        return Err(invalid("接收确认与本机数据包不符"));
    }
    let plan = Plan::parse(request)?;
    files::write_json(&directory.join("receipt.json"), receipt)?;
    let now = package::now_us()?;
    let mut removed = json!({"removed_executions":0,"removed_recordings":0});
    if let Some(days) = plan.execution_days {
        let cutoff = now
            .checked_sub(i64::from(days) * 86_400_000_000)
            .ok_or_else(|| invalid("保留范围无效"))?;
        removed["removed_executions"] = super::prune_history(
            database,
            &json!({"cutoff_us":cutoff,"executions":manifest.executions}),
        )?["removed_executions"]
            .clone();
    }
    if let Some(days) = plan.replay_days {
        let cutoff = now
            .checked_sub(i64::from(days) * 86_400_000_000)
            .ok_or_else(|| invalid("保留范围无效"))?;
        removed["removed_recordings"] = json!(prune_recordings(database, &manifest, cutoff)?);
    }
    files::remove_file(&directory.join("data.axile"))?;
    let pending = root.join("pending.json");
    if pending.exists() && files::read_json(&pending)?["batch"] == batch {
        files::remove_file(&pending)?;
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
    Ok(json!({"removed_recordings":prune_recordings(database, &manifest, cutoff)?}))
}
fn prune_recordings(database: &str, manifest: &Manifest, cutoff: i64) -> Result<u64> {
    let base = files::base(database)?;
    let mut removed = 0;
    for (name, entry) in &manifest.files {
        if name.starts_with("replay/") && super::raw::prune(&base, name, entry, cutoff)? {
            removed += 1;
        }
    }
    Ok(removed)
}
