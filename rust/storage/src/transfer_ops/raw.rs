//! Raw v2 transfer uses committed source prefixes and derives indexes locally.
use super::{
    files::{self, invalid, Result, MAX_BYTES},
    package::{Entry, Manifest},
};
use rusqlite::{params, types::Value as SqlValue, Connection, OpenFlags, OptionalExtension};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
};

fn database(account: &Path) -> Result<Connection> {
    let path = account.join(".replay-v2.sqlite");
    files::reject_links(&path)?;
    let db =
        Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(invalid)?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-2048; BEGIN")
        .map_err(invalid)?;
    Ok(db)
}
pub(super) fn pin(account: &Path, recording: &str, exclusive: bool) -> Result<File> {
    crate::execution_replay::identifier(recording).map_err(invalid)?;
    let path = account.join(format!(".raw-{recording}.lock"));
    files::reject_links(&path)?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)
        .map_err(invalid)?;
    if exclusive {
        fs2::FileExt::try_lock_exclusive(&file)
    } else {
        fs2::FileExt::try_lock_shared(&file)
    }
    .map_err(invalid)?;
    Ok(file)
}
fn prefix(path: &Path, offset: u64) -> Result<u64> {
    let mut file = File::open(path).map_err(invalid)?;
    file.seek(SeekFrom::Start(offset)).map_err(invalid)?;
    let mut header = [0; 9];
    file.read_exact(&mut header).map_err(invalid)?;
    if header[0] != 0x0c {
        return Err(invalid("原始提交记录无效"));
    }
    offset
        .checked_add(9)
        .and_then(|n| n.checked_add(u64::from_le_bytes(header[1..].try_into().unwrap())))
        .filter(|n| *n <= file.metadata().ok().map(|m| m.len()).unwrap_or(0))
        .ok_or_else(|| invalid("原始提交前缀无效"))
}
pub(super) fn collect(
    base: &Path,
    staging: &Path,
    start: i64,
    end: i64,
) -> Result<BTreeMap<String, PathBuf>> {
    let mut paths = BTreeMap::new();
    let replay = base.join("replay");
    if !replay.exists() {
        return Ok(paths);
    }
    let mut total = 0u64;
    for entry in fs::read_dir(&replay).map_err(invalid)? {
        let entry = entry.map_err(invalid)?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !entry.file_type().map_err(invalid)?.is_dir()
            || !name
                .strip_prefix("account-")
                .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        {
            continue;
        }
        let account = entry.path();
        if !account.join(".replay-v2.sqlite").exists() {
            continue;
        }
        let db = database(&account)?;
        let mut stmt = db.prepare("SELECT id,commit_id FROM recordings WHERE seq>0 AND watermark>=?1 AND (SELECT first_time FROM location_pages WHERE recording=id ORDER BY first_seq LIMIT 1)<?2 LIMIT 65").map_err(invalid)?;
        let mut queue = stmt
            .query_map(params![start, end], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(invalid)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(invalid)?;
        let mut selected = BTreeMap::new();
        while let Some((recording, commit)) = queue.pop() {
            if selected
                .get(&recording)
                .is_some_and(|old: &(u64, String)| old.1 == commit)
            {
                continue;
            }
            let (offset, seq): (u64, u64) = db
                .query_row(
                    "SELECT offset,seq FROM commits WHERE recording=?1 AND id=?2",
                    params![recording, commit],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(invalid)?;
            if selected.get(&recording).is_some_and(|old| old.0 >= seq) {
                continue;
            }
            selected.insert(recording.clone(), (seq, commit.clone()));
            if selected.len() > 64 {
                return Err(invalid("原始录制依赖超过 64 段预算"));
            }
            let context: String = db
                .query_row(
                    "SELECT context FROM recordings WHERE id=?1",
                    [&recording],
                    |r| r.get(0),
                )
                .map_err(invalid)?;
            let context: Value = serde_json::from_str(&context).map_err(invalid)?;
            if let Some(refs) = context["content_refs"].as_array() {
                if refs.len() > 64 {
                    return Err(invalid("原始内容引用超过预算"));
                }
                for reference in refs {
                    let id = reference["recording"]
                        .as_str()
                        .ok_or_else(|| invalid("无效内容引用"))?;
                    crate::execution_replay::identifier(id).map_err(invalid)?;
                    let commit = reference["commit_id"]
                        .as_str()
                        .filter(|s| s.len() == 64)
                        .ok_or_else(|| invalid("无效内容提交"))?;
                    queue.push((id.to_owned(), commit.to_owned()));
                }
            }
            let _ = offset;
        }
        for (recording, (_, commit)) in selected {
            let _pin = pin(&account, &recording, false)?;
            let source = account.join(format!("raw-{recording}.mcap"));
            files::reject_links(&source)?;
            let (offset,closed): (u64,bool) = db.query_row("SELECT c.offset,(r.state='closed' AND r.commit_id=c.id) FROM commits c JOIN recordings r ON r.id=c.recording WHERE c.recording=?1 AND c.id=?2", params![recording,commit], |r|Ok((r.get(0)?,r.get(1)?))).map_err(invalid)?;
            let length = if closed {
                fs::metadata(&source).map_err(invalid)?.len()
            } else {
                prefix(&source, offset)?
            };
            total = total
                .checked_add(length)
                .ok_or_else(|| invalid("原始数据超过预算"))?;
            if total > MAX_BYTES
                || fs2::available_space(base).map_err(invalid)?
                    < length.saturating_mul(2).saturating_add(256 * 1024 * 1024)
            {
                return Err(invalid("原始数据或暂存磁盘超过预算"));
            }
            let relative = format!("replay/{name}/raw-{recording}.mcap");
            let target = staging.join(&relative);
            files::mkdir(target.parent().unwrap())?;
            let mut output = File::create(&target).map_err(invalid)?;
            let n = std::io::copy(
                &mut File::open(source).map_err(invalid)?.take(length),
                &mut output,
            )
            .map_err(invalid)?;
            if n != length {
                return Err(invalid("原始提交前缀已丢失"));
            }
            output.sync_all().map_err(invalid)?;
            paths.insert(relative, target);
        }
    }
    Ok(paths)
}
struct DirectAccount {
    db: Connection,
    _leases: [File; 2],
}

impl DirectAccount {
    fn open(account: &Path) -> Result<Option<Self>> {
        let lock = |name: &str| -> Result<File> {
            let path = account.join(name);
            files::reject_links(&path)?;
            let file = OpenOptions::new()
                .create(true)
                .truncate(false)
                .write(true)
                .open(path)
                .map_err(invalid)?;
            fs2::FileExt::try_lock_exclusive(&file)
                .map_err(|_| invalid("接收端正在录制或维护索引"))?;
            Ok(file)
        };
        let leases = [
            lock(".raw-writer.lock")?,
            lock(".raw-index-maintenance.lock")?,
        ];
        if account.join(".raw-import-pending").exists() {
            return Ok(None);
        }
        let index = account.join(".replay-v2.sqlite");
        files::reject_links(&index)?;
        let exists = index.exists();
        if !exists {
            for entry in fs::read_dir(account).map_err(invalid)? {
                let name = entry.map_err(invalid)?.file_name();
                if name
                    .to_str()
                    .is_some_and(|name| name.starts_with("raw-") && name.ends_with(".mcap"))
                {
                    return Ok(None);
                }
            }
        }
        let db = Connection::open(index).map_err(invalid)?;
        if exists {
            let layout = db
                .query_row("SELECT value FROM meta WHERE key='layout'", [], |row| {
                    row.get::<_, String>(0)
                })
                .ok();
            if layout.as_deref() != Some(crate::execution_replay_raw_index::LAYOUT) {
                return Ok(None);
            }
        } else {
            db.execute_batch(include_str!("../execution_replay_raw.sql"))
                .map_err(invalid)?;
            db.execute(
                "INSERT INTO meta VALUES ('layout',?1),('generation',?2)",
                params![
                    crate::execution_replay_raw_index::LAYOUT,
                    uuid::Uuid::new_v4().to_string()
                ],
            )
            .map_err(invalid)?;
        }
        db.execute_batch("BEGIN IMMEDIATE").map_err(invalid)?;
        Ok(Some(Self {
            db,
            _leases: leases,
        }))
    }
}

pub(super) fn install_direct(
    database: &str,
    base: &Path,
    staging: &Path,
    manifest: &Manifest,
) -> Result<bool> {
    let mut accounts = BTreeMap::new();
    let mut recordings = Vec::new();
    for (name, entry) in manifest
        .files
        .iter()
        .filter(|(name, _)| name.starts_with("replay/"))
    {
        let source = staging.join(name);
        let target = base.join(name);
        files::reject_links(&target)?;
        let account = target.parent().unwrap();
        files::mkdir(account)?;
        if !accounts.contains_key(account) {
            let Some(install) = DirectAccount::open(account)? else {
                return Ok(false);
            };
            accounts.insert(account.to_owned(), install);
        }
        let db = &accounts[account].db;
        let recording = target
            .file_stem()
            .unwrap()
            .to_str()
            .unwrap()
            .strip_prefix("raw-")
            .unwrap();
        let state: Option<String> = db
            .query_row(
                "SELECT state FROM recordings WHERE id=?1",
                [recording],
                |row| row.get(0),
            )
            .optional()
            .map_err(invalid)?;
        if target.exists() {
            let length = target.metadata().map_err(invalid)?.len();
            if length != entry.size || state.as_deref().is_some_and(|state| state != "closed") {
                return Ok(false);
            }
            if files::digest(&target)? != entry.sha256 {
                return Err(invalid("同名原始录制内容冲突"));
            }
            if state.is_some() {
                continue;
            }
        } else if state.is_some() {
            return Ok(false);
        }
        let pin = pin(account, recording, true)?;
        let retired: bool = db
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM meta WHERE key=?1)",
                [format!("pruned:{recording}")],
                |row| row.get(0),
            )
            .map_err(invalid)?;
        if retired {
            return Err(invalid("请等待原始录制清理完成"));
        }
        crate::execution_replay_raw_rebuild::scan(db, &source, recording, false)
            .map_err(invalid)?;
        let closed: bool = db
            .query_row(
                "SELECT state='closed' AND seq>0 FROM recordings WHERE id=?1",
                [recording],
                |row| row.get(0),
            )
            .map_err(invalid)?;
        if !closed {
            return Err(invalid("在线迁移需要已封口的录制"));
        }
        // The direct sender only selects independently readable recordings.
        let missing: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM content_refs r WHERE r.recording=?1
             AND NOT EXISTS(SELECT 1 FROM commits c WHERE c.recording=r.target AND c.id=r.commit_id))",
            [recording], |row| row.get(0)
        ).map_err(invalid)?;
        if missing {
            return Err(invalid("数据包缺少被引用的原始提交前缀"));
        }
        recordings.push((source, target, pin));
    }
    super::import_snapshot(
        database,
        &serde_json::json!({
            "source_id":manifest.source_id, "snapshot":staging.join("database.sqlite3")
        }),
    )?;
    for (source, target, _) in &recordings {
        if !target.exists() {
            // Persist the same inode before publishing its index, leaving a retryable source until receipt.
            fs::hard_link(source, target).map_err(invalid)?;
        }
        files::sync_directory(target.parent().unwrap())?;
    }
    for install in accounts.values() {
        install.db.execute_batch("COMMIT").map_err(invalid)?;
    }
    Ok(true)
}

