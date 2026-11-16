//! Compact raw locations, not facts: immutable pages are published with their source commit.
use crate::execution_replay::{failure, io_error, ReplayResult, JS_SAFE};
use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

pub const PAGE_ROWS: usize = 256;
pub const BLOCK_ROWS: usize = 1024;
pub const COMMIT_ROWS: usize = 32_768;

pub struct Location {
    pub seq: u64,
    pub time: u64,
    pub block: u64,
    pub line: usize,
}

fn varint(output: &mut Vec<u8>, mut value: u64) {
    while value >= 128 {
        output.push((value as u8 & 127) | 128);
        value >>= 7;
    }
    output.push(value as u8);
}

fn seal(bytes: &mut Vec<u8>) {
    let hash = Sha256::digest(&bytes);
    bytes.extend_from_slice(&hash[..16]);
}

pub fn block_positions(rows: &[(u64, u64)]) -> ReplayResult<Vec<u8>> {
    if rows.is_empty() || rows.len() > BLOCK_ROWS {
        return Err(failure("BUDGET", "raw block location count"));
    }
    let mut output = Vec::new();
    varint(&mut output, rows.len() as u64);
    varint(&mut output, rows[0].1);
    let (mut sequence, mut time) = rows[0];
    for (i, &(seq, observed)) in rows.iter().enumerate() {
        if seq == 0
            || seq > JS_SAFE
            || observed > JS_SAFE
            || observed < time
            || i > 0 && seq <= sequence
        {
            return Err(failure("CORRUPT", "raw block locations"));
        }
        varint(&mut output, seq - sequence);
        varint(&mut output, observed - time);
        sequence = seq;
        time = observed;
    }
    seal(&mut output);
    Ok(output)
}

pub fn publish(
    db: &Connection,
    recording: &str,
    rows: &mut [Location],
    through: u64,
    watermark: u64,
) -> ReplayResult<()> {
    if rows.len() > COMMIT_ROWS {
        return Err(failure("BUDGET", "raw commit location count"));
    }
    rows.sort_unstable_by_key(|row| row.seq);
    let (mut sequence, mut time): (u64, u64) = db
        .query_row(
            "SELECT seq,watermark FROM recordings WHERE id=?1",
            [recording],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(io_error)?;
    if watermark < time || watermark > JS_SAFE || through > JS_SAFE {
        return Err(failure("CORRUPT", "raw location watermark"));
    }
    for row in rows.iter() {
        if row.seq != sequence + 1
            || row.time < time
            || row.time > watermark
            || row.block == 0
            || row.block > u32::MAX as u64
            || row.line >= BLOCK_ROWS
        {
            return Err(failure("CORRUPT", "raw location prefix"));
        }
        sequence = row.seq;
        time = row.time;
    }
    if sequence != through {
        return Err(failure("CORRUPT", "raw location prefix length"));
    }
    // A rowid table keeps these blobs in leaf pages instead of mostly empty overflow pages.
    for page in rows.chunks(PAGE_ROWS) {
        let mut bytes = Vec::new();
        let (mut time, mut block) = (page[0].time, 0i64);
        for row in page {
            varint(&mut bytes, row.time - time);
            let delta = row.block as i64 - block;
            varint(&mut bytes, ((delta << 1) ^ (delta >> 63)) as u64);
            varint(&mut bytes, row.line as u64);
            time = row.time;
            block = row.block as i64;
        }
        seal(&mut bytes);
        db.execute(
            "INSERT INTO location_pages VALUES (?1,?2,?3,?4,?5,?6)",
            params![
                recording,
                page[0].seq,
                page.last().unwrap().seq,
                page[0].time,
                time,
                bytes
            ],
        )
        .map_err(io_error)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn block_positions_encode_only_deltas_and_reject_invalid_order() {
        let encoded = block_positions(&[(5, 100), (12, 112)]).unwrap();
        assert_eq!(&encoded[..6], &[2, 100, 0, 0, 7, 12]);
        assert_eq!(&encoded[6..], &Sha256::digest(&encoded[..6])[..16]);
        assert!(block_positions(&[(5, 100), (4, 112)]).is_err());
        assert!(block_positions(&vec![(1, 1); BLOCK_ROWS + 1]).is_err());
    }
    #[test]
    fn global_pages_sort_interleaved_streams_and_split_at_a_fixed_bound() {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(include_str!("execution_replay_raw.sql"))
            .unwrap();
        db.execute("INSERT INTO recordings(id,context) VALUES ('R','{}')", [])
            .unwrap();
        let mut rows = (1..=600)
            .rev()
            .map(|seq| Location {
                seq,
                time: seq / 3,
                block: if seq % 2 == 0 { 1 } else { 2 },
                line: (seq / 2) as usize,
            })
            .collect::<Vec<_>>();
        publish(&db, "R", &mut rows, 600, 200).unwrap();
        let ranges = db
            .prepare("SELECT first_seq,last_seq FROM location_pages ORDER BY first_seq")
            .unwrap()
            .query_map([], |row| Ok((row.get::<_, u64>(0)?, row.get::<_, u64>(1)?)))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(ranges, vec![(1, 256), (257, 512), (513, 600)]);
    }
}
