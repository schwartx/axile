//! Bounded validation and decoding of an immutable, committed MCAP byte range.

use std::fs::{self, File};
use std::io::{Cursor, Read, Seek, SeekFrom};
use std::path::Path;

use mcap::records::{Channel, Header, MessageHeader, Record, SchemaHeader};
use mcap::sans_io::{LinearReadEvent, LinearReader, LinearReaderOptions};

use crate::execution_replay::{
    digest, failure, io_error, ReplayResult, PART_FILE_LIMIT, SCAN_BYTES, SCAN_EVENTS,
};

/// A committed range contains only complete MCAP records. The first range includes magic.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub(crate) struct CommittedBlock {
    pub offset: u64,
    pub length: u64,
    pub sha256: String,
}

pub(crate) struct BlockMessage {
    pub header: MessageHeader,
    pub data: Vec<u8>,
}

pub(crate) enum BlockRecord {
    Header(Header),
    Schema(SchemaHeader, Vec<u8>),
    Channel(Channel),
    Message(BlockMessage),
}

pub(crate) fn count_chunks(bytes: &[u8], first: bool) -> ReplayResult<u64> {
    let mut position = if first {
        if !bytes.starts_with(mcap::MAGIC) {
            return Err(failure("CORRUPT", "missing replay file magic"));
        }
        mcap::MAGIC.len()
    } else {
        0
    };
    let mut chunks = 0_u64;
    while position < bytes.len() {
        let header = bytes
            .get(position..position.saturating_add(9))
            .ok_or_else(|| failure("CORRUPT", "truncated replay record header"))?;
        let length = u64::from_le_bytes(header[1..9].try_into().unwrap());
        let length = usize::try_from(length)
            .map_err(|_| failure("BUDGET", "record length exceeds budget"))?;
        position = position
            .checked_add(9)
            .and_then(|pos| pos.checked_add(length))
            .filter(|pos| *pos <= bytes.len())
            .ok_or_else(|| failure("CORRUPT", "truncated replay record"))?;
        if header[0] == mcap::records::op::CHUNK {
            chunks += 1;
        }
    }
    Ok(chunks)
}

