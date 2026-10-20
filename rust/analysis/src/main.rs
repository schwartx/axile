use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use axile_analysis::error::AnalysisError;
use axile_analysis::{capabilities, query, query_stream, MAX_REQUEST_BYTES};

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let mut stderr = io::stderr().lock();
            let _ = serde_json::to_writer(&mut stderr, &error);
            let _ = stderr.write_all(b"\n");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), AnalysisError> {
    let mut arguments = std::env::args_os().skip(1);
    let command = arguments.next().unwrap_or_default();
    if command == "--version" || command == "-V" {
        println!("axile-analysis {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if command == "--help" || command == "-h" {
        println!(
            "axile-analysis query --database PATH\n\
             axile-analysis capabilities\n\n\
             query reads one JSON request from stdin and writes its JSON result to stdout.\n\
             Requests with command publish stream JSONL frames ending in a complete frame."
        );
        return Ok(());
    }
    if command == "capabilities" {
        return write_result(&capabilities());
    }
    if command != "query" {
        return Err(AnalysisError::new(
            "INVALID_ARGUMENT",
            "expected query --database PATH or capabilities",
        ));
    }
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--database")) {
        return Err(AnalysisError::new(
            "INVALID_ARGUMENT",
            "query requires --database PATH",
        ));
    }
    let database = arguments
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| AnalysisError::new("INVALID_ARGUMENT", "query requires --database PATH"))?;
    if arguments.next().is_some() {
        return Err(AnalysisError::new(
            "INVALID_ARGUMENT",
            "unexpected argument after database path",
        ));
    }
    let mut input = Vec::new();
    io::stdin()
        .lock()
        .take(MAX_REQUEST_BYTES + 1)
        .read_to_end(&mut input)
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))?;
    if input.len() as u64 > MAX_REQUEST_BYTES {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "request exceeds 1 MiB",
        ));
    }
    let request: serde_json::Value = serde_json::from_slice(&input)
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))?;
    if request.get("command").and_then(serde_json::Value::as_str) == Some("publish") {
        let mut stdout = io::BufWriter::new(io::stdout().lock());
        query_stream(&database, &request, &mut stdout)
    } else {
        write_result(&query(&database, &request)?)
    }
}

fn write_result(result: &serde_json::Value) -> Result<(), AnalysisError> {
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    serde_json::to_writer(&mut stdout, result)
        .map_err(|error| AnalysisError::new("RESULT_WRITE", error.to_string()))?;
    stdout
        .write_all(b"\n")
        .and_then(|()| stdout.flush())
        .map_err(|error| AnalysisError::new("RESULT_WRITE", error.to_string()))
}
