//! Bounded SQLite access for the native analysis process.
//!
//! Python owns business facts and migrations. Queries use a consistent read
//! snapshot; publish_store stages and atomically writes analysis-owned results.
//! No historical rows cross the Python/Rust process boundary in publish_store.

pub mod assets;
pub mod calendar;
pub mod costs;
pub mod error;
pub(crate) mod execution_summary;
pub mod native_publication;
pub mod performance;
pub mod publication;
#[cfg(feature = "python")]
pub mod python;
pub mod replay;
pub(crate) mod result_reader;
pub mod snapshots;
pub(crate) mod source;
pub mod submissions;
pub mod views;
pub mod wire_result;

use std::io::{self, Write};
use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

use crate::error::AnalysisError;

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_REQUEST_BYTES: u64 = 1024 * 1024;
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;

pub fn capabilities() -> Value {
    json!({
        "protocol": PROTOCOL_VERSION,
        "version": env!("CARGO_PKG_VERSION"),
        "commands": ["costs", "snapshot", "performance_result", "publish", "publish_store"],
        "streaming_commands": ["publish"],
        "max_frame_bytes": MAX_FRAME_BYTES,
        "read_only": false,
        "write_commands": ["publish_store"],
    })
}

/// Execute one complete analysis task against a consistent SQLite read view.
pub fn query(database: &Path, request: &Value) -> Result<Value, AnalysisError> {
    let command = request_command(request)?;
    if command == "publish_store" {
        return native_publication::publish(database, request);
    }
    if !matches!(command, "costs" | "snapshot" | "performance_result") {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            format!("unsupported command: {command}"),
        ));
    }

    with_read_snapshot(database, |connection| {
        match command {
            "costs" => costs::query_costs(connection, request),
            "snapshot" => snapshots::query(connection, request),
            _ => snapshots::performance_result(connection, request),
        }
        .map_err(AnalysisError::from)
    })
}

/// Preserve typed curve storage until serialization, after the read view closes.
pub fn query_wire(
    database: &Path,
    request: &Value,
) -> Result<wire_result::WireResult, AnalysisError> {
    let command = request_command(request)?;
    if matches!(command, "snapshot" | "performance_result") {
        return with_read_snapshot(database, |connection| {
            match command {
                "snapshot" => snapshots::query_wire(connection, request),
                _ => snapshots::performance_result_wire(connection, request),
            }
            .map_err(AnalysisError::from)
        });
    }
    query(database, request).map(|value| wire_result::WireResult {
        value,
        points: None,
        performance_only: false,
    })
}

/// Stream normalized publication rows without collecting an encoded response.
///
/// A complete frame is required before success, and no frame may follow it.
/// The caller must also check the process exit status before publishing rows.
pub fn query_stream(
    database: &Path,
    request: &Value,
    writer: &mut dyn Write,
) -> Result<(), AnalysisError> {
    if request_command(request)? != "publish" {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "streaming requests require command publish",
        ));
    }
    with_read_snapshot(database, |connection| {
        let mut complete = false;
        publication::write(connection, request, &mut |frame| {
            if complete {
                return Err("INVALID_STREAM: frame follows complete".into());
            }
            if !frame.is_object() {
                return Err("INVALID_STREAM: frame must be an object".into());
            }
            write_frame(writer, frame).map_err(|error| format!("RESULT_WRITE: {error}"))?;
            complete = frame.get("type").and_then(Value::as_str) == Some("complete");
            Ok(())
        })
        .map_err(AnalysisError::from)?;
        if !complete {
            return Err(AnalysisError::new(
                "INVALID_STREAM",
                "publication ended without a complete frame",
            ));
        }
        Ok(())
    })
}

fn request_command(request: &Value) -> Result<&str, AnalysisError> {
    request
        .as_object()
        .and_then(|request| request.get("command"))
        .and_then(Value::as_str)
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "command must be a string"))
}

fn with_read_snapshot<T>(
    database: &Path,
    task: impl FnOnce(&Connection) -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    // Read-only mode prevents accidental source writes, but deliberately avoids
    // immutable=1: live Python commits and WAL contents must remain visible.
    let connection = Connection::open_with_flags(
        database,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;
    // Cap the worker page cache rather than relying on SQLite's build defaults.
    // Sorting can use a temporary file; mapped database pages are disabled so
    // process working-set comparisons include the actual read cache.
    connection
        .execute_batch(
            "PRAGMA query_only = ON;
             PRAGMA cache_size = -4096;
             PRAGMA mmap_size = 0;
             PRAGMA temp_store = FILE;
             BEGIN DEFERRED;",
        )
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;

    let result = task(&connection);

    // End the snapshot promptly. Drop still closes the transaction on errors,
    // including an error while rolling back a damaged connection.
    let closed = connection
        .execute_batch("ROLLBACK;")
        .map_err(|error| AnalysisError::new("DATABASE_CLOSE", error.to_string()));
    match result {
        Ok(value) => closed.map(|()| value),
        Err(error) => Err(error),
    }
}

/// Count bytes while writing directly to the pipe. Oversized frames fail the
/// task rather than allocating a second serialized copy in memory.
fn write_frame(writer: &mut dyn Write, frame: &Value) -> io::Result<()> {
    let mut limited = FrameWriter {
        writer,
        remaining: MAX_FRAME_BYTES - 1,
    };
    serde_json::to_writer(&mut limited, frame).map_err(io::Error::other)?;
    limited.writer.write_all(b"\n")?;
    limited.writer.flush()
}

struct FrameWriter<'a> {
    writer: &'a mut dyn Write,
    remaining: usize,
}

impl Write for FrameWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.remaining {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "publication frame exceeds 1 MiB",
            ));
        }
        let written = self.writer.write(bytes)?;
        self.remaining -= written;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use serde_json::json;

    #[test]
    fn invalid_commands_do_not_open_a_database() {
        let result = super::query(
            Path::new("/nonexistent.sqlite"),
            &json!({"command": "write"}),
        );
        assert_eq!(result.unwrap_err().code, "INVALID_REQUEST");
    }

    #[test]
    fn missing_database_is_not_created() {
        let result = super::query(
            Path::new("/nonexistent-directory/analysis.sqlite"),
            &json!({"command": "costs"}),
        );
        assert_eq!(result.unwrap_err().code, "DATABASE_OPEN");
    }

    #[test]
    fn streaming_rejects_regular_queries_without_opening_database() {
        let mut output = Vec::new();
        let result = super::query_stream(
            Path::new("/nonexistent.sqlite"),
            &json!({"command": "costs"}),
            &mut output,
        );
        assert_eq!(result.unwrap_err().code, "INVALID_REQUEST");
        assert!(output.is_empty());
    }

    #[test]
    fn streamed_frame_is_one_json_line() {
        let frame = json!({"type": "rows", "rows": [{"symbol": "BTC\\nUSDT"}]});
        let mut output = Vec::new();
        super::write_frame(&mut output, &frame).unwrap();
        assert_eq!(output.iter().filter(|byte| **byte == b'\n').count(), 1);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&output).unwrap(),
            frame
        );
    }

    #[test]
    fn oversized_frames_fail_without_a_complete_line() {
        let frame = json!({"type": "rows", "value": "x".repeat(super::MAX_FRAME_BYTES)});
        let mut output = Vec::new();
        assert!(super::write_frame(&mut output, &frame).is_err());
        assert!(output.len() < super::MAX_FRAME_BYTES);
        assert!(!output.ends_with(b"\n"));
    }
}