pub(super) fn install(base: &Path, staging: &Path, manifest: &Manifest) -> Result<()> {
    let mut accounts = BTreeSet::new();
    for (name, entry) in manifest
        .files
        .iter()
        .filter(|(n, _)| n.starts_with("replay/"))
    {
        let source = staging.join(name);
        let target = base.join(name);
        files::reject_links(&target)?;
        files::mkdir(target.parent().unwrap())?;
        let recording = target
            .file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .strip_prefix("raw-")
            .unwrap()
            .strip_suffix(".mcap")
            .unwrap();
        let _pin = pin(target.parent().unwrap(), recording, true)?;
        let account = target.parent().unwrap();
        let pending = account.join(".raw-import-pending");
        let index_ok = Connection::open_with_flags(
            account.join(".replay-v2.sqlite"),
            OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .ok()
        .and_then(|db| {
            db.query_row("SELECT value FROM meta WHERE key='layout'", [], |r| {
                r.get::<_, String>(0)
            })
            .ok()
        })
        .is_some_and(|layout| layout == crate::execution_replay_raw_index::LAYOUT);
        if pending.exists() || !index_ok {
            accounts.insert(account.to_owned());
        }
        if target.exists() {
            let old = fs::metadata(&target).map_err(invalid)?.len();
            if !same_prefix(&target, &source, old.min(entry.size))? {
                return Err(invalid("同名原始录制内容冲突"));
            }
            if old >= entry.size {
                continue;
            }
        }
        File::create(&pending)
            .map_err(invalid)?
            .sync_all()
            .map_err(invalid)?;
        files::sync_directory(account)?;
        fs::rename(&source, &target).map_err(invalid)?;
        files::sync_directory(target.parent().unwrap())?;
        accounts.insert(target.parent().unwrap().to_owned());
    }
    for account in accounts {
        let id = account
            .file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .strip_prefix("account-")
            .unwrap()
            .parse()
            .map_err(invalid)?;
        crate::execution_replay_raw_rebuild::rebuild(&base.join("replay"), id).map_err(invalid)?;
        files::remove_file(&account.join(".raw-import-pending"))?;
    }
    Ok(())
}
pub(super) fn validate(staging: &Path, manifest: &Manifest) -> Result<()> {
    let accounts: BTreeSet<_> = manifest
        .files
        .keys()
        .filter(|n| n.starts_with("replay/"))
        .map(|n| {
            n.split('/')
                .nth(1)
                .unwrap()
                .strip_prefix("account-")
                .unwrap()
                .parse::<u64>()
                .map_err(invalid)
        })
        .collect::<Result<_>>()?;
    for account in accounts {
        crate::execution_replay_raw_rebuild::rebuild(&staging.join("replay"), account)
            .map_err(invalid)?;
        let db = database(&staging.join("replay").join(format!("account-{account}")))?;
        let missing: bool=db.query_row("SELECT EXISTS(SELECT 1 FROM content_refs r WHERE NOT EXISTS(SELECT 1 FROM commits c WHERE c.recording=r.target AND c.id=r.commit_id))",[],|r|r.get(0)).map_err(invalid)?;
        if missing {
            return Err(invalid("数据包缺少被引用的原始提交前缀"));
        }
    }
    Ok(())
}
pub(super) fn check_destination(base: &Path, staging: &Path, manifest: &Manifest) -> Result<()> {
    let mut accounts = BTreeSet::new();
    for (name, entry) in manifest
        .files
        .iter()
        .filter(|(n, _)| n.starts_with("replay/"))
    {
        let path = base.join(name);
        files::reject_links(&path)?;
        accounts.insert(path.parent().unwrap().to_owned());
        if path.exists()
            && !same_prefix(
                &path,
                &staging.join(name),
                fs::metadata(&path).map_err(invalid)?.len().min(entry.size),
            )?
        {
            return Err(invalid("同名原始录制内容冲突"));
        }
    }
    for account in accounts {
        if !account.exists() {
            continue;
        }
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(account.join(".raw-writer.lock"))
            .map_err(invalid)?;
        fs2::FileExt::try_lock_exclusive(&file).map_err(|_| invalid("请先停止接收端原始录制"))?;
    }
    Ok(())
}
fn same_prefix(left: &Path, right: &Path, mut length: u64) -> Result<bool> {
    let (mut a, mut b) = (
        File::open(left).map_err(invalid)?,
        File::open(right).map_err(invalid)?,
    );
    let (mut x, mut y) = ([0; 65536], [0; 65536]);
    while length > 0 {
        let n = length.min(x.len() as u64) as usize;
        a.read_exact(&mut x[..n]).map_err(invalid)?;
        b.read_exact(&mut y[..n]).map_err(invalid)?;
        if x[..n] != y[..n] {
            return Ok(false);
        }
        length -= n as u64;
    }
    Ok(true)
}
pub(super) fn prune(base: &Path, name: &str, entry: &Entry, cutoff: i64) -> Result<bool> {
    let path = base.join(name);
    let tombstone = path.with_extension("mcap.deleted");
    if !path.exists() && !tombstone.exists() {
        return Ok(false);
    }
    files::reject_links(&path)?;
    let account = path.parent().unwrap();
    let writer = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .open(account.join(".raw-writer.lock"))
        .map_err(invalid)?;
    if fs2::FileExt::try_lock_exclusive(&writer).is_err() {
        return Ok(false);
    }
    let recording = path
        .file_name()
        .unwrap()
        .to_str()
        .unwrap()
        .strip_prefix("raw-")
        .unwrap()
        .strip_suffix(".mcap")
        .unwrap();
    let Ok(_pin) = pin(account, recording, true) else {
        return Ok(false);
    };
    let db = Connection::open(account.join(".replay-v2.sqlite")).map_err(invalid)?;
    if !path.exists() {
        files::reject_links(&tombstone)?;
        let indexed: bool = db
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM recordings WHERE id=?1)",
                [recording],
                |r| r.get(0),
            )
            .map_err(invalid)?;
        if indexed {
            fs::rename(&tombstone, &path).map_err(invalid)?;
            files::sync_directory(account)?;
        } else {
            let referenced: bool = db
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM content_refs WHERE target=?1)",
                    [recording],
                    |r| r.get(0),
                )
                .map_err(invalid)?;
            if referenced {
                return Ok(false);
            }
            files::remove_file(&tombstone)?;
            return Ok(true);
        }
    }
    if fs::metadata(&path).map_err(invalid)?.len() != entry.size
        || files::digest(&path)? != entry.sha256
    {
        return Ok(false);
    }
    db.execute_batch("PRAGMA busy_timeout=100; BEGIN IMMEDIATE")
        .map_err(invalid)?;
    let safe: bool=db.query_row("SELECT EXISTS(SELECT 1 FROM recordings WHERE id=?1 AND state='closed' AND watermark<?2) AND NOT EXISTS(SELECT 1 FROM content_refs WHERE target=?1 AND recording<>?1)", params![recording,cutoff],|r|r.get(0)).map_err(invalid)?;
    if !safe {
        return Ok(false);
    }
    // Rename under the lease; a crash leaves a recoverable tombstone, never a live index to partial bytes.
    fs::rename(&path, &tombstone).map_err(invalid)?;
    files::sync_directory(account)?;
    delete_index_rows(&db, recording, false)?;
    db.execute_batch("COMMIT").map_err(invalid)?;
    files::remove_file(&tombstone)?;
    Ok(true)
}

