//! Oversized observations are stored in independently bounded MCAP fragments.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::path::Path;

use mcap::records::MessageHeader;
use serde::{Deserialize, Serialize};

use crate::execution_replay::{
    child_file, digest, failure, identifier, io_error, read_bounded, sync_directory, CappedFile,
    ReplayResult, JS_SAFE, PART_FILE_LIMIT,
};

const PREFIX: &[u8] = b"{\"__axile_fragment__\":";
const TOPIC: &str = "/axile/replay-fragment";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Piece {
    size: u64,
    sha256: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Reference {
    recording: String,
    sequence: u64,
    sha256: String,
    pieces: Vec<Piece>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope {
    __axile_fragment__: Reference,
}

fn name(recording: &str, sequence: u64, number: usize) -> String {
    format!("fragment-{recording}-{sequence}-{number:06}.mcap")
}

fn validate(reference: &Reference) -> ReplayResult<()> {
    identifier(&reference.recording)?;
    let hash =
        |value: &str| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit());
    if reference.sequence == 0
        || reference.sequence > JS_SAFE
        || reference.pieces.is_empty()
        || !hash(&reference.sha256)
        || reference
            .pieces
            .iter()
            .any(|piece| piece.size == 0 || piece.size > PART_FILE_LIMIT || !hash(&piece.sha256))
    {
        return Err(failure("CORRUPT", "invalid replay fragment reference"));
    }
    Ok(())
}

pub(crate) fn write(
    directory: &Path,
    recording: &str,
    sequence: u64,
    payload: &[u8],
    limit: u64,
) -> ReplayResult<Vec<u8>> {
    identifier(recording)?;
    let chunk_size = usize::try_from(limit.saturating_sub(64 * 1024)).map_err(io_error)?;
    if chunk_size < 1024 {
        return Err(failure("BUDGET", "file limit cannot contain a fragment"));
    }
    let mut pieces = Vec::new();
    for (number, bytes) in payload.chunks(chunk_size).enumerate() {
        let file_name = name(recording, sequence, number);
        let sealed = child_file(directory, &file_name)?;
        let partial = child_file(directory, &format!("{file_name}.partial"))?;
        if sealed.exists() {
            return Err(failure("IDENTITY", "fragment already exists"));
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&partial)
            .map_err(io_error)?;
        let sync = file.try_clone().map_err(io_error)?;
        let mut writer = mcap::WriteOptions::new()
            .compression(None)
            .emit_message_indexes(false)
            .create(CappedFile { file, limit })
            .map_err(io_error)?;
        let schema = writer
            .add_schema("AxileReplayFragment.v1", "binary", b"")
            .map_err(io_error)?;
        let channel = writer
            .add_channel(schema, TOPIC, "binary", &BTreeMap::new())
            .map_err(io_error)?;
        writer
            .write_to_known_channel(
                &MessageHeader {
                    channel_id: channel,
                    sequence: 0,
                    log_time: 0,
                    publish_time: 0,
                },
                bytes,
            )
            .map_err(io_error)?;
        writer.finish().map_err(io_error)?;
        sync.sync_all().map_err(io_error)?;
        drop(writer);
        drop(sync);
        let file_bytes = read_bounded(&partial, limit as usize)?;
        pieces.push(Piece {
            size: file_bytes.len() as u64,
            sha256: digest(&file_bytes),
        });
        fs::rename(&partial, &sealed).map_err(io_error)?;
    }
    sync_directory(directory)?;
    serde_json::to_vec(&Envelope {
        __axile_fragment__: Reference {
            recording: recording.to_owned(),
            sequence,
            sha256: digest(payload),
            pieces,
        },
    })
    .map_err(io_error)
}

pub(crate) fn restore(path: &Path, bytes: &[u8]) -> ReplayResult<Option<Vec<u8>>> {
    // shortcut: each response page reassembles the full event; stream fragments if latency matters.
    if !bytes.starts_with(PREFIX) {
        return Ok(None);
    }
    let envelope: Envelope =
        serde_json::from_slice(bytes).map_err(|error| failure("CORRUPT", error.to_string()))?;
    let reference = envelope.__axile_fragment__;
    validate(&reference)?;
    let directory = path
        .parent()
        .ok_or_else(|| failure("PATH", "recording has no directory"))?;
    let mut payload = Vec::new();
    for (number, piece) in reference.pieces.iter().enumerate() {
        let path = child_file(
            directory,
            &name(&reference.recording, reference.sequence, number),
        )?;
        let bytes = read_bounded(&path, PART_FILE_LIMIT as usize)?;
        if bytes.len() as u64 != piece.size || digest(&bytes) != piece.sha256 {
            return Err(failure(
                "CORRUPT",
                "replay fragment differs from its reference",
            ));
        }
        let mut messages = mcap::MessageStream::new(&bytes).map_err(io_error)?;
        let message = messages
            .next()
            .ok_or_else(|| failure("CORRUPT", "empty replay fragment"))?
            .map_err(io_error)?;
        if message.channel.topic != TOPIC
            || message.channel.message_encoding != "binary"
            || message.channel.schema.as_ref().is_none_or(|schema| {
                schema.name != "AxileReplayFragment.v1" || schema.encoding != "binary"
            })
        {
            return Err(failure("CORRUPT", "invalid replay fragment channel"));
        }
        payload.extend_from_slice(&message.data);
        if messages.next().is_some() {
            return Err(failure("CORRUPT", "multiple replay fragment messages"));
        }
    }
    if digest(&payload) != reference.sha256 {
        return Err(failure(
            "CORRUPT",
            "reassembled replay event differs from its reference",
        ));
    }
    Ok(Some(payload))
}

pub(crate) fn check_present(directory: &Path, envelope: &serde_json::Value) -> ReplayResult<()> {
    let envelope: Envelope = serde_json::from_value(envelope.clone())
        .map_err(|error| failure("CORRUPT", error.to_string()))?;
    let reference = envelope.__axile_fragment__;
    validate(&reference)?;
    for (number, piece) in reference.pieces.iter().enumerate() {
        let path = child_file(
            directory,
            &name(&reference.recording, reference.sequence, number),
        )?;
        if fs::metadata(path).map_or(true, |metadata| metadata.len() != piece.size) {
            return Err(failure(
                "INCOMPLETE",
                "replay event fragment is missing or truncated",
            ));
        }
    }
    Ok(())
}