/// Read exactly one frozen range; never trust a growing file's current length as its watermark.
pub(crate) fn read_block(path: &Path, block: &CommittedBlock) -> ReplayResult<Vec<BlockRecord>> {
    if block.length == 0
        || block.length > SCAN_BYTES
        || block.sha256.len() != 64
        || !block.sha256.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err(failure("CORRUPT", "invalid committed block descriptor"));
    }
    if fs::symlink_metadata(path)
        .map_err(io_error)?
        .file_type()
        .is_symlink()
    {
        return Err(failure("PATH", "replay files must not be symbolic links"));
    }
    let mut file = File::open(path).map_err(io_error)?;
    let end = block
        .offset
        .checked_add(block.length)
        .ok_or_else(|| failure("CORRUPT", "block end overflow"))?;
    if file.metadata().map_err(io_error)?.len() < end {
        return Err(failure("VIEW_CHANGED", "committed block is truncated"));
    }
    file.seek(SeekFrom::Start(block.offset)).map_err(io_error)?;
    let mut bytes = Vec::with_capacity(block.length as usize);
    file.take(block.length)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() as u64 != block.length || digest(&bytes) != block.sha256 {
        return Err(failure("CORRUPT", "committed block hash mismatch"));
    }
    let options = LinearReaderOptions::default()
        .with_skip_start_magic(block.offset != 0)
        .with_skip_end_magic(true)
        .with_prevalidate_chunk_crcs(true)
        .with_record_length_limit(SCAN_BYTES as usize);
    let mut reader = LinearReader::new_with_options(options);
    let mut source = Cursor::new(bytes);
    let mut records = Vec::new();
    let mut decoded_bytes = 0_u64;
    while let Some(event) = reader.next_event() {
        match event.map_err(|error| failure("CORRUPT", error.to_string()))? {
            LinearReadEvent::ReadRequest(need) => {
                let read = source
                    .read(reader.insert(need.min(64 * 1024)))
                    .map_err(io_error)?;
                reader.notify_read(read);
            }
            LinearReadEvent::Record { opcode, data } => {
                decoded_bytes = decoded_bytes.saturating_add(data.len() as u64);
                if decoded_bytes > SCAN_BYTES || records.len() >= SCAN_EVENTS {
                    return Err(failure("BUDGET", "committed block exceeds decoding budget"));
                }
                let record = mcap::parse_record(opcode, data)
                    .map_err(|error| failure("CORRUPT", error.to_string()))?;
                match record {
                    Record::Header(header) => records.push(BlockRecord::Header(header)),
                    Record::Schema { header, data } => {
                        records.push(BlockRecord::Schema(header, data.into_owned()))
                    }
                    Record::Channel(channel) => records.push(BlockRecord::Channel(channel)),
                    Record::Message { header, data } => {
                        if data.len() > PART_FILE_LIMIT as usize {
                            return Err(failure(
                                "BUDGET",
                                "committed block exceeds decoding budget",
                            ));
                        }
                        records.push(BlockRecord::Message(BlockMessage {
                            header,
                            data: data.into_owned(),
                        }));
                    }
                    _ => (),
                }
            }
        }
    }
    Ok(records)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::fs::OpenOptions;
    use std::io::Write;

    #[test]
    fn reads_committed_block_without_reading_prior_or_later_bytes_and_checks_hash() {
        let root = std::env::temp_dir().join(format!(
            "axile-replay-block-{}",
            uuid::Uuid::new_v5(
                &uuid::Uuid::NAMESPACE_OID,
                format!("{:?}", std::thread::current().id()).as_bytes()
            )
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("active.mcap.partial");
        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .read(true)
            .open(&path)
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
        let first = MessageHeader {
            channel_id: channel,
            sequence: 1,
            log_time: 1_000,
            publish_time: 1_000,
        };
        writer.write_to_known_channel(&first, b"first").unwrap();
        writer.flush().unwrap();
        sync.sync_data().unwrap();
        let offset = sync.metadata().unwrap().len();
        writer
            .write_to_known_channel(
                &MessageHeader {
                    sequence: 2,
                    ..first
                },
                b"second",
            )
            .unwrap();
        writer.flush().unwrap();
        sync.sync_data().unwrap();
        let length = sync.metadata().unwrap().len() - offset;
        let mut source = File::open(&path).unwrap();
        source.seek(SeekFrom::Start(offset)).unwrap();
        let mut bytes = Vec::new();
        source.take(length).read_to_end(&mut bytes).unwrap();
        let block = CommittedBlock {
            offset,
            length,
            sha256: digest(&bytes),
        };
        writer
            .write_to_known_channel(
                &MessageHeader {
                    sequence: 3,
                    ..first
                },
                b"third",
            )
            .unwrap();
        writer.flush().unwrap();
        let records = read_block(&path, &block).unwrap();
        assert_eq!(records.len(), 1);
        let BlockRecord::Message(message) = &records[0] else {
            panic!("expected message")
        };
        assert_eq!(message.header.sequence, 2);
        assert_eq!(message.data, b"second");
        writer.finish().unwrap();
        drop(writer);
        drop(sync);
        let mut corrupt = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        corrupt.seek(SeekFrom::Start(offset + length / 2)).unwrap();
        let mut original = [0];
        corrupt.read_exact(&mut original).unwrap();
        corrupt.seek(SeekFrom::Start(offset + length / 2)).unwrap();
        corrupt.write_all(&[original[0] ^ 0xff]).unwrap();
        assert!(read_block(&path, &block).is_err());
        assert!(read_block(
            &path,
            &CommittedBlock {
                length: SCAN_BYTES + 1,
                ..block
            }
        )
        .is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
