//! Python 绑定:每业务函数一个 typed 入口,进程内直调,不经 JSON 文本协议。

use std::time::Duration;

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::Py;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use std::sync::Mutex;

use crate::error::AnalysisError;
use crate::performance::{self, PerformanceRequest, SourceVersionCheck};
use crate::snapshots;

create_exception!(_core, NativeError, PyException, "原生分析查询错误");

fn raise(error: AnalysisError) -> PyErr {
    NativeError::new_err((error.code, error.error))
}

fn open(path: &str) -> PyResult<Connection> {
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| raise(AnalysisError::new("DATABASE_OPEN", error.to_string())))?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|error| raise(AnalysisError::new("DATABASE_CONFIG", error.to_string())))?;
    // 只读连接;缓存与映射约束沿用 worker 读路径,保证资源口径可比。
    connection
        .execute_batch("PRAGMA query_only = ON;\nPRAGMA cache_size = -4096;\nPRAGMA mmap_size = 0;")
        .map_err(|error| raise(AnalysisError::new("DATABASE_CONFIG", error.to_string())))?;
    Ok(connection)
}

/// 只读 SQLite 访问点;每次查询一个 DEFERRED 快照,结束即释放。
#[pyclass]
struct Store {
    connection: Mutex<Connection>,
}

#[pymethods]
impl Store {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        Ok(Self {
            connection: Mutex::new(open(path)?),
        })
    }
}

impl Store {
    fn with_snapshot<T>(
        &self,
        task: impl FnOnce(&Connection) -> Result<T, String>,
    ) -> Result<T, AnalysisError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AnalysisError::new("DATABASE_LOCK", "store connection mutex poisoned"))?;
        connection
            .execute_batch("BEGIN DEFERRED;")
            .map_err(|error| AnalysisError::new("DATABASE_BEGIN", error.to_string()))?;
        let result = task(&connection);
        let closed = connection
            .execute_batch("ROLLBACK;")
            .map_err(|error| AnalysisError::new("DATABASE_CLOSE", error.to_string()));
        match result {
            Ok(value) => closed.map(|()| value),
            Err(error) => Err(AnalysisError::from(error)),
        }
    }
}

fn json_to_py(py: Python<'_>, value: &Value) -> Py<PyAny> {
    match value {
        Value::Null => py.None().into_any(),
        Value::Bool(inner) => inner
            .into_pyobject(py)
            .unwrap()
            .to_owned()
            .into_any()
            .unbind(),
        Value::Number(number) => {
            if let Some(inner) = number.as_i64() {
                inner.into_pyobject(py).unwrap().into_any().unbind()
            } else {
                number
                    .as_f64()
                    .into_pyobject(py)
                    .unwrap()
                    .into_any()
                    .unbind()
            }
        }
        Value::String(inner) => inner.into_pyobject(py).unwrap().into_any().unbind(),
        Value::Array(items) => items
            .iter()
            .map(|item| json_to_py(py, item))
            .collect::<Vec<Py<PyAny>>>()
            .into_pyobject(py)
            .unwrap()
            .into_any()
            .unbind(),
        Value::Object(map) => map
            .iter()
            .map(|(key, item)| (key, json_to_py(py, item)))
            .collect::<std::collections::HashMap<&std::string::String, Py<PyAny>>>()
            .into_pyobject(py)
            .unwrap()
            .into_any()
            .unbind(),
    }
}

