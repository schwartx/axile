//! Account-local, append-only index of durable MCAP blocks and versioned facts.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::Value;

use crate::execution_replay::{
    digest, failure, identifier, io_error, CatalogRecording, PartDescriptor, ReplayResult,
    CONTEXT_LIMIT, JS_SAFE, SCAN_BYTES,
};
use crate::execution_replay_block::{count_chunks, read_block, BlockRecord, CommittedBlock};

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub(crate) struct IndexedBlock {
    pub part: u32,
    pub range: CommittedBlock,
    pub first_seq: u64,
    pub last_seq: u64,
    pub first_us: u64,
    pub last_us: u64,
}

/// One observation may replace or remove an earlier fact without rewriting history.
pub(crate) struct FactVersion<'a> {
    pub kind: &'a str,
    pub key: &'a str,
    pub seq: u64,
    pub value: Option<&'a Value>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct IndexedCheckpoint {
    pub part: u32,
    pub seq: u64,
    pub time_us: Option<u64>,
    pub state: Value,
    pub schema: Option<u16>,
    pub channels: BTreeMap<u16, String>,
}

pub(crate) struct BlockIndex {
    db: Connection,
}

fn reject_symlink(path: &Path) -> ReplayResult<()> {
    if fs::symlink_metadata(path)
        .map_err(io_error)?
        .file_type()
        .is_symlink()
    {
        return Err(failure("PATH", "replay files must not be symbolic links"));
    }
    Ok(())
}

fn valid_fact_key(kind: &str, key: &str) -> bool {
    !kind.is_empty() && kind.len() <= 64 && !key.is_empty() && key.len() <= 64 * 1024
}

fn block_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<IndexedBlock> {
    Ok(IndexedBlock {
        part: row.get(0)?,
        range: CommittedBlock {
            offset: row.get(1)?,
            length: row.get(2)?,
            sha256: row.get(3)?,
        },
        first_seq: row.get(4)?,
        last_seq: row.get(5)?,
        first_us: row.get(6)?,
        last_us: row.get(7)?,
    })
}

fn validate_indexed_block(block: &IndexedBlock, through_seq: u64) -> ReplayResult<()> {
    if block.part == 0
        || block.range.length == 0
        || block.range.length > SCAN_BYTES
        || block.range.offset.checked_add(block.range.length).is_none()
        || block.range.sha256.len() != 64
        || !block
            .range
            .sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit())
        || block.first_seq == 0
        || block.first_seq > block.last_seq
        || block.last_seq > through_seq
        || block.first_us > block.last_us
        || block.last_us > JS_SAFE
    {
        return Err(failure("CORRUPT", "invalid committed block descriptor"));
    }
    Ok(())
}

impl BlockIndex {
    pub fn writer(path: &Path) -> ReplayResult<Self> {
        if !path.exists() {
            return Self::create(path);
        }
        reject_symlink(path)?;
        let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(io_error)?;
        let version: u32 = db
            .query_row("SELECT version FROM identity", [], |row| row.get(0))
            .map_err(io_error)?;
        if version != 1 {
            return Err(failure("CORRUPT", "unsupported replay block index"));
        }
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;")
            .map_err(io_error)?;
        Ok(Self { db })
    }

