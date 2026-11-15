//! GIL-free Python boundary; Python owns the bounded recorder queue.

use std::path::Path;
use std::sync::{Mutex, MutexGuard};

use pyo3::prelude::*;

use crate::execution_replay::{self, ReplayResult};
use crate::execution_replay_raw::RawWriter;

#[pyclass]
struct ReplayReadOwner {
    _lock: std::fs::File,
}

#[pyclass]
struct ReplayReadLease {
    _lock: std::fs::File,
}
#[pymethods]
impl ReplayReadLease {
    #[new]
    fn new(py: Python<'_>, root: &str, account: u64, recording: &str) -> PyResult<Self> {
        py.detach(|| {
            let directory = execution_replay::account_directory(Path::new(root), account, false)?;
            execution_replay::identifier(recording)?;
            let path = execution_replay::child_file(&directory, &format!(".raw-{recording}.lock"))?;
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)
                .map_err(execution_replay::io_error)?;
            fs2::FileExt::try_lock_shared(&file).map_err(execution_replay::io_error)?;
            Ok(Self { _lock: file })
        })
        .map_err(crate::python::raise)
    }
}

#[pymethods]
impl ReplayReadOwner {
    #[new]
    fn new(py: Python<'_>, root: &str) -> PyResult<Self> {
        py.detach(|| {
            let directory = Path::new(root)
                .canonicalize()
                .map_err(execution_replay::io_error)?;
            let path = execution_replay::child_file(&directory, ".read-owner-v2.lock")?;
            let file = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)
                .map_err(execution_replay::io_error)?;
            fs2::FileExt::try_lock_exclusive(&file).map_err(|_| {
                execution_replay::failure("BUSY", "read executor still owns replay I/O")
            })?;
            Ok(Self { _lock: file })
        })
        .map_err(crate::python::raise)
    }
}

#[pyclass]
struct ExecutionReplayWriter {
    writer: Mutex<RawWriter>,
}

impl ExecutionReplayWriter {
    fn locked(&self) -> ReplayResult<MutexGuard<'_, RawWriter>> {
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
        py.detach(|| RawWriter::new(Path::new(root), account_id, recording_id, context_json))
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

#[pyfunction]
fn execution_replay_rebuild(py: Python<'_>, root: &str, account_id: u64) -> PyResult<String> {
    py.detach(|| {
        crate::execution_replay_raw_rebuild::rebuild(Path::new(root), account_id)
            .map(|v| v.to_string())
    })
    .map_err(crate::python::raise)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(execution_replay_rebuild, module)?)?;
    module.add_class::<ReplayReadOwner>()?;
    module.add_class::<ReplayReadLease>()?;
    module.add_class::<ExecutionReplayWriter>()?;
    module.add("EXECUTION_REPLAY_SCHEMA", 2)?;
    Ok(())
}
