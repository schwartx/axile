//! Thin Python boundary for process-local diagnostics.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::logging;

#[pyfunction]
#[pyo3(signature = (directory, capacity=4096, max_bytes=134217728, rotation_seconds=86400, minimum="INFO", rotation_json=None))]
fn logging_start(
    py: Python<'_>,
    directory: &str,
    capacity: usize,
    max_bytes: u64,
    rotation_seconds: u64,
    minimum: &str,
    rotation_json: Option<&str>,
) -> PyResult<()> {
    py.detach(|| {
        logging::start(
            directory,
            capacity,
            max_bytes,
            rotation_seconds,
            minimum,
            rotation_json,
        )
    })
    .map_err(PyValueError::new_err)
}

#[pyfunction]
fn logging_emit(payload: &str) -> bool {
    logging::emit_json(payload)
}

#[pyfunction]
fn logging_stats() -> String {
    logging::stats().to_string()
}

#[pyfunction]
#[pyo3(signature = (run_id=None))]
fn logging_console(run_id: Option<&str>) -> Option<String> {
    logging::console(run_id)
}

#[pyfunction]
#[pyo3(signature = (run_id=None))]
fn logging_archive(run_id: Option<&str>) -> Option<String> {
    logging::archive(run_id)
}

#[pyfunction]
fn logging_invalid() {
    logging::invalid();
}

#[pyfunction]
#[pyo3(signature = (run_id=None))]
fn logging_console_failed(run_id: Option<&str>) {
    logging::console_failed(run_id);
}

#[pyfunction]
#[pyo3(signature = (run_id=None))]
fn logging_archive_failed(run_id: Option<&str>) {
    logging::archive_failed(run_id);
}

#[pyfunction]
#[pyo3(signature = (timeout_ms=200))]
fn logging_close(py: Python<'_>, timeout_ms: u64) {
    py.detach(|| logging::close(timeout_ms));
}

#[pyfunction]
#[pyo3(signature = (name, context_json, parent=None))]
fn logging_span(name: String, context_json: &str, parent: Option<u64>) -> PyResult<String> {
    let context = serde_json::from_str(context_json)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    serde_json::to_string(&logging::span(name, parent, context))
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
fn logging_scope(payload: &str) -> PyResult<String> {
    let value =
        serde_json::from_str(payload).map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(logging::scope(value).to_string())
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(logging_start, module)?)?;
    module.add_function(wrap_pyfunction!(logging_emit, module)?)?;
    module.add_function(wrap_pyfunction!(logging_stats, module)?)?;
    module.add_function(wrap_pyfunction!(logging_console, module)?)?;
    module.add_function(wrap_pyfunction!(logging_archive, module)?)?;
    module.add_function(wrap_pyfunction!(logging_invalid, module)?)?;
    module.add_function(wrap_pyfunction!(logging_console_failed, module)?)?;
    module.add_function(wrap_pyfunction!(logging_archive_failed, module)?)?;
    module.add_function(wrap_pyfunction!(logging_close, module)?)?;
    module.add_function(wrap_pyfunction!(logging_span, module)?)?;
    module.add_function(wrap_pyfunction!(logging_scope, module)?)?;
    Ok(())
}