    pub fn create(path: &Path) -> ReplayResult<Self> {
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .map_err(io_error)?;
        let db = Connection::open(path).map_err(io_error)?;
        db.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
             CREATE TABLE identity (version INTEGER NOT NULL);
             INSERT INTO identity VALUES (1);
             CREATE TABLE recordings (recording TEXT PRIMARY KEY, metadata TEXT) WITHOUT ROWID;
             CREATE TABLE files (recording TEXT NOT NULL, part INTEGER NOT NULL, day TEXT NOT NULL,
               PRIMARY KEY(recording, part)) WITHOUT ROWID;
             CREATE TABLE recording_executions (recording TEXT NOT NULL, execution TEXT NOT NULL,
               PRIMARY KEY(recording, execution)) WITHOUT ROWID;
             CREATE TABLE blocks (
               recording TEXT NOT NULL, part INTEGER NOT NULL, offset INTEGER NOT NULL,
               length INTEGER NOT NULL, hash TEXT NOT NULL, first_seq INTEGER NOT NULL,
               last_seq INTEGER NOT NULL, first_us INTEGER NOT NULL, last_us INTEGER NOT NULL,
               PRIMARY KEY (recording, part, offset), UNIQUE (recording, first_seq),
               UNIQUE (recording, last_seq)
             ) WITHOUT ROWID;
             CREATE INDEX block_times ON blocks(recording, first_us, last_us);
             CREATE TABLE watermarks (
               recording TEXT NOT NULL, part INTEGER NOT NULL, seq INTEGER NOT NULL, end_us INTEGER NOT NULL,
               PRIMARY KEY (recording, part, seq)
             ) WITHOUT ROWID;
             CREATE TABLE sealed_files (
               recording TEXT NOT NULL, part INTEGER NOT NULL, descriptor TEXT NOT NULL,
               context TEXT NOT NULL, previous TEXT, PRIMARY KEY (recording, part)
             ) WITHOUT ROWID;
             CREATE TABLE checkpoints (
               recording TEXT NOT NULL, part INTEGER NOT NULL, seq INTEGER NOT NULL, time_us INTEGER,
               payload TEXT NOT NULL, hash TEXT NOT NULL, state_hash TEXT NOT NULL,
               PRIMARY KEY (recording, part, seq)
             ) WITHOUT ROWID;
             CREATE INDEX checkpoint_times ON checkpoints(recording, time_us, seq);
             CREATE TABLE facts (
               recording TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
               seq INTEGER NOT NULL, value TEXT,
               PRIMARY KEY (recording, kind, key, seq)
             ) WITHOUT ROWID;",
        )
        .map_err(io_error)?;
        if let Some(directory) = path.parent() {
            File::open(directory)
                .and_then(|dir| dir.sync_all())
                .map_err(io_error)?;
        }
        Ok(Self { db })
    }

    pub fn open(path: &Path) -> ReplayResult<Self> {
        reject_symlink(path)?;
        let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(io_error)?;
        let version: u32 = db
            .query_row("SELECT version FROM identity", [], |row| row.get(0))
            .map_err(io_error)?;
        if version != 1 {
            return Err(failure("CORRUPT", "unsupported replay block index"));
        }
        Ok(Self { db })
    }

    pub fn register_recording(&self, recording: &str) -> ReplayResult<()> {
        identifier(recording)?;
        let inserted = self
            .db
            .execute(
                "INSERT OR IGNORE INTO recordings VALUES (?1, NULL)",
                [recording],
            )
            .map_err(io_error)?;
        if inserted == 0 {
            return Err(failure("IDENTITY", "recording identity already exists"));
        }
        Ok(())
    }

    pub fn set_metadata(&self, recording: &str, metadata: &CatalogRecording) -> ReplayResult<()> {
        identifier(recording)?;
        let payload = serde_json::to_string(metadata).map_err(io_error)?;
        if self
            .db
            .execute(
                "UPDATE recordings SET metadata=?2 WHERE recording=?1",
                params![recording, payload],
            )
            .map_err(io_error)?
            != 1
        {
            return Err(failure("CORRUPT", "unknown recording identity"));
        }
        Ok(())
    }

    pub fn register_file(&self, recording: &str, part: u32, day: &str) -> ReplayResult<()> {
        identifier(recording)?;
        if part == 0
            || day.len() != 10
            || chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d").is_err()
        {
            return Err(failure("CORRUPT", "invalid replay file identity"));
        }
        self.db
            .execute(
                "INSERT INTO files VALUES (?1, ?2, ?3)",
                params![recording, part, day],
            )
            .map_err(io_error)?;
        Ok(())
    }

    pub fn register_execution(&self, recording: &str, execution: &str) -> ReplayResult<()> {
        identifier(recording)?;
        if !valid_fact_key("execution_member", execution) {
            return Err(failure("CORRUPT", "invalid execution identity"));
        }
        self.db
            .execute(
                "INSERT OR IGNORE INTO recording_executions VALUES (?1, ?2)",
                params![recording, execution],
            )
            .map_err(io_error)?;
        Ok(())
    }

    pub fn recording_metadata(
        &self,
        execution: &str,
    ) -> ReplayResult<BTreeMap<String, CatalogRecording>> {
        let mut recordings = BTreeMap::new();
        let mut query = self
            .db
            .prepare("SELECT recording, metadata, EXISTS(SELECT 1 FROM recording_executions e WHERE e.recording=recordings.recording AND e.execution=?1) FROM recordings WHERE metadata IS NOT NULL")
            .map_err(io_error)?;
        let rows = query
            .query_map([execution], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, bool>(2)?,
                ))
            })
            .map_err(io_error)?;
        for row in rows {
            let (recording, payload, belongs) = row.map_err(io_error)?;
            identifier(&recording)?;
            let mut metadata: CatalogRecording =
                serde_json::from_str(&payload).map_err(|e| failure("CORRUPT", e.to_string()))?;
            metadata.executions = if belongs {
                BTreeSet::from([execution.to_owned()])
            } else {
                BTreeSet::new()
            };
            recordings.insert(recording, metadata);
        }
        Ok(recordings)
    }

    pub fn files(&self, execution: &str) -> ReplayResult<Vec<(String, u32, String)>> {
        let mut query = self
            .db
            .prepare("SELECT f.recording, f.part, f.day FROM files f JOIN recordings r ON r.recording=f.recording
                WHERE EXISTS(SELECT 1 FROM recording_executions e WHERE e.recording=f.recording AND e.execution=?1)
                   OR json_extract(r.metadata, '$.unsealed_start_us') IS NOT NULL
                ORDER BY f.recording, f.part")
            .map_err(io_error)?;
        let rows = query
            .query_map([execution], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })
            .map_err(io_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(io_error)
    }

    /// The writer must have flushed complete MCAP records before calling this.
    pub fn publish(
        &mut self,
        recording: &str,
        part: u32,
        path: &Path,
        file: &File,
        first_seq: u64,
        last_seq: u64,
        first_us: u64,
        last_us: u64,
        facts: &[FactVersion<'_>],
        checkpoints: &[IndexedCheckpoint],
        validate: impl FnOnce(&[BlockRecord]) -> ReplayResult<()>,
    ) -> ReplayResult<(IndexedBlock, u64)> {
        identifier(recording)?;
        reject_symlink(path)?;
        let source = File::open(path).map_err(io_error)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let expected = file.metadata().map_err(io_error)?;
            let actual = source.metadata().map_err(io_error)?;
            if (expected.dev(), expected.ino()) != (actual.dev(), actual.ino()) {
                return Err(failure("VIEW_CHANGED", "replay file was replaced"));
            }
        }
        let transaction = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(io_error)?;
        let tail: Option<(u32, u64, u64, u64, u64)> = transaction
            .query_row(
                "SELECT part, offset, length, last_seq, last_us FROM blocks
             WHERE recording = ?1 ORDER BY last_seq DESC LIMIT 1",
                [recording],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .optional()
            .map_err(io_error)?;
        let (offset, expected_seq, previous_us) = match tail {
            Some((previous_part, offset, length, seq, time)) => {
                let offset = if part == previous_part {
                    offset
                        .checked_add(length)
                        .ok_or_else(|| failure("CORRUPT", "block end overflow"))?
                } else if previous_part.checked_add(1) == Some(part) {
                    0
                } else {
                    return Err(failure(
                        "SEQUENCE",
                        "replay file sequence is not contiguous",
                    ));
                };
                (
                    offset,
                    seq.checked_add(1)
                        .ok_or_else(|| failure("BUDGET", "sequence overflow"))?,
                    Some(time),
                )
            }
            None if part == 1 => (0, 1, None),
            None => {
                return Err(failure(
                    "SEQUENCE",
                    "recording must start with the first file",
                ))
            }
        };
        let end = file.metadata().map_err(io_error)?.len();
        let length = end
            .checked_sub(offset)
            .ok_or_else(|| failure("VIEW_CHANGED", "replay file was truncated"))?;
        if first_seq != expected_seq
            || last_seq < first_seq
            || last_seq > JS_SAFE
            || first_us > last_us
            || last_us > JS_SAFE
            || previous_us.is_some_and(|time| first_us < time)
            || length == 0
            || length > SCAN_BYTES
            || checkpoints.len() > 2
        {
            return Err(failure("CORRUPT", "invalid committed block bounds"));
        }
        for fact in facts {
            if !valid_fact_key(fact.kind, fact.key) || !(first_seq..=last_seq).contains(&fact.seq) {
                return Err(failure("CORRUPT", "fact lies outside its committed block"));
            }
        }
        // SQLite cannot publish a block or its facts until the MCAP bytes are durable.
        file.sync_data().map_err(io_error)?;
        let mut source = source;
        source.seek(SeekFrom::Start(offset)).map_err(io_error)?;
        let mut bytes = Vec::with_capacity(length as usize);
        source
            .take(length)
            .read_to_end(&mut bytes)
            .map_err(io_error)?;
        if bytes.len() as u64 != length {
            return Err(failure(
                "VIEW_CHANGED",
                "replay file was truncated during commit",
            ));
        }
        let range = CommittedBlock {
            offset,
            length,
            sha256: digest(&bytes),
        };
        let chunks = count_chunks(&bytes, offset == 0)?;
        let records = read_block(path, &range)?;
        validate(&records)?;
        transaction
            .execute(
                "INSERT INTO blocks VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                params![
                    recording,
                    part,
                    offset,
                    length,
                    range.sha256,
                    first_seq,
                    last_seq,
                    first_us,
                    last_us
                ],
            )
            .map_err(io_error)?;
        for fact in facts {
            let value = fact
                .value
                .map(serde_json::to_string)
                .transpose()
                .map_err(io_error)?;
            transaction
                .execute(
                    "INSERT INTO facts VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![recording, fact.kind, fact.key, fact.seq, value],
                )
                .map_err(io_error)?;
        }
        for checkpoint in checkpoints {
            if checkpoint.part != part
                || ![first_seq - 1, last_seq].contains(&checkpoint.seq)
                || checkpoint.state["recording_id"] != recording
                || checkpoint.state["state_through_seq"] != checkpoint.seq
                || checkpoint.state["time_us"].as_u64() != checkpoint.time_us
            {
                return Err(failure(
                    "CORRUPT",
                    "checkpoint does not match its committed block",
                ));
            }
            crate::execution_replay_projection::Projection::restore(checkpoint.state.clone())?;
            let state_hash = digest(&serde_json::to_vec(&checkpoint.state).map_err(io_error)?);
            let mut compact = checkpoint.clone();
            let accounting = &mut compact.state["trade_accounting"];
            accounting["own_seen_keys"] = serde_json::json!([]);
            accounting["own_records"] = serde_json::json!({});
            accounting["pending"]
                .as_object_mut()
                .ok_or_else(|| failure("CORRUPT", "invalid checkpoint pending facts"))?
                .retain(|key, _| !key.starts_with("own:"));
            let payload = serde_json::to_string(&compact).map_err(io_error)?;
            transaction
                .execute(
                    "INSERT INTO checkpoints VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![
                        recording,
                        part,
                        checkpoint.seq,
                        checkpoint.time_us,
                        payload,
                        digest(payload.as_bytes()),
                        state_hash
                    ],
                )
                .map_err(io_error)?;
        }
        transaction.commit().map_err(io_error)?;
        Ok((
            IndexedBlock {
                part,
                range,
                first_seq,
                last_seq,
                first_us,
                last_us,
            },
            chunks,
        ))
    }

    /// `through_seq` fixes the visible prefix even if a writer commits more blocks.
    pub fn blocks(
        &self,
        recording: &str,
        through_seq: u64,
        after: Option<(u32, u64)>,
        limit: usize,
    ) -> ReplayResult<Vec<IndexedBlock>> {
        self.blocks_in_time(recording, through_seq, after, limit, None)
    }

    pub fn blocks_in_time(
        &self,
        recording: &str,
        through_seq: u64,
        after: Option<(u32, u64)>,
        limit: usize,
        time: Option<(u64, u64)>,
    ) -> ReplayResult<Vec<IndexedBlock>> {
        identifier(recording)?;
        if through_seq > JS_SAFE
            || limit == 0
            || limit > 4096
            || time.is_some_and(|(start, end)| start >= end || end > JS_SAFE)
        {
            return Err(failure("BUDGET", "invalid block index query"));
        }
        let (after_part, after_offset) = after.unwrap_or((0, 0));
        let (start, end) = time.map_or((None, None), |(start, end)| (Some(start), Some(end)));
        let mut statement = self.db.prepare(
            "SELECT part, offset, length, hash, first_seq, last_seq, first_us, last_us FROM blocks
             WHERE recording = ?1 AND last_seq <= ?2 AND (part > ?3 OR (part = ?3 AND offset > ?4))
             AND (?6 IS NULL OR (last_us >= ?6 AND first_us < ?7)) ORDER BY part, offset LIMIT ?5",
        ).map_err(io_error)?;
        let rows = statement
            .query_map(
                params![
                    recording,
                    through_seq,
                    after_part,
                    after_offset,
                    limit,
                    start,
                    end
                ],
                block_from_row,
            )
            .map_err(io_error)?;
        let blocks = rows.collect::<Result<Vec<_>, _>>().map_err(io_error)?;
        for block in &blocks {
            validate_indexed_block(block, through_seq)?;
        }
        Ok(blocks)
    }

    pub fn file_bounds(
        &self,
        recording: &str,
        part: u32,
        through_seq: u64,
    ) -> ReplayResult<Option<(IndexedBlock, IndexedBlock)>> {
        identifier(recording)?;
        if part == 0 || through_seq > JS_SAFE {
            return Err(failure("CORRUPT", "invalid replay file query"));
        }
        let query =
            "SELECT part, offset, length, hash, first_seq, last_seq, first_us, last_us FROM blocks
                     WHERE recording=?1 AND part=?2 AND last_seq<=?3 ORDER BY offset";
        let first: Option<IndexedBlock> = self
            .db
            .query_row(
                &format!("{query} ASC LIMIT 1"),
                params![recording, part, through_seq],
                block_from_row,
            )
            .optional()
            .map_err(io_error)?;
        let last: Option<IndexedBlock> = self
            .db
            .query_row(
                &format!("{query} DESC LIMIT 1"),
                params![recording, part, through_seq],
                block_from_row,
            )
            .optional()
            .map_err(io_error)?;
        match (first, last) {
            (None, None) => Ok(None),
            (Some(first), Some(last)) => {
                validate_indexed_block(&first, through_seq)?;
                validate_indexed_block(&last, through_seq)?;
                if first.range.offset != 0
                    || first.first_seq > last.last_seq
                    || first.first_us > last.last_us
                {
                    return Err(failure("CORRUPT", "invalid indexed file bounds"));
                }
                Ok(Some((first, last)))
            }
            _ => Err(failure("CORRUPT", "inconsistent indexed file bounds")),
        }
    }

    pub fn file_blocks(
        &self,
        recording: &str,
        part: u32,
        through_seq: u64,
        after: Option<u64>,
        limit: usize,
    ) -> ReplayResult<Vec<IndexedBlock>> {
        identifier(recording)?;
        if part == 0 || through_seq > JS_SAFE || limit == 0 || limit > 4096 {
            return Err(failure("BUDGET", "invalid indexed file query"));
        }
        let mut statement = self.db.prepare(
            "SELECT part, offset, length, hash, first_seq, last_seq, first_us, last_us FROM blocks
             WHERE recording=?1 AND part=?2 AND last_seq<=?3 AND (?4 IS NULL OR offset>?4) ORDER BY offset LIMIT ?5"
        ).map_err(io_error)?;
        let rows = statement
            .query_map(
                params![recording, part, through_seq, after, limit],
                block_from_row,
            )
            .map_err(io_error)?;
        let blocks = rows.collect::<Result<Vec<_>, _>>().map_err(io_error)?;
        for block in &blocks {
            validate_indexed_block(block, through_seq)?;
        }
        Ok(blocks)
    }

    pub fn time_at_block_end(&self, recording: &str, seq: u64) -> ReplayResult<Option<u64>> {
        identifier(recording)?;
        if seq > JS_SAFE {
            return Err(failure("CORRUPT", "invalid replay sequence"));
        }
        self.db
            .query_row(
                "SELECT last_us FROM blocks WHERE recording=?1 AND last_seq=?2",
                params![recording, seq],
                |row| row.get(0),
            )
            .optional()
            .map_err(io_error)
    }

    pub fn record_watermark(
        &self,
        recording: &str,
        part: u32,
        seq: u64,
        end_us: u64,
    ) -> ReplayResult<()> {
        identifier(recording)?;
        if end_us > JS_SAFE {
            return Err(failure("BUDGET", "invalid replay watermark"));
        }
        let (_, last) = self
            .file_bounds(recording, part, seq)?
            .ok_or_else(|| failure("INCOMPLETE", "watermark has no committed block"))?;
        if last.last_seq != seq || end_us <= last.last_us {
            return Err(failure(
                "CORRUPT",
                "watermark does not cover the committed block",
            ));
        }
        self.db
            .execute(
                "INSERT INTO watermarks VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(recording, part, seq) DO UPDATE SET end_us=max(end_us, excluded.end_us)",
                params![recording, part, seq, end_us],
            )
            .map_err(io_error)?;
        Ok(())
    }

    pub fn watermark(
        &self,
        recording: &str,
        part: u32,
        through_seq: u64,
    ) -> ReplayResult<Option<u64>> {
        identifier(recording)?;
        if through_seq > JS_SAFE {
            return Err(failure("BUDGET", "invalid replay watermark query"));
        }
        let value: Option<u64> = self.db.query_row(
            "SELECT end_us FROM watermarks WHERE recording=?1 AND part=?2 AND seq<=?3 ORDER BY seq DESC LIMIT 1",
            params![recording, part, through_seq], |row| row.get(0),
        ).optional().map_err(io_error)?;
        if value.is_some_and(|end| end > JS_SAFE) {
            return Err(failure("CORRUPT", "invalid replay watermark"));
        }
        Ok(value)
    }

    pub fn block_at_end(
        &self,
        recording: &str,
        part: u32,
        seq: u64,
    ) -> ReplayResult<Option<IndexedBlock>> {
        identifier(recording)?;
        if part == 0 || seq > JS_SAFE {
            return Err(failure("CORRUPT", "invalid replay block query"));
        }
        let block = self.db.query_row("SELECT part, offset, length, hash, first_seq, last_seq, first_us, last_us FROM blocks WHERE recording=?1 AND part=?2 AND last_seq=?3",
            params![recording, part, seq], block_from_row).optional().map_err(io_error)?;
        if let Some(block) = &block {
            validate_indexed_block(block, seq)?;
        }
        Ok(block)
    }

    fn restore_checkpoint(
        &self,
        recording: &str,
        payload: &str,
        hash: &str,
        state_hash: &str,
    ) -> ReplayResult<IndexedCheckpoint> {
        if digest(payload.as_bytes()) != hash {
            return Err(failure("CORRUPT", "indexed checkpoint hash mismatch"));
        }
        let mut checkpoint: IndexedCheckpoint =
            serde_json::from_str(payload).map_err(|error| failure("CORRUPT", error.to_string()))?;
        let accounting = &mut checkpoint.state["trade_accounting"];
        let mut pending = accounting["pending"]
            .as_object()
            .cloned()
            .ok_or_else(|| failure("CORRUPT", "invalid checkpoint pending facts"))?;
        if accounting["own_records"] != serde_json::json!({})
            || accounting["own_seen_keys"] != serde_json::json!([])
            || pending.keys().any(|key| key.starts_with("own:"))
        {
            return Err(failure(
                "CORRUPT",
                "checkpoint contains duplicated own facts",
            ));
        }
        let mut records = serde_json::Map::new();
        let mut seen = Vec::new();
        let mut statement = self
            .db
            .prepare(
                "SELECT f.kind, f.key, f.value FROM facts f WHERE f.recording=?1
             AND f.kind IN ('own_trade', 'own_observation') AND f.seq=(
                 SELECT MAX(v.seq) FROM facts v WHERE v.recording=f.recording
                 AND v.kind=f.kind AND v.key=f.key AND v.seq<=?2)
             AND f.value IS NOT NULL ORDER BY f.kind, f.key",
            )
            .map_err(io_error)?;
        let rows = statement
            .query_map(params![recording, checkpoint.seq], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(io_error)?;
        for row in rows {
            let (kind, key, text) = row.map_err(io_error)?;
            if !valid_fact_key(&kind, &key) {
                return Err(failure("CORRUPT", "invalid indexed own fact"));
            }
            let fact: Value =
                serde_json::from_str(&text).map_err(|e| failure("CORRUPT", e.to_string()))?;
            if kind == "own_observation" {
                pending.insert(format!("own:observation:{key}"), fact);
            } else {
                if !fact["seen"].is_boolean()
                    || !(fact["record"].is_object() || fact["record"].is_null())
                    || !(fact["pending"].is_object() || fact["pending"].is_null())
                {
                    return Err(failure("CORRUPT", "invalid indexed trade fact"));
                }
                if fact["seen"] == true {
                    seen.push(Value::String(key.clone()));
                }
                if !fact["record"].is_null() {
                    records.insert(key.clone(), fact["record"].clone());
                }
                if !fact["pending"].is_null() {
                    pending.insert(format!("own:trade:{key}"), fact["pending"].clone());
                }
            }
        }
        accounting["own_records"] = Value::Object(records);
        accounting["own_seen_keys"] = Value::Array(seen);
        accounting["pending"] = Value::Object(pending);
        if digest(&serde_json::to_vec(&checkpoint.state).map_err(io_error)?) != state_hash {
            return Err(failure(
                "CORRUPT",
                "restored checkpoint does not match its facts",
            ));
        }
        crate::execution_replay_projection::Projection::restore(checkpoint.state.clone())?;
        Ok(checkpoint)
    }

    pub fn checkpoint_before(
        &self,
        recording: &str,
        start_us: u64,
        through_seq: u64,
    ) -> ReplayResult<Option<IndexedCheckpoint>> {
        identifier(recording)?;
        if start_us > JS_SAFE || through_seq > JS_SAFE {
            return Err(failure("BUDGET", "invalid checkpoint query"));
        }
        let row: Option<(String, String, String)> = self.db.query_row(
            "SELECT payload, hash, state_hash FROM checkpoints WHERE recording=?1 AND seq<=?2 AND (time_us IS NULL OR time_us<?3)
             ORDER BY seq DESC, part DESC LIMIT 1", params![recording, through_seq, start_us],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        ).optional().map_err(io_error)?;
        row.map(|(payload, hash, state_hash)| {
            let checkpoint = self.restore_checkpoint(recording, &payload, &hash, &state_hash)?;
            if checkpoint.part == 0
                || checkpoint.seq > through_seq
                || checkpoint.time_us.is_some_and(|time| time >= start_us)
                || checkpoint.state["recording_id"] != recording
                || checkpoint.state["state_through_seq"] != checkpoint.seq
                || checkpoint.state["time_us"].as_u64() != checkpoint.time_us
            {
                return Err(failure(
                    "CORRUPT",
                    "indexed checkpoint identity or time mismatch",
                ));
            }
            Ok(checkpoint)
        })
        .transpose()
    }

    pub fn checkpoint_at(
        &self,
        recording: &str,
        part: u32,
        seq: u64,
    ) -> ReplayResult<Option<IndexedCheckpoint>> {
        identifier(recording)?;
        if part == 0 || seq > JS_SAFE {
            return Err(failure("CORRUPT", "invalid indexed checkpoint query"));
        }
        let row: Option<(String, String, String)> = self
            .db
            .query_row(
                "SELECT payload, hash, state_hash FROM checkpoints WHERE recording=?1 AND part=?2 AND seq=?3",
                params![recording, part, seq],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(io_error)?;
        row.map(|(payload, hash, state_hash)| {
            let checkpoint = self.restore_checkpoint(recording, &payload, &hash, &state_hash)?;
            if checkpoint.part != part
                || checkpoint.seq != seq
                || checkpoint.state["recording_id"] != recording
                || checkpoint.state["state_through_seq"] != seq
                || checkpoint.state["time_us"].as_u64() != checkpoint.time_us
            {
                return Err(failure("CORRUPT", "indexed checkpoint identity mismatch"));
            }
            Ok(checkpoint)
        })
        .transpose()
    }

    pub fn seal_file(
        &mut self,
        part: &PartDescriptor,
        context: &Value,
        previous: Option<&str>,
    ) -> ReplayResult<()> {
        identifier(&part.recording_id)?;
        if let Some(previous) = previous {
            identifier(previous)?;
        }
        let descriptor = serde_json::to_string(part).map_err(io_error)?;
        let context = serde_json::to_string(context).map_err(io_error)?;
        if descriptor.len() > 64 * 1024 || context.len() > CONTEXT_LIMIT {
            return Err(failure("BUDGET", "sealed replay metadata exceeds budget"));
        }
        let (_, last) = self
            .file_bounds(&part.recording_id, part.part, part.last_seq)?
            .ok_or_else(|| failure("INCOMPLETE", "sealed replay has no committed blocks"))?;
        if last.last_seq != part.last_seq
            || last.range.offset.saturating_add(last.range.length) > part.size_bytes
        {
            return Err(failure(
                "CORRUPT",
                "sealed replay metadata exceeds committed bounds",
            ));
        }
        self.db
            .execute(
                "INSERT INTO sealed_files VALUES (?1, ?2, ?3, ?4, ?5)",
                params![part.recording_id, part.part, descriptor, context, previous],
            )
            .map_err(io_error)?;
        Ok(())
    }

    pub fn sealed_file(
        &self,
        recording: &str,
        part: u32,
    ) -> ReplayResult<Option<(PartDescriptor, Value, Option<String>)>> {
        identifier(recording)?;
        let row: Option<(String, String, Option<String>)> = self.db.query_row(
            "SELECT descriptor, context, previous FROM sealed_files WHERE recording=?1 AND part=?2",
            params![recording, part], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        ).optional().map_err(io_error)?;
        row.map(|(descriptor, context, previous)| {
            if descriptor.len() > 64 * 1024 || context.len() > CONTEXT_LIMIT {
                return Err(failure("BUDGET", "sealed replay metadata exceeds budget"));
            }
            Ok((
                serde_json::from_str(&descriptor)
                    .map_err(|error| failure("CORRUPT", error.to_string()))?,
                serde_json::from_str(&context)
                    .map_err(|error| failure("CORRUPT", error.to_string()))?,
                previous,
            ))
        })
        .transpose()
    }

    pub fn fact_at(
        &self,
        recording: &str,
        kind: &str,
        key: &str,
        through_seq: u64,
    ) -> ReplayResult<Option<(u64, Option<Value>)>> {
        identifier(recording)?;
        if !valid_fact_key(kind, key) || through_seq > JS_SAFE {
            return Err(failure("CORRUPT", "invalid indexed fact query"));
        }
        let row: Option<(u64, Option<String>)> = self
            .db
            .query_row(
                "SELECT seq, value FROM facts WHERE recording=?1 AND kind=?2 AND key=?3 AND seq<=?4
             ORDER BY seq DESC LIMIT 1",
                params![recording, kind, key, through_seq],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(io_error)?;
        row.map(|(seq, value)| {
            let value = value
                .map(|text| {
                    serde_json::from_str(&text)
                        .map_err(|error| failure("CORRUPT", error.to_string()))
                })
                .transpose()?;
            Ok((seq, value))
        })
        .transpose()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mcap::records::MessageHeader;
    use serde_json::json;
    use std::collections::BTreeMap;

    fn file(path: &Path) -> (mcap::Writer<File>, File, u16) {
        let file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .open(path)
            .unwrap();
        let sync = file.try_clone().unwrap();
        let mut writer = mcap::WriteOptions::new()
            .compression(Some(mcap::Compression::Zstd))
            .create(file)
            .unwrap();
        let schema = writer.add_schema("test", "jsonschema", b"{}").unwrap();
        let channel = writer
            .add_channel(schema, "/trade", "json", &BTreeMap::new())
            .unwrap();
        (writer, sync, channel)
    }

    fn write(writer: &mut mcap::Writer<File>, channel: u16, seq: u32, bytes: &[u8]) {
        writer
            .write_to_known_channel(
                &MessageHeader {
                    channel_id: channel,
                    sequence: seq,
                    log_time: seq as u64 * 1_000,
                    publish_time: seq as u64 * 1_000,
                },
                bytes,
            )
            .unwrap();
        writer.flush().unwrap();
    }

    #[test]
    fn a_committed_block_can_index_more_than_one_hundred_thousand_facts() {
        let root =
            std::env::temp_dir().join(format!("axile-replay-many-facts-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let path = root.join("one.mcap.partial");
        let (mut writer, sync, channel) = file(&path);
        let mut index = BlockIndex::create(&root.join("account.sqlite")).unwrap();
        write(&mut writer, channel, 1, b"event");
        let keys: Vec<_> = (0..100_001).map(|n| format!("T{n}")).collect();
        let facts: Vec<_> = keys
            .iter()
            .map(|key| FactVersion {
                kind: "market",
                key,
                seq: 1,
                value: None,
            })
            .collect();
        index
            .publish(
                "rec",
                1,
                &path,
                &sync,
                1,
                1,
                10,
                10,
                &facts,
                &[],
                |_| Ok(()),
            )
            .unwrap();
        assert_eq!(
            index.fact_at("rec", "market", "T100000", 1).unwrap(),
            Some((1, None))
        );
        drop(index);
        drop(writer);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn commits_blocks_and_fact_versions_together_across_files() {
        let root = std::env::temp_dir().join(format!("axile-replay-index-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let path1 = root.join("one.mcap.partial");
        let path2 = root.join("two.mcap.partial");
        let index_path = root.join("account.sqlite");
        let (mut writer1, sync1, channel1) = file(&path1);
        let mut index = BlockIndex::create(&index_path).unwrap();
        write(&mut writer1, channel1, 1, b"first");
        assert!(BlockIndex::open(&index_path)
            .unwrap()
            .blocks("rec", JS_SAFE, None, 8)
            .unwrap()
            .is_empty());
        let first_value = json!({"quantity":3});
        let fact1 = FactVersion {
            kind: "market",
            key: "T",
            seq: 1,
            value: Some(&first_value),
        };
        let first = index
            .publish(
                "rec",
                1,
                &path1,
                &sync1,
                1,
                1,
                10,
                10,
                &[fact1],
                &[],
                |_| Ok(()),
            )
            .unwrap()
            .0;
        write(&mut writer1, channel1, 2, b"second");
        assert_eq!(
            BlockIndex::open(&index_path)
                .unwrap()
                .blocks("rec", JS_SAFE, None, 8)
                .unwrap(),
            vec![first.clone()]
        );
        assert!(index
            .publish("rec", 1, &path1, &sync1, 3, 3, 10, 10, &[], &[], |_| Ok(()))
            .is_err());
        let bad = FactVersion {
            kind: "market",
            key: "T",
            seq: 3,
            value: None,
        };
        assert!(index
            .publish("rec", 1, &path1, &sync1, 2, 2, 10, 10, &[bad], &[], |_| Ok(
                ()
            ))
            .is_err());
        let duplicate = [
            FactVersion {
                kind: "market",
                key: "other",
                seq: 2,
                value: None,
            },
            FactVersion {
                kind: "market",
                key: "other",
                seq: 2,
                value: None,
            },
        ];
        assert!(index
            .publish(
                "rec",
                1,
                &path1,
                &sync1,
                2,
                2,
                10,
                10,
                &duplicate,
                &[],
                |_| Ok(())
            )
            .is_err());
        let reader = BlockIndex::open(&index_path).unwrap();
        assert_eq!(
            reader.blocks("rec", JS_SAFE, None, 8).unwrap(),
            vec![first.clone()]
        );
        assert!(reader
            .fact_at("rec", "market", "other", 2)
            .unwrap()
            .is_none());
        drop(reader);
        let updated = json!({"quantity":3,"price":5});
        let fact2 = FactVersion {
            kind: "market",
            key: "T",
            seq: 2,
            value: Some(&updated),
        };
        let second = index
            .publish(
                "rec",
                1,
                &path1,
                &sync1,
                2,
                2,
                10,
                10,
                &[fact2],
                &[],
                |_| Ok(()),
            )
            .unwrap()
            .0;
        writer1.finish().unwrap();
        drop(writer1);
        let (mut writer2, sync2, channel2) = file(&path2);
        write(&mut writer2, channel2, 3, b"third");
        let fact3 = FactVersion {
            kind: "market",
            key: "T",
            seq: 3,
            value: None,
        };
        let third = index
            .publish(
                "rec",
                2,
                &path2,
                &sync2,
                3,
                3,
                11,
                11,
                &[fact3],
                &[],
                |_| Ok(()),
            )
            .unwrap()
            .0;
        let reader = BlockIndex::open(&index_path).unwrap();
        assert_eq!(
            reader.file_bounds("rec", 1, 2).unwrap(),
            Some((first.clone(), second.clone()))
        );
        assert_eq!(
            reader.file_bounds("rec", 2, 3).unwrap(),
            Some((third.clone(), third.clone()))
        );
        assert_eq!(
            reader.file_blocks("rec", 1, 2, None, 1).unwrap(),
            vec![first.clone()]
        );
        assert_eq!(
            reader
                .file_blocks("rec", 1, 2, Some(first.range.offset), 1)
                .unwrap(),
            vec![second.clone()]
        );
        assert_eq!(reader.time_at_block_end("rec", 2).unwrap(), Some(10));
        assert_eq!(
            reader.blocks("rec", 1, None, 8).unwrap(),
            vec![first.clone()]
        );
        assert_eq!(
            reader
                .blocks("rec", 3, Some((first.part, first.range.offset)), 8)
                .unwrap(),
            vec![second.clone(), third.clone()]
        );
        assert_eq!(
            reader
                .blocks_in_time("rec", 3, None, 8, Some((10, 11)))
                .unwrap(),
            vec![first.clone(), second.clone()]
        );
        assert_eq!(
            reader
                .blocks_in_time("rec", 3, None, 8, Some((11, 12)))
                .unwrap(),
            vec![third.clone()]
        );
        assert_eq!(
            reader
                .blocks_in_time("rec", 1, None, 8, Some((10, 11)))
                .unwrap(),
            vec![first.clone()]
        );
        assert_eq!(
            reader.fact_at("rec", "market", "T", 1).unwrap(),
            Some((1, Some(first_value)))
        );
        assert_eq!(
            reader.fact_at("rec", "market", "T", 2).unwrap(),
            Some((2, Some(updated)))
        );
        assert_eq!(
            reader.fact_at("rec", "market", "T", 3).unwrap(),
            Some((3, None))
        );
        assert!(reader.fact_at("other", "market", "T", 3).unwrap().is_none());
        let records = read_block(&path2, &third.range).unwrap();
        assert!(records.iter().any(
            |record| matches!(record, BlockRecord::Message(message) if message.data == b"third")
        ));
        assert!(reader.blocks("rec", 3, None, 0).is_err());
        writer2.finish().unwrap();
        drop(writer2);
        drop(sync1);
        drop(sync2);
        drop(index);
        drop(reader);
        fs::remove_dir_all(root).unwrap();
    }
}
