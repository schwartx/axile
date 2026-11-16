//! Request-scoped native index snapshots; no caller-supplied SQL or writes.

use pyo3::{prelude::*, types::PyBytes};
use rusqlite::{types::Value, Connection, OpenFlags};
use std::{sync::Mutex, time::Duration};

use crate::execution_replay::{failure, io_error, ReplayResult};

#[pyclass]
pub(super) struct ReplayIndex {
    connection: Mutex<Option<Connection>>,
}

fn error(error: impl std::fmt::Display) -> crate::error::AnalysisError {
    failure("INDEX", error.to_string())
}

#[pymethods]
impl ReplayIndex {
    #[new]
    fn new(py: Python<'_>, path: &str, progress: Py<PyAny>) -> PyResult<Self> {
        py.detach(|| {
            let path = std::path::Path::new(path);
            if std::fs::symlink_metadata(path).map_err(io_error)?.file_type().is_symlink() {
                return Err(failure("PATH", "replay index is a symlink"));
            }
            let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
                .map_err(error)?;
            connection.set_prepared_statement_cache_capacity(64);
            connection.busy_timeout(Duration::from_millis(100)).map_err(error)?;
            connection.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-2048; PRAGMA mmap_size=0; PRAGMA temp_store=FILE;").map_err(error)?;
            let progress = Mutex::new(progress);
            connection.progress_handler(100, Some(move || Python::attach(|py| {
                progress.lock().ok().and_then(|callback| callback.bind(py).call0().ok())
                    .and_then(|v| v.extract::<i32>().ok()).map(|n| n != 0).unwrap_or(true)
            })));
            connection.execute_batch("BEGIN").map_err(error)?;
            Ok(Self { connection: Mutex::new(Some(connection)) })
        }).map_err(crate::python::raise)
    }

    fn lookup(
        &self,
        py: Python<'_>,
        operation: &str,
        parameters: &str,
    ) -> PyResult<(Vec<String>, Vec<Vec<Py<PyAny>>>)> {
        let (columns, rows) = py
            .detach(|| -> ReplayResult<_> {
                if parameters.len() > 600 * 1024 {
                    return Err(failure("BUDGET", "replay lookup parameters"));
                }
                let input: Vec<serde_json::Value> =
                    serde_json::from_str(parameters).map_err(error)?;
                if input.len() > 144 {
                    return Err(failure("BUDGET", "replay lookup parameters"));
                }
                let values = input
                    .into_iter()
                    .map(|v| match v {
                        serde_json::Value::String(v) => Ok(Value::Text(v)),
                        serde_json::Value::Number(v) => v
                            .as_i64()
                            .map(Value::Integer)
                            .ok_or_else(|| failure("INPUT", "replay lookup integer")),
                        _ => Err(failure("INPUT", "replay lookup scalar")),
                    })
                    .collect::<ReplayResult<Vec<_>>>()?;
                let sql = crate::execution_replay_raw_queries::query(operation, values.len())
                    .map_err(|v| failure("INPUT", v))?;
                let guard = self.connection.lock().map_err(error)?;
                let connection = guard
                    .as_ref()
                    .ok_or_else(|| failure("STOPPED", "replay index is closed"))?;
            let mut statement = connection.prepare_cached(&sql).map_err(error)?;
                let columns = statement
                    .column_names()
                    .iter()
                    .map(|v| v.to_string())
                    .collect::<Vec<_>>();
                let mut cursor = statement
                    .query(rusqlite::params_from_iter(values))
                    .map_err(error)?;
                let mut rows = Vec::new();
                let mut bytes = 0;
                while let Some(row) = cursor.next().map_err(error)? {
                    if rows.len() == 256 {
                        return Err(failure("BUDGET", "replay lookup rows"));
                    }
                    let mut result = Vec::new();
                    for index in 0..columns.len() {
                        let value: Value = row.get(index).map_err(error)?;
                        bytes += match &value {
                            Value::Text(v) => v.len(),
                            Value::Blob(v) => v.len(),
                            _ => 8,
                        };
                        if bytes > 1024 * 1024 {
                            return Err(failure("BUDGET", "replay lookup bytes"));
                        }
                        result.push(value);
                    }
                    rows.push(result);
                }
                Ok((columns, rows))
            })
            .map_err(crate::python::raise)?;
        let rows = rows
            .into_iter()
            .map(|row| {
                row.into_iter()
                    .map(|value| -> PyResult<Py<PyAny>> {
                        Ok(match value {
                            Value::Null => py.None(),
                            Value::Integer(v) => v.into_pyobject(py)?.into_any().unbind(),
                            Value::Real(v) => v.into_pyobject(py)?.into_any().unbind(),
                            Value::Text(v) => v.into_pyobject(py)?.into_any().unbind(),
                            Value::Blob(v) => PyBytes::new(py, &v).into_any().unbind(),
                        })
                    })
                    .collect::<PyResult<Vec<_>>>()
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok((columns, rows))
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| {
            self.connection.lock().map_err(error)?.take();
            Ok(())
        })
        .map_err(crate::python::raise)
    }
}
