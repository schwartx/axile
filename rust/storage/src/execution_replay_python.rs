//! GIL-free Python boundary; Python owns the bounded recorder queue.

use std::path::Path;
use std::sync::{Mutex, MutexGuard};

use pyo3::prelude::*;

use crate::execution_replay::{self, ReplayResult};

#[pyclass]
struct ExecutionReplayWriter {
    writer: Mutex<execution_replay::ExecutionReplayWriter>,
}

impl ExecutionReplayWriter {
    fn locked(&self) -> ReplayResult<MutexGuard<'_, execution_replay::ExecutionReplayWriter>> {
        self.writer
            .lock()
            .map_err(|_| execution_replay::failure("STOPPED", "recorder lock is poisoned"))
    }
}

#[pymethods]
impl ExecutionReplayWriter {
    #[new]
    fn new(
        py: Python<'_>,
        root: &str,
        account_id: u64,
        recording_id: &str,
        context_json: &str,
    ) -> PyResult<Self> {
        py.detach(|| {
            execution_replay::ExecutionReplayWriter::new(
                Path::new(root),
                account_id,
                recording_id,
                context_json,
            )
        })
        .map(|writer| Self {
            writer: Mutex::new(writer),
        })
        .map_err(crate::python::raise)
    }

    fn append(&self, py: Python<'_>, message_json: &str) -> PyResult<()> {
        py.detach(|| self.locked()?.append(message_json))
            .map_err(crate::python::raise)
    }

    fn seal(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.locked()?.seal())
            .map_err(crate::python::raise)
    }

    fn finish(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.locked()?.finish())
            .map_err(crate::python::raise)
    }

    fn poll(&self, py: Python<'_>, now_us: u64) -> PyResult<()> {
        py.detach(|| self.locked()?.poll(now_us))
            .map_err(crate::python::raise)
    }

    fn status_json(&self, py: Python<'_>) -> PyResult<String> {
        py.detach(|| Ok(self.locked()?.status().to_string()))
            .map_err(crate::python::raise)
    }
}

#[pyfunction(signature = (root, account_id, execution_id, execution_end_us=None))]
fn execution_replay_manifest(
    py: Python<'_>,
    root: &str,
    account_id: u64,
    execution_id: &str,
    execution_end_us: Option<u64>,
) -> PyResult<String> {
    py.detach(|| {
        crate::execution_replay_reader::manifest(
            Path::new(root),
            account_id,
            execution_id,
            execution_end_us,
        )
    })
    .map(|value| value.to_string())
    .map_err(crate::python::raise)
}

#[pyfunction]
fn execution_replay_window(
    py: Python<'_>,
    root: &str,
    account_id: u64,
    execution_id: &str,
    request_json: &str,
) -> PyResult<String> {
    py.detach(|| {
        crate::execution_replay_reader::window(
            Path::new(root),
            account_id,
            execution_id,
            request_json,
        )
    })
    .map(|value| value.to_string())
    .map_err(crate::python::raise)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ExecutionReplayWriter>()?;
    module.add_function(wrap_pyfunction!(execution_replay_manifest, module)?)?;
    module.add_function(wrap_pyfunction!(execution_replay_window, module)?)?;
    module.add("EXECUTION_REPLAY_SCHEMA", 1)?;
    Ok(())
}
