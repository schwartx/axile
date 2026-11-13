//! 待传批次、已收回执与可重复完成的历史清理。
use super::files::{self, invalid, Result};
use super::package::{self, Manifest, Plan};
use chrono::{TimeZone, Utc};
use chrono_tz::Asia::Shanghai;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

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
    let cutoff = request["cutoff_us"]
        .as_i64()
        .ok_or_else(|| invalid("缺少保留时间"))?;
    Ok(json!({"removed_recordings":prune_recordings(database, &manifest, cutoff)?}))
}
fn prune_recordings(database: &str, manifest: &Manifest, cutoff: i64) -> Result<u64> {
    let base = files::base(database)?;
    let day = Utc
        .timestamp_micros(cutoff)
        .single()
        .ok_or_else(|| invalid("保留范围无效"))?
        .with_timezone(&Shanghai)
        .format("%Y-%m-%d")
        .to_string();
    let mut recordings = BTreeSet::new();
    for name in manifest.files.keys() {
        let path = Path::new(name);
        let Some(file_name) = path.file_name().and_then(|v| v.to_str()) else {
            continue;
        };
        if !name.starts_with("replay/") || !file_name.starts_with("recording-") {
            continue;
        }
        if let Some(recording) = recording_id(file_name) {
            recordings.insert((
                path.parent().unwrap().parent().unwrap().to_owned(),
                recording.to_owned(),
            ));
        }
    }
    let mut removed = 0;
    for (relative, recording) in recordings {
        let account = base.join(&relative);
        files::reject_links(&account)?;
        let files_to_remove = recording_files(&account, &recording)?;
        if files_to_remove.is_empty()
            || !confirmed_recording(&base, manifest, &files_to_remove, &day)?
        {
            continue;
        }
        if super::prune_recording(
            database,
            &json!({"index":account.join(".replay-index.sqlite"),"recording":recording}),
        )?["safe"]
            != true
        {
            continue;
        }
        for path in files_to_remove {
            files::remove_file(&path)?;
            removed += 1;
        }
    }
    Ok(removed)
}

fn recording_files(account: &Path, recording: &str) -> Result<Vec<PathBuf>> {
    let mut files_to_remove = Vec::new();
    let mut active = false;
    for entry in fs::read_dir(&account).map_err(invalid)? {
        let entry = entry.map_err(invalid)?;
        if entry.file_type().map_err(invalid)?.is_symlink() {
            active = true;
            continue;
        }
        if !entry.file_type().map_err(invalid)?.is_dir() {
            continue;
        }
        for entry in fs::read_dir(entry.path()).map_err(invalid)? {
            let entry = entry.map_err(invalid)?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.ends_with(".partial") {
                active = true;
            }
            if recording_id(&name) == Some(recording) {
                files_to_remove.push(entry.path());
            }
        }
    }
    Ok(if active { Vec::new() } else { files_to_remove })
}
fn confirmed_recording(
    base: &Path,
    manifest: &Manifest,
    candidates: &[PathBuf],
    day: &str,
) -> Result<bool> {
    for path in candidates {
        let name = files::archive_name(path.strip_prefix(&base).map_err(invalid)?)?;
        let Some(info) = manifest.files.get(&name) else {
            return Ok(false);
        };
        if path
            .parent()
            .unwrap()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .as_ref()
            >= day
            || fs::symlink_metadata(path)
                .map_err(invalid)?
                .file_type()
                .is_symlink()
        {
            return Ok(false);
        }
        if files::digest(path)? != info.sha256 {
            return Ok(false);
        }
    }
    Ok(true)
}

fn recording_id(name: &str) -> Option<&str> {
    let stem = name.strip_suffix(".mcap")?;
    let (prefix, fragment) = match stem.strip_prefix("recording-") {
        Some(value) => (value, false),
        None => (stem.strip_prefix("fragment-")?, true),
    };
    let (recording, part) = prefix.rsplit_once('-')?;
    if part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    if !fragment {
        return Some(recording);
    }
    let (recording, sequence) = recording.rsplit_once('-')?;
    (!sequence.is_empty() && sequence.bytes().all(|byte| byte.is_ascii_digit()))
        .then_some(recording)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recording_identity_does_not_match_a_prefix() {
        assert_eq!(recording_id("recording-a-000001.mcap"), Some("a"));
        assert_eq!(recording_id("recording-a-12-000001.mcap"), Some("a-12"));
        assert_eq!(recording_id("fragment-a-12-1-000000.mcap"), Some("a-12"));
        assert_eq!(recording_id("recording-a-000001.py"), None);
        assert_eq!(recording_id("recording-a-000001.mcap.partial"), None);
    }
}
