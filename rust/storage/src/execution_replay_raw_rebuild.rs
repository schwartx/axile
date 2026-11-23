//! Explicit offline index rebuild. Normal read requests never enter this decoder.

use std::fs::{File, OpenOptions};
use std::io::{BufReader, Read, Seek};
use std::path::Path;

use flate2::read::ZlibDecoder;
use mcap::records::Record;
use rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::execution_replay::{
    account_directory, child_file, digest, failure, identifier, io_error, ReplayResult, JS_SAFE,
};
use crate::execution_replay_raw::EVENT_LIMIT;
use crate::execution_replay_raw_index as raw_index;
use crate::execution_replay_raw_locations::{self as locator, Location};

fn locked(path: &Path) -> ReplayResult<File> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(path)
        .map_err(io_error)?;
    fs2::FileExt::try_lock_exclusive(&file)
        .map_err(|_| failure("BUSY", "rebuild requires stopped readers and capture"))?;
    Ok(file)
}

fn recover_deletions(directory: &Path, index: &Path) -> ReplayResult<()> {
    let old = Connection::open_with_flags(index, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).ok();
    for entry in std::fs::read_dir(directory).map_err(io_error)? {
        let entry = entry.map_err(io_error)?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        let Some(recording) = name
            .strip_prefix("raw-")
            .and_then(|s| s.strip_suffix(".mcap.deleted"))
        else {
            continue;
        };
        identifier(recording)?;
        let pending = child_file(directory, name)?;
        let source = child_file(directory, &format!("raw-{recording}.mcap"))?;
        let needed=old.as_ref().and_then(|db|db.query_row("SELECT EXISTS(SELECT 1 FROM recordings WHERE id=?1) OR EXISTS(SELECT 1 FROM content_refs WHERE target=?1)",[recording],|r|r.get::<_,bool>(0)).ok()).unwrap_or(true);
        if !source.exists() && !needed {
            std::fs::remove_file(pending).map_err(io_error)?;
            continue;
        }
        if source.exists() {
            let (mut a, mut b) = (
                File::open(&source).map_err(io_error)?,
                File::open(&pending).map_err(io_error)?,
            );
            let (left, right) = (
                a.metadata().map_err(io_error)?.len(),
                b.metadata().map_err(io_error)?.len(),
            );
            let mut remaining = left.min(right);
            let (mut x, mut y) = ([0; 65536], [0; 65536]);
            while remaining > 0 {
                let n = remaining.min(x.len() as u64) as usize;
                a.read_exact(&mut x[..n]).map_err(io_error)?;
                b.read_exact(&mut y[..n]).map_err(io_error)?;
                if x[..n] != y[..n] {
                    return Err(failure(
                        "CONFLICT",
                        "raw deletion recovery has conflicting content",
                    ));
                }
                remaining -= n as u64;
            }
            drop(a);
            drop(b);
            if left >= right {
                std::fs::remove_file(&pending).map_err(io_error)?;
                continue;
            }
        }
        std::fs::rename(pending, source).map_err(io_error)?;
    }
    File::open(directory)
        .and_then(|f| f.sync_all())
        .map_err(io_error)
}

