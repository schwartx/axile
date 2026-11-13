//! v1 ZIP 包的快照、文件选择、流式校验与接收落盘。
use super::files::{self, invalid, Result, MAX_BYTES, MAX_FILES};
use chrono::{DateTime, FixedOffset, SecondsFormat, TimeZone, Utc};
use chrono_tz::Asia::Shanghai;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipArchive, ZipWriter};

#[derive(Default, Deserialize)]
pub(super) struct Plan {
    pub start: Option<DateTime<FixedOffset>>,
    pub end: Option<DateTime<FixedOffset>>,
    pub execution_days: Option<u32>,
    pub replay_days: Option<u32>,
}
impl Plan {
    pub fn parse(request: &Value) -> Result<Self> {
        let plan: Self = serde_json::from_value(request["plan"].clone()).map_err(invalid)?;
        if matches!(plan.execution_days, Some(0)) || matches!(plan.replay_days, Some(0)) {
            return Err(invalid("保留天数必须大于零"));
        }
        Ok(plan)
    }
}
pub(super) fn now_us() -> Result<i64> {
    let time = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(invalid)?;
    i64::try_from(time.as_micros()).map_err(invalid)
}
fn iso(time: DateTime<FixedOffset>) -> String {
    time.to_rfc3339_opts(
        if time.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        false,
    )
}
#[derive(Clone, Deserialize, Serialize)]
pub(super) struct Entry {
    pub size: u64,
    pub sha256: String,
}
#[derive(Deserialize, Serialize)]
pub(super) struct Manifest {
    pub format: u32,
    pub batch: String,
    pub source_id: String,
    pub start: Option<String>,
    pub end: String,
    pub executions: Vec<String>,
    pub files: BTreeMap<String, Entry>,
}
impl Manifest {
    pub fn parse(value: &Value) -> Result<Self> {
        let manifest: Self = serde_json::from_value(value.clone()).map_err(invalid)?;
        files::identity(&manifest.batch)?;
        files::identity(&manifest.source_id)?;
        if manifest.format != 1
            || !manifest.files.contains_key("database.sqlite3")
            || manifest.files.len() > MAX_FILES
        {
            return Err(invalid("不支持的数据包或文件数超过预算"));
        }
        let mut size = 0u64;
        for (name, entry) in &manifest.files {
            validate_name(name)?;
            size = size
                .checked_add(entry.size)
                .ok_or_else(|| invalid("数据包超过预算"))?;
            if size > MAX_BYTES
                || entry.sha256.len() != 64
                || !entry
                    .sha256
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err(invalid("数据包超过预算或哈希无效"));
            }
        }
        Ok(manifest)
    }
}
fn validate_name(name: &str) -> Result<()> {
    let parts = name.split('/').collect::<Vec<_>>();
    if name.contains('\\')
        || name.contains('\0')
        || parts
            .iter()
            .any(|p| p.is_empty() || *p == "." || *p == "..")
    {
        return Err(invalid("无效数据包路径"));
    }
    let valid = name == "database.sqlite3"
        || (parts.len() >= 3
            && parts[0] == "code"
            && !parts[1].is_empty()
            && parts[1].bytes().all(|b| b.is_ascii_digit()))
        || (parts.len() == 3
            && parts[0] == "replay"
            && parts[1].starts_with("account-")
            && parts[2] == ".replay-index.sqlite")
        || (parts.len() == 4
            && parts[0] == "replay"
            && parts[1].starts_with("account-")
            && parts[3].ends_with(".mcap"));
    if !valid {
        return Err(invalid("无效数据包路径"));
    }
    Ok(())
}
fn children(path: &Path) -> Result<Vec<PathBuf>> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let mut result = Vec::new();
    for child in fs::read_dir(path).map_err(invalid)? {
        let child = child.map_err(invalid)?;
        if !child.file_type().map_err(invalid)?.is_symlink() {
            result.push(child.path());
        }
    }
    result.sort();
    Ok(result)
}
fn code_files(
    root: &Path,
    current: &Path,
    prefix: &str,
    files: &mut BTreeMap<String, PathBuf>,
) -> Result<()> {
    for path in children(current)? {
        let name = path
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or_else(|| invalid("算法文件名不是 UTF-8"))?;
        if name.starts_with('.') || name == "__pycache__" {
            continue;
        }
        if path.is_dir() {
            code_files(root, &path, prefix, files)?;
        } else if path.is_file() {
            let relative = files::archive_name(path.strip_prefix(root).map_err(invalid)?)?;
            files.insert(format!("code/{prefix}/{relative}"), path);
        }
        if files.len() > MAX_FILES {
            return Err(invalid("文件数超过预算"));
        }
    }
    Ok(())
}
fn collect(
    base: &Path,
    directory: &Path,
    plan: &Plan,
    end_time: DateTime<FixedOffset>,
    request: &Value,
) -> Result<BTreeMap<String, PathBuf>> {
    let start = plan
        .start
        .map(|t| t.with_timezone(&Shanghai).format("%Y-%m-%d").to_string())
        .unwrap_or_default();
    let end = end_time
        .with_timezone(&Shanghai)
        .format("%Y-%m-%d")
        .to_string();
    let mut files = BTreeMap::from([(
        "database.sqlite3".to_owned(),
        directory.join("database.sqlite3"),
    )]);
    for account in children(&base.join("replay"))? {
        if !account.is_dir()
            || !account
                .file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("account-")
        {
            continue;
        }
        let index = account.join(".replay-index.sqlite");
        if index.is_file() {
            files::reject_links(&index)?;
            let relative = index.strip_prefix(base).map_err(invalid)?;
            let copy = directory.join(relative);
            files::mkdir(copy.parent().unwrap())?;
            super::snapshot_file(&index, &copy)?;
            files.insert(files::archive_name(relative)?, copy);
        }
        for day in children(&account)? {
            if !day.is_dir() {
                continue;
            }
            let date = day.file_name().unwrap().to_string_lossy();
            for path in children(&day)? {
                let name = path.file_name().unwrap().to_string_lossy();
                if path.is_file()
                    && name.ends_with(".mcap")
                    && (name.starts_with("fragment-")
                        || (start.as_str() <= date.as_ref() && date.as_ref() <= end.as_str()))
                {
                    files.insert(
                        files::archive_name(path.strip_prefix(base).map_err(invalid)?)?,
                        path,
                    );
                }
            }
        }
    }
    let directories = request["algorithm_directories"]
        .as_array()
        .ok_or_else(|| invalid("缺少算法目录"))?;
    for (number, value) in directories.iter().enumerate() {
        let path = Path::new(value.as_str().ok_or_else(|| invalid("算法目录无效"))?);
        if path.exists() {
            code_files(
                &path.canonicalize().map_err(invalid)?,
                &path.canonicalize().map_err(invalid)?,
                &number.to_string(),
                &mut files,
            )?;
        }
    }
    Ok(files)
}
pub(super) fn export(database: &str, request: &Value) -> Result<Value> {
    let plan = Plan::parse(request)?;
    let now = now_us()?;
    let end = plan.end.map(|t| t.timestamp_micros()).unwrap_or(now);
    if end > now || plan.start.is_some_and(|t| t.timestamp_micros() >= end) {
        return Err(invalid("传送时间范围无效"));
    }
    let source = super::text(request, "source_id")?;
    files::identity(source)?;
    let base = files::base(database)?;
    let outbox = files::root(database)?.join("outbox");
    files::mkdir(&outbox)?;
    if fs2::available_space(&base).map_err(invalid)?
        < fs::metadata(database)
            .map_err(invalid)?
            .len()
            .saturating_add(256 * 1024 * 1024)
    {
        return Err(invalid("剩余磁盘不足以暂存快照"));
    }
    let temporary = tempfile::Builder::new()
        .prefix(".export-")
        .tempdir_in(&outbox)
        .map_err(invalid)?;
    let directory = temporary.path();
    let snapshot = directory.join("database.sqlite3");
    let report = super::export_snapshot(
        database,
        &json!({"snapshot":snapshot,"start_us":plan.start.map(|t|t.timestamp_micros()).unwrap_or(0),"end_us":end}),
    )?;
    let end_time = plan
        .end
        .unwrap_or(Utc.timestamp_micros(end).unwrap().fixed_offset());
    let paths = collect(&base, directory, &plan, end_time, request)?;
    let mut entries = BTreeMap::new();
    let mut size = 0u64;
    for (name, path) in &paths {
        validate_name(name)?;
        size = size
            .checked_add(fs::metadata(path).map_err(invalid)?.len())
            .ok_or_else(|| invalid("数据包超过预算"))?;
        if size > MAX_BYTES || paths.len() > MAX_FILES {
            return Err(invalid("数据包超过预算，请缩短范围"));
        }
        entries.insert(
            name.clone(),
            Entry {
                size: fs::metadata(path).map_err(invalid)?.len(),
                sha256: files::digest(path)?,
            },
        );
    }
    if fs2::available_space(&base).map_err(invalid)? < size.saturating_add(256 * 1024 * 1024) {
        return Err(invalid("剩余磁盘不足以暂存数据包"));
    }
    let batch = uuid::Uuid::new_v4().simple().to_string();
    let manifest = serde_json::to_value(Manifest {
        format: 1,
        batch: batch.clone(),
        source_id: source.to_owned(),
        start: plan.start.map(iso),
        end: iso(end_time),
        executions: serde_json::from_value(report["executions"].clone()).map_err(invalid)?,
        files: entries,
    })
    .map_err(invalid)?;
    if serde_json::to_vec(&manifest).map_err(invalid)?.len() > 16 * 1024 * 1024 {
        return Err(invalid("清单超过预算，请缩短范围"));
    }
    write_archive(&directory.join("data.axile"), &paths, &manifest)?;
    files::write_json(&directory.join("manifest.json"), &manifest)?;
    fs::remove_file(snapshot).map_err(invalid)?;
    if directory.join("replay").exists() {
        fs::remove_dir_all(directory.join("replay")).map_err(invalid)?;
    }
    files::sync_directory(directory)?;
    let final_directory = outbox.join(&batch);
    if final_directory.exists() {
        return Err(invalid("数据包身份冲突"));
    }
    fs::rename(directory, &final_directory).map_err(invalid)?;
    files::sync_directory(&outbox)?;
    Ok(json!({"package":final_directory.join("data.axile"),"manifest":manifest,"receipt":null}))
}
fn write_archive(path: &Path, files: &BTreeMap<String, PathBuf>, manifest: &Value) -> Result<()> {
    let output = tempfile::NamedTempFile::new_in(path.parent().unwrap()).map_err(invalid)?;
    let mut archive = ZipWriter::new(output.reopen().map_err(invalid)?);
    for (name, source) in files {
        let mut input = File::open(source).map_err(invalid)?;
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .unix_permissions(0o600)
            .large_file(input.metadata().map_err(invalid)?.len() >= u32::MAX as u64);
        archive.start_file(name, options).map_err(invalid)?;
        std::io::copy(&mut input, &mut archive).map_err(invalid)?;
    }
    archive
        .start_file(
            "manifest.json",
            SimpleFileOptions::default().compression_method(CompressionMethod::Deflated),
        )
        .map_err(invalid)?;
    archive
        .write_all(&serde_json::to_vec(manifest).map_err(invalid)?)
        .map_err(invalid)?;
    archive
        .finish()
        .map_err(invalid)?
        .sync_all()
        .map_err(invalid)?;
    output.persist(path).map_err(invalid)?;
    files::sync_directory(path.parent().unwrap())
}
fn unpack(package: &Path, directory: &Path) -> Result<(Value, Manifest)> {
    let mut archive = ZipArchive::new(File::open(package).map_err(invalid)?).map_err(invalid)?;
    if archive.len() > MAX_FILES + 1 {
        return Err(invalid("文件数超过预算"));
    }
    archive.validate_header_consistency().map_err(invalid)?;
    let mut raw = Vec::new();
    {
        let manifest = archive.by_name("manifest.json").map_err(invalid)?;
        if manifest.size() > 16 * 1024 * 1024 {
            return Err(invalid("清单超过预算"));
        }
        manifest
            .take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut raw)
            .map_err(invalid)?;
        if raw.len() > 16 * 1024 * 1024 {
            return Err(invalid("清单超过预算"));
        }
    }
    let value: Value = serde_json::from_slice(&raw).map_err(invalid)?;
    let manifest = Manifest::parse(&value)?;
    if archive.len() != manifest.files.len() + 1 {
        return Err(invalid("数据包文件清单不一致"));
    }
    let mut names = BTreeSet::new();
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(invalid)?;
        let name = entry.name().map_err(invalid)?.into_owned();
        if !names.insert(name.clone()) {
            return Err(invalid("数据包路径重复"));
        }
        if name == "manifest.json" {
            continue;
        }
        let info = manifest
            .files
            .get(&name)
            .ok_or_else(|| invalid("数据包文件清单不一致"))?;
        if entry.size() != info.size || entry.is_dir() || entry.is_symlink() {
            return Err(invalid("文件大小或类型与清单不符"));
        }
        let path = directory.join(&name);
        files::mkdir(path.parent().unwrap())?;
        let mut destination =
            tempfile::NamedTempFile::new_in(path.parent().unwrap()).map_err(invalid)?;
        let bytes = std::io::copy(
            &mut (&mut entry).take(info.size + 1),
            destination.as_file_mut(),
        )
        .map_err(invalid)?;
        destination.as_file().sync_all().map_err(invalid)?;
        if bytes != info.size || files::digest(destination.path())? != info.sha256 {
            return Err(invalid("文件校验失败"));
        }
        destination.persist(&path).map_err(invalid)?;
        files::sync_directory(path.parent().unwrap())?;
    }
    Ok((value, manifest))
}
fn destination(base: &Path, root: &Path, manifest: &Manifest, name: &str) -> PathBuf {
    match name.strip_prefix("code/") {
        Some(relative) => root
            .join("algorithms")
            .join(&manifest.source_id)
            .join(&manifest.batch)
            .join(relative),
        None => base.join(name),
    }
}
pub(super) fn import(database: &str, request: &Value) -> Result<Value> {
    let root = files::root(database)?;
    let base = files::base(database)?;
    let temporary = tempfile::Builder::new()
        .prefix("receive-")
        .tempdir_in(&root)
        .map_err(invalid)?;
    let (value, manifest) = unpack(
        Path::new(super::text(request, "package")?),
        temporary.path(),
    )?;
    for (name, entry) in &manifest.files {
        if name == "database.sqlite3" {
            continue;
        }
        let target = destination(&base, &root, &manifest, name);
        files::reject_links(&target)?;
        if target.exists()
            && !name.ends_with("/.replay-index.sqlite")
            && files::digest(&target)? != entry.sha256
        {
            return Err(invalid("同名录制内容冲突"));
        }
    }
    super::import_snapshot(
        database,
        &json!({"source_id":manifest.source_id,"snapshot":temporary.path().join("database.sqlite3")}),
    )?;
    let mut code_roots = BTreeSet::new();
    for name in manifest.files.keys() {
        if name == "database.sqlite3" {
            continue;
        }
        let target = destination(&base, &root, &manifest, name);
        files::mkdir(target.parent().unwrap())?;
        if name.ends_with("/.replay-index.sqlite") {
            super::import_index(
                target.to_str().ok_or_else(|| invalid("无效索引路径"))?,
                &json!({"snapshot":temporary.path().join(name)}),
            )?;
        } else if !target.exists() {
            fs::rename(temporary.path().join(name), &target).map_err(invalid)?;
            files::sync_directory(target.parent().unwrap())?;
        }
        if name.starts_with("code/") {
            code_roots.insert(
                root.join("algorithms")
                    .join(&manifest.source_id)
                    .join(&manifest.batch)
                    .join(name.split('/').nth(1).unwrap()),
            );
        }
    }
    let receipt = json!({"batch":manifest.batch,"source_id":manifest.source_id,"manifest_sha256":files::manifest_hash(&value)?});
    files::write_json(
        &root.join(format!("receipt-{}.json", manifest.batch)),
        &receipt,
    )?;
    Ok(json!({"receipt":receipt,"algorithm_directories":code_roots,"source_id":manifest.source_id}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn manifest_rejects_paths_and_expansion_budget() {
        let mut value = json!({"format":1,"batch":"a".repeat(32),"source_id":"b".repeat(32),"start":null,"end":"2026-01-01T00:00:00+00:00","executions":[],"files":{"database.sqlite3":{"sha256":"c".repeat(64),"size":0}}});
        assert!(Manifest::parse(&value).is_ok());
        for name in [
            "../outside",
            "code/0/../../outside",
            "code/0/./a",
            "code/0//a",
            "replay/account-1/../../a.mcap",
            "C:\\outside",
        ] {
            value["files"][name] = json!({"sha256":"c".repeat(64),"size":0});
            assert!(Manifest::parse(&value).is_err(), "{name}");
            value["files"].as_object_mut().unwrap().remove(name);
        }
        value["files"]["database.sqlite3"]["size"] = json!(MAX_BYTES + 1);
        assert!(Manifest::parse(&value).is_err());
    }
}