pub(super) fn prune_live(
    base: &Path,
    name: &str,
    entry: &Entry,
    cutoff: i64,
) -> Result<(bool, bool)> {
    let path = base.join(name);
    let tombstone = path.with_extension("mcap.deleted");
    let account = path.parent().unwrap();
    let recording = path
        .file_name()
        .unwrap()
        .to_str()
        .unwrap()
        .strip_prefix("raw-")
        .unwrap()
        .strip_suffix(".mcap")
        .unwrap();
    let maintenance = account.join(".raw-index-maintenance.lock");
    for path in [&path, &tombstone, &maintenance] {
        files::reject_links(path)?;
    }
    if !account.exists() {
        return Ok((false, false));
    }
    let lease = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .open(maintenance)
        .map_err(invalid)?;
    if fs2::FileExt::try_lock_exclusive(&lease).is_err() {
        return Ok((false, true));
    }
    let Ok(_pin) = pin(account, recording, true) else {
        return Ok((false, true));
    };
    let index = account.join(".replay-v2.sqlite");
    files::reject_links(&index)?;
    let db = Connection::open(index).map_err(invalid)?;
    db.busy_timeout(std::time::Duration::from_millis(100))
        .map_err(invalid)?;
    let marker = format!("pruned:{recording}");
    let retired: bool = db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM meta WHERE key=?1)",
            [&marker],
            |row| row.get(0),
        )
        .map_err(invalid)?;
    let mut removed = false;
    if !retired && (path.exists() || tombstone.exists()) {
        let source = if path.exists() { &path } else { &tombstone };
        if fs::metadata(source).map_err(invalid)?.len() != entry.size
            || files::digest(source)? != entry.sha256
        {
            return Ok((false, true));
        }
        let tx =
            rusqlite::Transaction::new_unchecked(&db, rusqlite::TransactionBehavior::Immediate)
                .map_err(invalid)?;
        let safe: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM recordings WHERE id=?1 AND state='closed' AND watermark<?2)
             AND NOT EXISTS(SELECT 1 FROM content_refs WHERE target=?1 AND recording<>?1)",
            params![recording,cutoff], |row| row.get(0)
        ).map_err(invalid)?;
        if !safe {
            return Ok((false, true));
        }
        if source == &path {
            if tombstone.exists() {
                return Err(invalid("原始录制清理存在冲突"));
            }
            fs::rename(&path, &tombstone).map_err(invalid)?;
            files::sync_directory(account)?;
        }
        // Readers resolve the recording row first. The remaining derived rows are now unreachable.
        tx.execute("DELETE FROM recordings WHERE id=?1", [recording])
            .map_err(invalid)?;
        tx.execute("INSERT INTO meta VALUES (?1,'')", [&marker])
            .map_err(invalid)?;
        tx.commit().map_err(invalid)?;
        removed = true;
    } else if !retired {
        return Ok((false, false));
    }
    files::remove_file(&tombstone)?;
    let pending = delete_index_rows(&db, recording, true)?;
    if !pending {
        db.execute("DELETE FROM meta WHERE key=?1", [&marker])
            .map_err(invalid)?;
    }
    Ok((removed, pending))
}