pub(crate) fn scan(
    db: &Connection,
    path: &Path,
    recording: &str,
    unpublished: bool,
) -> ReplayResult<()> {
    let (begin, checkpoint, rollback) = if unpublished {
        ("BEGIN IMMEDIATE", "COMMIT; BEGIN IMMEDIATE", "ROLLBACK")
    } else {
        (
            "SAVEPOINT raw_scan",
            "RELEASE raw_scan; SAVEPOINT raw_scan",
            "ROLLBACK TO raw_scan; RELEASE raw_scan",
        )
    };
    let mut input = BufReader::with_capacity(64 * 1024, File::open(path).map_err(io_error)?);
    let mut magic = [0; 8];
    input.read_exact(&mut magic).map_err(io_error)?;
    if magic != mcap::MAGIC {
        return Err(failure("CORRUPT", "raw MCAP magic"));
    }
    let (mut block, mut committed_block, mut sequence, mut watermark) = (0u64, 0u64, 0u64, 0u64);
    let (mut context, mut profile, mut footer, mut ended) = (false, false, false, false);
    let mut observations = Vec::new();
    let mut market = Vec::new();
    let mut commit_bytes = 0usize;
    db.execute_batch(begin).map_err(io_error)?;
    loop {
        let offset = input.stream_position().map_err(io_error)?;
        let mut header = [0u8; 9];
        if input.read(&mut header[..1]).map_err(io_error)? == 0 {
            break;
        }
        if header[0] == mcap::MAGIC[0] {
            let mut end = magic;
            end[0] = header[0];
            if input.read_exact(&mut end[1..]).is_ok() && end == magic && footer {
                let mut extra = [0];
                ended = input.read(&mut extra).map_err(io_error)? == 0;
            }
            break;
        }
        if input.read_exact(&mut header[1..]).is_err() {
            break;
        }
        let length = u64::from_le_bytes(header[1..].try_into().unwrap());
        if length > 2 * 1024 * 1024 {
            return Err(failure("BUDGET", "raw MCAP record length"));
        }
        let mut body = vec![0; length as usize];
        if input.read_exact(&mut body).is_err() {
            break;
        }
        match mcap::parse_record(header[0], &body).map_err(io_error)? {
            Record::Header(h) => {
                if h.profile != "axile.raw.v2" {
                    return Err(failure("SCHEMA", "raw v2 required"));
                }
                profile = true;
            }
            Record::Channel(channel) => {
                if channel.message_encoding != "axile.ndjson.zlib.v2" || channel.topic.len() > 512 {
                    return Err(failure("SCHEMA", "raw channel"));
                }
                let symbol = channel
                    .metadata
                    .get("symbol")
                    .map(String::as_str)
                    .unwrap_or("");
                if symbol.len() > 1024 {
                    return Err(failure("BUDGET", "raw symbol"));
                }
                db.execute(
                    "INSERT OR IGNORE INTO streams VALUES (?1,?2,?3,?4)",
                    params![recording, channel.id, channel.topic, symbol],
                )
                .map_err(io_error)?;
                let count: u64 = db
                    .query_row(
                        "SELECT count(*) FROM streams WHERE recording=?1",
                        [recording],
                        |r| r.get(0),
                    )
                    .map_err(io_error)?;
                if count > 128 {
                    return Err(failure("BUDGET", "raw stream count"));
                }
            }
            Record::Message { header, data } => {
                if !profile || !context || footer || data.len() > 600 * 1024 {
                    return Err(failure("CORRUPT", "raw message boundary"));
                }
                block += 1;
                if block != u64::from(header.sequence) {
                    return Err(failure("CORRUPT", "raw block sequence"));
                }
                let (topic, symbol): (String, String) = db
                    .query_row(
                        "SELECT topic,symbol FROM streams WHERE recording=?1 AND id=?2",
                        params![recording, header.channel_id],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .map_err(io_error)?;
                let mut raw = Vec::new();
                ZlibDecoder::new(data.as_ref())
                    .take((EVENT_LIMIT + 2) as u64)
                    .read_to_end(&mut raw)
                    .map_err(io_error)?;
                if raw.len() > EVENT_LIMIT + 1 || raw.last() != Some(&b'\n') {
                    return Err(failure("BUDGET", "raw block decoded length"));
                }
                commit_bytes += raw.len();
                if commit_bytes > 1536 * 1024 + 2 {
                    return Err(failure("BUDGET", "raw commit decoded bytes"));
                }
                let mut first = 0;
                let mut last = 0;
                // Insert the block before its direct observation references in this unpublished transaction.
                db.execute(
                    "INSERT INTO blocks(recording,id,stream,offset,length,raw_length,sha256,first_seq,last_seq) VALUES (?1,?2,?3,?4,?5,?6,?7,0,0)",
                    params![
                        recording,
                        block,
                        header.channel_id,
                        offset + 31,
                        data.len(),
                        raw.len(),
                        digest(data.as_ref())
                    ],
                )
                .map_err(io_error)?;
                let mut block_rows = Vec::new();
                for (line, bytes) in raw[..raw.len() - 1].split(|b| *b == b'\n').enumerate() {
                    if bytes.len() > EVENT_LIMIT {
                        return Err(failure("BUDGET", "raw event length"));
                    }
                    let event: Value = serde_json::from_slice(bytes).map_err(io_error)?;
                    let seq = event["capture_seq"]
                        .as_u64()
                        .filter(|s| *s > last && *s <= JS_SAFE)
                        .ok_or_else(|| failure("CORRUPT", "raw observation sequence"))?;
                    if event["schema_version"] != 2
                        || event["recording_id"] != recording
                        || event["time_us"].as_u64().is_none_or(|t| t > JS_SAFE)
                    {
                        return Err(failure("SCHEMA", "raw observation source"));
                    }
                    if event["topic"] != topic
                        || event["data"]["symbol"].as_str().unwrap_or("") != symbol
                    {
                        return Err(failure("CORRUPT", "raw channel identity"));
                    }
                    if first == 0 {
                        first = seq;
                    }
                    last = seq;
                    let observed = event["time_us"].as_u64().unwrap();
                    if observations.len() >= locator::COMMIT_ROWS
                        || block_rows.len() >= locator::BLOCK_ROWS
                    {
                        return Err(failure("BUDGET", "raw location count"));
                    }
                    observations.push(Location {
                        seq,
                        time: observed,
                        block,
                        line,
                    });
                    block_rows.push((seq, observed));
                    if let Some(row) = crate::execution_replay_raw_market::capture(
                        &event,
                        block,
                        line,
                        header.channel_id,
                    )? {
                        market.push(row);
                    }
                    raw_index::observation(db, recording, &event, header.channel_id, block)?;
                }
                db.execute(
                    "UPDATE blocks SET first_seq=?3,last_seq=?4,positions=?5 WHERE recording=?1 AND id=?2",
                    params![recording, block, first, last,locator::block_positions(&block_rows)?],
                )
                .map_err(io_error)?;
            }
            Record::Metadata(metadata) if metadata.name == "axile.raw.context.v2" => {
                let text = metadata
                    .metadata
                    .get("context")
                    .ok_or_else(|| failure("CORRUPT", "raw context"))?;
                let value: Value = serde_json::from_str(text).map_err(io_error)?;
                if context
                    || metadata.metadata.get("recording_id").map(String::as_str) != Some(recording)
                    || value["schema_version"] != 2
                    || text.len() > 256 * 1024
                {
                    return Err(failure("SCHEMA", "raw context source"));
                }
                db.execute(
                    "INSERT INTO recordings(id,context) VALUES (?1,?2)",
                    params![recording, text],
                )
                .map_err(io_error)?;
                context = true;
                raw_index::context(db, recording, &value)?;
                db.execute_batch(checkpoint).map_err(io_error)?;
            }
            Record::Metadata(metadata) if metadata.name == "axile.raw.commit.v2" => {
                let text = metadata
                    .metadata
                    .get("receipt")
                    .ok_or_else(|| failure("CORRUPT", "raw commit"))?;
                let receipt: Value = serde_json::from_str(text).map_err(io_error)?;
                let next = receipt["sequence"]
                    .as_u64()
                    .filter(|n| *n >= sequence && *n <= JS_SAFE)
                    .ok_or_else(|| failure("CORRUPT", "raw commit sequence"))?;
                let next_watermark = receipt["watermark"]
                    .as_u64()
                    .filter(|n| *n >= watermark && *n <= JS_SAFE)
                    .ok_or_else(|| failure("CORRUPT", "raw commit watermark"))?;
                let locations = receipt["blocks"]
                    .as_array()
                    .ok_or_else(|| failure("CORRUPT", "raw commit blocks"))?;
                if locations.len() as u64 != block - committed_block {
                    return Err(failure("CORRUPT", "raw commit block count"));
                }
                for (i, location) in locations.iter().enumerate() {
                    let id = committed_block + i as u64 + 1;
                    let indexed:Value=db.query_row("SELECT stream,offset,length,raw_length,sha256,first_seq,last_seq FROM blocks WHERE recording=?1 AND id=?2",params![recording,id],|r|Ok(json!({"block":id,"stream":r.get::<_,u16>(0)?,"offset":r.get::<_,u64>(1)?,"length":r.get::<_,u64>(2)?,"raw_length":r.get::<_,u64>(3)?,"sha256":r.get::<_,String>(4)?,"first_seq":r.get::<_,u64>(5)?,"last_seq":r.get::<_,u64>(6)?}))).map_err(io_error)?;
                    if *location != indexed {
                        return Err(failure("CORRUPT", "raw commit checksum or position"));
                    }
                }
                crate::execution_replay_raw_market::publish(db, recording, &mut market)?;
                raw_index::finish_links(db, recording, sequence)?;
                locator::publish(db, recording, &mut observations, next, next_watermark)?;
                commit_bytes = 0;
                observations.clear();
                sequence = next;
                let id = digest(format!("{recording}:{offset}:{text}").as_bytes());
                db.execute(
                    "UPDATE recordings SET seq=?2,watermark=?3,commit_id=?4 WHERE id=?1",
                    params![recording, sequence, next_watermark, id],
                )
                .map_err(io_error)?;
                db.execute(
                    "INSERT INTO commits VALUES (?1,?2,?3,?4,?5,?6)",
                    params![recording, id, offset, sequence, next_watermark, block],
                )
                .map_err(io_error)?;
                db.execute_batch(checkpoint).map_err(io_error)?;
                watermark = next_watermark;
                committed_block = block;
            }
            Record::Footer(_) => footer = true,
            Record::Chunk { .. } | Record::Attachment { .. } => {
                return Err(failure("SCHEMA", "raw v2 unchunked records required"))
            }
            _ => {}
        }
    }
    if !profile || !context {
        return Err(failure("CORRUPT", "raw context not committed"));
    }
    db.execute_batch(rollback).map_err(io_error)?;
    db.execute(
        "UPDATE recordings SET state=?2 WHERE id=?1",
        params![
            recording,
            if ended && block == committed_block {
                "closed"
            } else {
                "error"
            }
        ],
    )
    .map_err(io_error)?;
    Ok(())
}

pub fn rebuild(root: &Path, account: u64) -> ReplayResult<Value> {
    let root = root.canonicalize().map_err(io_error)?;
    let _reader = locked(&child_file(&root, ".read-owner-v2.lock")?)?;
    let directory = account_directory(&root, account, false)?;
    let _writer = locked(&child_file(&directory, ".raw-writer.lock")?)?;
    let _maintenance = locked(&child_file(&directory, ".raw-index-maintenance.lock")?)?;
    let index = child_file(&directory, ".replay-v2.sqlite")?;
    // Recover interrupted cleanup before enumerating inputs for the new generation.
    recover_deletions(&directory, &index)?;
    let temp = tempfile::Builder::new()
        .prefix(".replay-rebuild-")
        .tempfile_in(&directory)
        .map_err(io_error)?;
    let db = Connection::open(temp.path()).map_err(io_error)?;
    db.execute_batch(include_str!("execution_replay_raw.sql"))
        .map_err(io_error)?;
    // The index is unpublished until the completed file is synced and renamed below.
    db.execute_batch("PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF; PRAGMA cache_size=-2048")
        .map_err(io_error)?;
    let generation = uuid::Uuid::new_v4().to_string();
    db.execute("INSERT INTO meta VALUES ('layout',?1)", [raw_index::LAYOUT])
        .map_err(io_error)?;
    db.execute("INSERT INTO meta VALUES ('generation',?1)", [&generation])
        .map_err(io_error)?;
    let mut recordings = 0;
    for entry in std::fs::read_dir(&directory).map_err(io_error)? {
        let entry = entry.map_err(io_error)?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        let Some(recording) = name
            .strip_prefix("raw-")
            .and_then(|s| s.strip_suffix(".mcap"))
        else {
            continue;
        };
        identifier(recording)?;
        scan(&db, &child_file(&directory, name)?, recording, true)?;
        recordings += 1;
    }
    let observations: u64 = db
        .query_row("SELECT coalesce(sum(seq),0) FROM recordings", [], |r| {
            r.get(0)
        })
        .map_err(io_error)?;
    db.execute_batch("PRAGMA journal_mode=WAL")
        .map_err(io_error)?;
    db.close().map_err(|(_, error)| io_error(error))?;
    if index.exists() {
        let old = Connection::open(&index).map_err(io_error)?;
        let busy: u64 = old
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get(0))
            .map_err(io_error)?;
        if busy != 0 {
            return Err(failure("BUSY", "old raw index has active readers"));
        }
        old.close().map_err(|(_, error)| io_error(error))?;
    }
    // No live readers/writer exist; an empty WAL must never attach to the replacement generation.
    for suffix in ["-wal", "-shm"] {
        let sidecar = child_file(&directory, &format!(".replay-v2.sqlite{suffix}"))?;
        if sidecar.exists() {
            std::fs::remove_file(sidecar).map_err(io_error)?;
        }
    }
    temp.as_file().sync_all().map_err(io_error)?;
    temp.persist(&index).map_err(|e| io_error(e.error))?;
    File::open(&directory)
        .and_then(|f| f.sync_all())
        .map_err(io_error)?;
    Ok(json!({"index_generation":generation,"recordings":recordings,"observations":observations}))
}
