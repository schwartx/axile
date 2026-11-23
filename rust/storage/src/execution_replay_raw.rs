//! V2 stores compressed raw observations once. SQLite contains locations only.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{Seek, Write};
use std::path::Path;

use flate2::{write::ZlibEncoder, Compression};
use mcap::records::MessageHeader;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::execution_replay::{
    account_directory, child_file, digest, failure, identifier, io_error, ReplayResult, JS_SAFE,
};
use crate::execution_replay_raw_index as raw_index;
use crate::execution_replay_raw_locations::{self as locator, Location};

pub const EVENT_LIMIT: usize = 512 * 1024;
const BLOCK_TARGET: usize = 64 * 1024;
const BUFFER_LIMIT: usize = 1024 * 1024;
const STREAM_LIMIT: usize = 128;

struct Buffer {
    bytes: Vec<u8>,
    rows: Vec<(u64, u64)>,
}

pub struct RawWriter {
    db: Connection,
    writer: mcap::Writer<File>,
    file: File,
    _lock: File,
    recording: String,
    buffers: BTreeMap<u16, Buffer>,
    streams: BTreeMap<(String, String), u16>,
    sequence: u64,
    time: u64,
    watermark: u64,
    buffered: usize,
    block: u64,
    closed: bool,
}

impl RawWriter {
    pub fn new(root: &Path, account: u64, recording: &str, context: &str) -> ReplayResult<Self> {
        identifier(recording)?;
        if context.len() > 256 * 1024 {
            return Err(failure("BUDGET", "raw context length"));
        }
        let context: Value = serde_json::from_str(context).map_err(io_error)?;
        if context["schema_version"] != 2 || context.to_string().len() > 256 * 1024 {
            return Err(failure("SCHEMA", "v2 context required"));
        }
        let directory = account_directory(root, account, true)?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(child_file(&directory, ".raw-writer.lock")?)
            .map_err(io_error)?;
        fs2::FileExt::try_lock_exclusive(&lock).map_err(|_| failure("BUSY", "raw writer busy"))?;
        let index_path = child_file(&directory, ".replay-v2.sqlite")?;
        let existed = index_path.exists();
        let db = Connection::open(index_path).map_err(io_error)?;
        if existed
            && db
                .query_row("SELECT value FROM meta WHERE key='layout'", [], |r| {
                    r.get::<_, String>(0)
                })
                .optional()
                .ok()
                .flatten()
                .as_deref()
                != Some(raw_index::LAYOUT)
        {
            return Err(failure(
                "INDEX_REBUILD_REQUIRED",
                "raw index layout changed",
            ));
        }
        db.busy_timeout(std::time::Duration::from_secs(2))
            .map_err(io_error)?;
        db.execute_batch(include_str!("execution_replay_raw.sql"))
            .map_err(io_error)?;
        db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('layout',?1)",
            [raw_index::LAYOUT],
        )
        .map_err(io_error)?;
        db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('generation',?1)",
            [uuid::Uuid::new_v4().to_string()],
        )
        .map_err(io_error)?;
        let tx =
            rusqlite::Transaction::new_unchecked(&db, rusqlite::TransactionBehavior::Immediate)
                .map_err(io_error)?;
        let retired: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM meta WHERE key='pruned:'||?1)",
                [recording],
                |row| row.get(0),
            )
            .map_err(io_error)?;
        if retired {
            return Err(failure("INVALID_REQUEST", "raw recording is being retired"));
        }
        tx.execute(
            "INSERT INTO recordings(id,context) VALUES (?1,?2)",
            params![recording, context.to_string()],
        )
        .map_err(io_error)?;
        raw_index::context(&tx, recording, &context)?;
        let missing: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM content_refs r WHERE r.recording=?1 AND NOT EXISTS(
             SELECT 1 FROM commits c JOIN recordings s ON s.id=c.recording
             WHERE c.recording=r.target AND c.id=r.commit_id))",
                [recording],
                |row| row.get(0),
            )
            .map_err(io_error)?;
        if missing {
            return Err(failure(
                "INVALID_REQUEST",
                "raw content reference is unavailable",
            ));
        }
        let file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .open(directory.join(format!("raw-{recording}.mcap")))
            .map_err(io_error)?;
        let sync_file = file.try_clone().map_err(io_error)?;
        let mut writer = mcap::WriteOptions::new()
            .use_chunks(false)
            .compression(None)
            .emit_metadata_indexes(false)
            .profile("axile.raw.v2")
            .library("axile-storage")
            .create(file)
            .map_err(io_error)?;
        writer
            .write_metadata(&mcap::records::Metadata {
                name: "axile.raw.context.v2".into(),
                metadata: [
                    ("recording_id".into(), recording.into()),
                    ("context".into(), context.to_string()),
                ]
                .into(),
            })
            .map_err(io_error)?;
        writer.flush().map_err(io_error)?;
        sync_file.sync_all().map_err(io_error)?;
        tx.commit().map_err(io_error)?;
        Ok(Self {
            db,
            writer,
            file: sync_file,
            _lock: lock,
            recording: recording.into(),
            buffers: BTreeMap::new(),
            streams: BTreeMap::new(),
            sequence: 0,
            time: 0,
            watermark: 0,
            buffered: 0,
            block: 0,
            closed: false,
        })
    }

    pub fn append(&mut self, text: &str) -> ReplayResult<()> {
        if self.closed || text.len() > EVENT_LIMIT {
            return Err(failure("BUDGET", "raw event or writer limit"));
        }
        let value: Value = serde_json::from_str(text).map_err(io_error)?;
        let seq = value["capture_seq"].as_u64().unwrap_or(0);
        let time = value["time_us"].as_u64().unwrap_or(JS_SAFE + 1);
        if value["schema_version"] != 2
            || value["recording_id"] != self.recording
            || seq != self.sequence + 1
            || time < self.time.max(self.watermark)
            || time > JS_SAFE
        {
            return Err(failure(
                "INVALID_REQUEST",
                "raw observation order or schema",
            ));
        }
        let topic = value["topic"]
            .as_str()
            .ok_or_else(|| failure("SCHEMA", "topic required"))?;
        let symbol = value["data"]["symbol"].as_str().unwrap_or("");
        if topic.len() > 512 || symbol.len() > 1024 {
            return Err(failure("BUDGET", "stream identity limit"));
        }
        let key = (topic.to_string(), symbol.to_string());
        let channel = if let Some(id) = self.streams.get(&key) {
            *id
        } else {
            if self.streams.len() >= STREAM_LIMIT {
                return Err(failure("BUDGET", "raw stream limit"));
            }
            let id = self
                .writer
                .add_channel(
                    0,
                    topic,
                    "axile.ndjson.zlib.v2",
                    &[("symbol".into(), symbol.into())].into(),
                )
                .map_err(io_error)?;
            self.db
                .execute(
                    "INSERT INTO streams VALUES (?1,?2,?3,?4)",
                    params![self.recording, id, topic, symbol],
                )
                .map_err(io_error)?;
            self.streams.insert(key, id);
            id
        };
        let encoded = serde_json::to_vec(&value).map_err(io_error)?;
        let buffer = self.buffers.entry(channel).or_insert_with(|| Buffer {
            bytes: Vec::new(),
            rows: Vec::new(),
        });
        buffer.bytes.extend_from_slice(&encoded);
        buffer.bytes.push(b'\n');
        buffer.rows.push((seq, time));
        self.buffered += encoded.len() + 1;
        self.sequence = seq;
        self.time = time;
        if self.buffered >= BUFFER_LIMIT {
            self.commit(time)?;
        }
        Ok(())
    }

    fn flush_stream(
        &mut self,
        channel: u16,
        buffer: Buffer,
        observations: &mut Vec<Location>,
        market: &mut Vec<crate::execution_replay_raw_market::Pending>,
    ) -> ReplayResult<Value> {
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(&buffer.bytes).map_err(io_error)?;
        let compressed = encoder.finish().map_err(io_error)?;
        self.writer.flush().map_err(io_error)?;
        let offset = self.file.stream_position().map_err(io_error)? + 31;
        let first = buffer.rows.first().unwrap();
        let last = buffer.rows.last().unwrap();
        if self.block >= u32::MAX as u64 {
            return Err(failure("BUDGET", "raw block sequence exhausted"));
        }
        self.block += 1;
        self.writer
            .write_to_known_channel(
                &MessageHeader {
                    channel_id: channel,
                    sequence: self.block as u32,
                    log_time: first.1 * 1000,
                    publish_time: last.1 * 1000,
                },
                &compressed,
            )
            .map_err(io_error)?;
        let hash = digest(&compressed);
        self.db
            .execute(
                "INSERT INTO blocks VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
                params![
                    self.recording,
                    self.block,
                    channel,
                    offset,
                    compressed.len(),
                    buffer.bytes.len(),
                    hash,
                    first.0,
                    last.0,
                    locator::block_positions(&buffer.rows)?
                ],
            )
            .map_err(io_error)?;
        for (line, bytes) in buffer
            .bytes
            .split(|b| *b == b'\n')
            .filter(|line| !line.is_empty())
            .enumerate()
        {
            let value: Value = serde_json::from_slice(bytes).map_err(io_error)?;
            if let Some(row) =
                crate::execution_replay_raw_market::capture(&value, self.block, line, channel)?
            {
                market.push(row);
            }
            raw_index::observation(&self.db, &self.recording, &value, channel, self.block)?;
            if observations.len() >= locator::COMMIT_ROWS {
                return Err(failure("BUDGET", "raw commit location count"));
            }
            observations.push(Location {
                seq: value["capture_seq"].as_u64().unwrap(),
                time: value["time_us"].as_u64().unwrap(),
                block: self.block,
                line,
            });
        }
        Ok(
            json!({"block":self.block,"stream":channel,"offset":offset,"length":compressed.len(),
            "raw_length":buffer.bytes.len(),"sha256":hash,"first_seq":first.0,"last_seq":last.0}),
        )
    }

    fn commit(&mut self, watermark: u64) -> ReplayResult<()> {
        if self.closed {
            return Err(failure("STOPPED", "raw writer closed"));
        }
        self.db.execute_batch("BEGIN IMMEDIATE").map_err(io_error)?;
        let result = self.commit_inner(watermark);
        if result.is_err() {
            let _ = self.db.execute_batch("ROLLBACK");
            self.closed = true;
        }
        result
    }

    fn commit_inner(&mut self, watermark: u64) -> ReplayResult<()> {
        let mut locations = Vec::new();
        let mut observations = Vec::new();
        let mut market = Vec::new();
        for (channel, buffer) in std::mem::take(&mut self.buffers) {
            // Bound each decoder allocation even when a single event exceeds the block target.
            let mut batch = Buffer {
                bytes: Vec::new(),
                rows: Vec::new(),
            };
            for (line, row) in buffer
                .bytes
                .split_inclusive(|b| *b == b'\n')
                .zip(buffer.rows)
            {
                if !batch.bytes.is_empty() && batch.bytes.len() + line.len() > BLOCK_TARGET {
                    locations.push(self.flush_stream(
                        channel,
                        batch,
                        &mut observations,
                        &mut market,
                    )?);
                    batch = Buffer {
                        bytes: Vec::new(),
                        rows: Vec::new(),
                    };
                }
                batch.bytes.extend_from_slice(line);
                batch.rows.push(row);
            }
            if !batch.rows.is_empty() {
                locations.push(self.flush_stream(
                    channel,
                    batch,
                    &mut observations,
                    &mut market,
                )?);
            }
        }
        crate::execution_replay_raw_market::publish(&self.db, &self.recording, &mut market)?;
        let previous = self
            .db
            .query_row(
                "SELECT seq FROM recordings WHERE id=?1",
                [&self.recording],
                |r| r.get(0),
            )
            .map_err(io_error)?;
        raw_index::finish_links(&self.db, &self.recording, previous)?;
        locator::publish(
            &self.db,
            &self.recording,
            &mut observations,
            self.sequence,
            watermark,
        )?;
        let receipt =
            json!({"sequence":self.sequence,"watermark":watermark,"blocks":locations}).to_string();
        self.writer.flush().map_err(io_error)?;
        let commit_offset = self.file.stream_position().map_err(io_error)?;
        let commit_id = digest(format!("{}:{commit_offset}:{receipt}", self.recording).as_bytes());
        self.writer
            .write_metadata(&mcap::records::Metadata {
                name: "axile.raw.commit.v2".into(),
                metadata: [("receipt".into(), receipt)].into(),
            })
            .map_err(io_error)?;
        self.writer.flush().map_err(io_error)?;
        self.file.sync_all().map_err(io_error)?;
        self.db
            .execute(
                "UPDATE recordings SET seq=?2,watermark=?3,commit_id=?4 WHERE id=?1",
                params![self.recording, self.sequence, watermark, commit_id],
            )
            .map_err(io_error)?;
        self.db
            .execute(
                "INSERT INTO commits VALUES (?1,?2,?3,?4,?5,?6)",
                params![
                    self.recording,
                    commit_id,
                    commit_offset,
                    self.sequence,
                    watermark,
                    self.block
                ],
            )
            .map_err(io_error)?;
        self.db.execute_batch("COMMIT").map_err(io_error)?;
        self.buffered = 0;
        self.watermark = watermark;
        Ok(())
    }

    pub fn poll(&mut self, time: u64) -> ReplayResult<()> {
        if time < self.time || time > JS_SAFE {
            return Err(failure("INVALID_REQUEST", "raw watermark"));
        }
        if time >= self.watermark.saturating_add(1_000_000) {
            self.commit(time)?;
        }
        Ok(())
    }
    pub fn seal(&mut self) -> ReplayResult<()> {
        self.commit(self.time.max(self.watermark))
    }
    pub fn finish(&mut self) -> ReplayResult<()> {
        if !self.closed {
            self.seal()?;
            self.writer.finish().map_err(io_error)?;
            self.file.sync_all().map_err(io_error)?;
            self.db
                .execute(
                    "UPDATE recordings SET state='closed' WHERE id=?1",
                    [&self.recording],
                )
                .map_err(io_error)?;
            self.closed = true;
            fs2::FileExt::unlock(&self._lock).map_err(io_error)?;
        }
        Ok(())
    }
    pub fn status(&self) -> Value {
        json!({"schema_version":2,"recording_id":self.recording,"sequence":self.sequence,"watermark":self.watermark,"closed":self.closed})
    }
}