/// 同步计算账户绩效;费率与权重设置由 Rust 从账户行读取(旧 ts 归一为 cs)。
#[pyfunction]
#[pyo3(signature = (store, account_id, range=None, include_backtest=true, verify_source=false, expected_source_version=None))]
fn account_performance(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    range: Option<&str>,
    include_backtest: bool,
    verify_source: bool,
    expected_source_version: Option<i64>,
) -> PyResult<Py<PyAny>> {
    let result = store
        .with_snapshot(|connection| {
            // 回测口径存于 account_settings.backtest JSON 列;无行/缺省回退 0.0,
            // 权重类型一律按 cs 计算(Python 校验器同样把旧 ts 归一为 cs)。
            let fee: f64 = connection
                .query_row(
                    "SELECT COALESCE(json_extract(backtest,'$.backtest_fee_rate'),0.0) \
                     FROM account_settings WHERE account_id=?1",
                    [account_id],
                    |row| row.get(0),
                )
                .unwrap_or(0.0);
            let range: Option<&'static str> = match range {
                None => Ok(None),
                Some("30") => Ok(Some("30")),
                Some("90") => Ok(Some("90")),
                Some("all") => Ok(Some("all")),
                Some(_) => Err("INVALID_REQUEST: range must be 30, 90 or all".to_owned()),
            }?;
            performance::performance(
                connection,
                &PerformanceRequest {
                    account_id,
                    range,
                    include_backtest,
                    // 响应契约包含 bindings 与 calendar,同步计算按带元数据口径返回。
                    include_metadata: true,
                    require_observation_facts: false,
                    expected_source_version: if verify_source {
                        SourceVersionCheck::Version(expected_source_version)
                    } else {
                        SourceVersionCheck::Skip
                    },
                    backtest_fee_rate: fee,
                },
            )
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

/// 读取已发布绩效结果;未就绪或版本过期时抛 NativeError。
#[pyfunction]
#[pyo3(signature = (store, account_id, range, include_backtest, logic_version, engine_version))]
fn performance_result(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    range: &str,
    include_backtest: bool,
    logic_version: &str,
    engine_version: &str,
) -> PyResult<Py<PyAny>> {
    let result = store
        .with_snapshot(|connection| {
            snapshots::performance_result(
                connection,
                &json!({
                    "account_id": account_id,
                    "range": range,
                    "include_backtest": include_backtest,
                    "logic_version": logic_version,
                    "engine_version": engine_version,
                }),
            )
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
fn order_submission_summary(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    start_ms: i64,
    end_ms: i64,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::submissions::summary(connection, account_id, start_ms, end_ms)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn order_submissions(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    day: &str,
    timezone: &str,
    start_ms: i64,
    end_ms: i64,
    limit: i64,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::submissions::page(
                    connection, account_id, day, timezone, start_ms, end_ms, limit,
                )
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn submission_monitor(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    day: &str,
    timezone: &str,
    start_ms: i64,
    end_ms: i64,
    now_ms: i64,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::submissions::monitor(
                    connection, account_id, day, timezone, start_ms, end_ms, now_ms,
                )
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
fn asset_snapshots(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    skip: i64,
    limit: i64,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::assets::page(connection, account_id, skip, limit)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[pyo3(signature = (store, account_id, skip, limit, execution_id=None, record_id=None))]
fn execution_records(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    skip: i64,
    limit: i64,
    execution_id: Option<&str>,
    record_id: Option<i64>,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::executions::page(
                    connection,
                    account_id,
                    skip,
                    limit,
                    execution_id,
                    record_id,
                )
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
fn execution_record_detail(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    record_id: i64,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::executions::detail(connection, account_id, record_id)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[pyo3(signature = (store, account_id, skip, limit, since=None, until=None))]
fn journal(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    skip: i64,
    limit: i64,
    since: Option<&str>,
    until: Option<&str>,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::journal::page(connection, account_id, skip, limit, since, until)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[pyo3(signature = (store, account_id, since, until, symbol=None))]
fn activity_symbols(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    since: &str,
    until: &str,
    symbol: Option<&str>,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::symbols::query(connection, account_id, since, until, symbol)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

/// Classify scalars read in the caller's SQLAlchemy transaction.
#[pyfunction]
#[pyo3(signature = (snapshot_id, compatible, error, active, versions_match))]
fn publication_status(
    snapshot_id: Option<&str>,
    compatible: bool,
    error: Option<&str>,
    active: bool,
    versions_match: bool,
) -> &'static str {
    snapshots::publication_status(snapshot_id, compatible, error, active, versions_match)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Store>()?;
    m.add_function(wrap_pyfunction!(journal, m)?)?;
    m.add_function(wrap_pyfunction!(activity_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(execution_records, m)?)?;
    m.add_function(wrap_pyfunction!(execution_record_detail, m)?)?;
    m.add_function(wrap_pyfunction!(asset_snapshots, m)?)?;
    m.add_function(wrap_pyfunction!(account_performance, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(query_costs, m)?)?;
    m.add_function(wrap_pyfunction!(summaries, m)?)?;
    m.add_function(wrap_pyfunction!(performance_result, m)?)?;
    m.add_function(wrap_pyfunction!(status, m)?)?;
    m.add_function(wrap_pyfunction!(publication_status, m)?)?;
    m.add_function(wrap_pyfunction!(chart, m)?)?;
    m.add_function(wrap_pyfunction!(execution_summary, m)?)?;
    m.add_function(wrap_pyfunction!(execution_detail, m)?)?;
    m.add_function(wrap_pyfunction!(order_submission_summary, m)?)?;
    m.add_function(wrap_pyfunction!(order_submissions, m)?)?;
    m.add_function(wrap_pyfunction!(submission_monitor, m)?)?;
    m.add("NativeError", m.py().get_type::<NativeError>())?;
    Ok(())
}

/// 读取快照状态信封与已发布结果(含 daily_costs/events)。
#[pyfunction]
#[pyo3(signature = ( store, account_id, range, logic_version, engine_version))]
fn snapshot(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    range: &str,
    logic_version: &str,
    engine_version: &str,
) -> PyResult<Py<PyAny>> {
    let result = store
        .with_snapshot(|connection| {
            snapshots::query(
                connection,
                &json!({
                    "account_id": account_id,
                    "range": range,
                    "logic_version": logic_version,
                    "engine_version": engine_version,
                }),
            )
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

/// 分页读取成本投影;快照版本失效时抛 SNAPSHOT_EXPIRED。
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = ( store, account_id, snapshot_id, range, dimension, sort, day, start, end, record_id, symbol, cursor, logic_version, engine_version))]
fn query_costs(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    snapshot_id: &str,
    range: &str,
    dimension: &str,
    sort: &str,
    day: Option<&str>,
    start: Option<f64>,
    end: Option<f64>,
    record_id: Option<i64>,
    symbol: Option<&str>,
    cursor: Option<&str>,
    logic_version: &str,
    engine_version: &str,
) -> PyResult<Py<PyAny>> {
    let result = store
        .with_snapshot(|connection| {
            crate::costs::query_costs(
                connection,
                &json!({
                    "account_id": account_id,
                    "snapshot_id": snapshot_id,
                    "range": range,
                    "dimension": dimension,
                    "sort": sort,
                    "day": day,
                    "start": start,
                    "end": end,
                    "record_id": record_id,
                    "symbol": symbol,
                    "cursor": cursor,
                    "logic_version": logic_version,
                    "engine_version": engine_version,
                }),
            )
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

/// 批量账户卡片摘要:状态与全区间曲线末点。
#[pyfunction]
#[pyo3(signature = ( store, account_ids, logic_version, engine_version))]
fn summaries(
    py: Python<'_>,
    store: &Store,
    account_ids: Vec<i64>,
    logic_version: &str,
    engine_version: &str,
) -> PyResult<Py<PyAny>> {
    let result = store
        .with_snapshot(|connection| {
            snapshots::summaries(
                connection,
                &json!({
                    "account_ids": account_ids,
                    "logic_version": logic_version,
                    "engine_version": engine_version,
                }),
            )
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[pyo3(signature = (store, account_id, logic_version, engine_version))]
fn status(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    logic_version: &str,
    engine_version: &str,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::views::status(connection, account_id, logic_version, engine_version)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[pyo3(signature = (store, account_id, snapshot_id, range))]
fn chart(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    snapshot_id: &str,
    range: &str,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::views::chart(connection, account_id, snapshot_id, range)
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (store, account_id, snapshot_id, range, limit, offset, start, end))]
fn execution_summary(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    snapshot_id: &str,
    range: &str,
    limit: i64,
    offset: i64,
    start: Option<f64>,
    end: Option<f64>,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::views::execution_summary(
                    connection,
                    account_id,
                    snapshot_id,
                    range,
                    limit,
                    offset,
                    start,
                    end,
                )
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}

#[pyfunction]
#[pyo3(signature = (store, account_id, snapshot_id, record_id, limit, offset))]
fn execution_detail(
    py: Python<'_>,
    store: &Store,
    account_id: i64,
    snapshot_id: &str,
    record_id: i64,
    limit: i64,
    offset: i64,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            store.with_snapshot(|connection| {
                crate::views::execution_detail(
                    connection,
                    account_id,
                    snapshot_id,
                    record_id,
                    limit,
                    offset,
                )
            })
        })
        .map_err(raise)?;
    Ok(json_to_py(py, &result))
}