fn delete_index_rows(db: &Connection, recording: &str, bounded: bool) -> Result<bool> {
    let tables = db
        .prepare(
            "SELECT name FROM sqlite_master WHERE type='table' AND name<>'meta'
         ORDER BY name='recording_keys',name",
        )
        .map_err(invalid)?
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(invalid)?
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(invalid)?;
    let key: Option<i64> = db
        .query_row(
            "SELECT id FROM recording_keys WHERE recording=?1",
            [recording],
            |row| row.get(0),
        )
        .ok();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
    for table in tables {
        let (predicate, value) = if table.starts_with("market_") {
            let Some(key) = key else { continue };
            let predicate = if table == "market_namespace_symbols" {
                "namespace IN (SELECT id FROM market_namespaces WHERE recording=?1)"
            } else {
                "recording=?1"
            };
            (predicate, SqlValue::Integer(key))
        } else {
            let column = if table == "recordings" {
                "id=?1"
            } else {
                "recording=?1"
            };
            (column, SqlValue::Text(recording.to_owned()))
        };
        let name = super::quote(&table);
        if !bounded {
            db.execute(&format!("DELETE FROM {name} WHERE {predicate}"), [&value])
                .map_err(invalid)?;
            continue;
        }
        let keys = db
            .prepare("SELECT name FROM pragma_table_info(?1) WHERE pk>0 ORDER BY pk")
            .map_err(invalid)?
            .query_map([&table], |row| row.get::<_, String>(0))
            .map_err(invalid)?
            .map(|name| name.map(|name| super::quote(&name)))
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(invalid)?
            .join(",");
        let sql = format!("DELETE FROM {name} WHERE ({keys}) IN (SELECT {keys} FROM {name} WHERE {predicate} LIMIT 256)");
        loop {
            let count = db.execute(&sql, [&value]).map_err(invalid)?;
            if count < 256 {
                break;
            }
            if std::time::Instant::now() >= deadline {
                return Ok(true);
            }
        }
    }
    Ok(false)
}
