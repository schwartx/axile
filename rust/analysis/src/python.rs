//! Python 绑定:每业务函数一个 typed 入口,进程内直调;纯透传查询返回 JSON 字节。

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyFloat, PyInt, PyTuple};
use pyo3::Py;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::error::AnalysisError;
use crate::performance::{self, PerformanceRequest, SourceVersionCheck};
use crate::snapshots;

create_exception!(_core, NativeError, PyException, "原生数据库操作错误");

const EVIDENCE_INPUT_STAGES: [&str; 6] = [
    "persist_execute_record",
    "freeze_execution_input",
    "freeze_execution_after_assets",
    "asset_snapshot_write",
    "target_operation",
    "portfolio_operation",
];

fn raise(error: AnalysisError) -> PyErr {
    NativeError::new_err((error.code, error.error))
}

/// 只读 SQLite 访问点;每次查询一个 DEFERRED 快照,结束即释放。
#[pyclass]
struct Store {
    path: PathBuf,
}

#[pyclass]
struct EvidenceCancellation {
    cancelled: Arc<AtomicBool>,
}

#[pymethods]
impl EvidenceCancellation {
    #[new]
    fn new() -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
    }
}

#[pymethods]
impl Store {
    #[new]
    fn new(py: Python<'_>, path: String) -> PyResult<Self> {
        let path = py
            .detach(|| {
                if path.is_empty() || path == ":memory:" {
                    return Err(AnalysisError::new(
                        "DATABASE_OPEN",
                        "A file-backed SQLite database is required",
                    ));
                }
                let path = Path::new(&path)
                    .canonicalize()
                    .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
                if !path.is_file() {
                    return Err(AnalysisError::new(
                        "DATABASE_OPEN",
                        "database must be a file",
                    ));
                }
                Ok(path)
            })
            .map_err(raise)?;
        Ok(Self { path })
    }
}

impl Store {
    fn value(
        &self,
        py: Python<'_>,
        task: impl FnOnce(&Connection) -> Result<Value, String> + Send,
    ) -> PyResult<Py<PyAny>> {
        let result = py.detach(|| self.with_snapshot(task)).map_err(raise)?;
        json_to_py(py, &result)
    }

    fn json_bytes(
        &self,
        py: Python<'_>,
        task: impl FnOnce(&Connection) -> Result<Value, String> + Send,
    ) -> PyResult<Py<PyBytes>> {
        let result = py
            .detach(|| self.with_snapshot(task).and_then(encode_json))
            .map_err(raise)?;
        Ok(PyBytes::new(py, &result).unbind())
    }

    fn with_snapshot<T>(
        &self,
        task: impl FnOnce(&Connection) -> Result<T, String>,
    ) -> Result<T, AnalysisError> {
        crate::with_read_snapshot_errors(
            &self.path,
            |connection| task(connection).map_err(AnalysisError::from),
            ("DATABASE_BEGIN", "DATABASE_CLOSE"),
        )
    }
}

fn encode_json(value: Value) -> Result<Vec<u8>, AnalysisError> {
    serde_json::to_vec(&value)
        .map_err(|error| AnalysisError::new("ANALYSIS_ERROR", error.to_string()))
}

fn json_to_py(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
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
            } else if let Some(inner) = number.as_u64() {
                inner.into_pyobject(py).unwrap().into_any().unbind()
            } else {
                let literal = number.to_string();
                let number_type = if literal
                    .trim_start_matches('-')
                    .bytes()
                    .all(|byte| byte.is_ascii_digit())
                {
                    py.get_type::<PyInt>()
                } else {
                    py.get_type::<PyFloat>()
                };
                number_type.call1((literal,))?.unbind()
            }
        }
        Value::String(inner) => inner.into_pyobject(py).unwrap().into_any().unbind(),
        Value::Array(items) => items
            .iter()
            .map(|item| json_to_py(py, item))
            .collect::<PyResult<Vec<Py<PyAny>>>>()?
            .into_pyobject(py)?
            .into_any()
            .unbind(),
        Value::Object(map) => {
            let result = PyDict::new(py);
            for (key, item) in map {
                result.set_item(key, json_to_py(py, item)?)?;
            }
            result.into_any().unbind()
        }
    })
}

fn business_request(stage: &str, request_json: &str) -> Result<Value, AnalysisError> {
    match serde_json::from_str(request_json) {
        Ok(value) => Ok(value),
        Err(_) if EVIDENCE_INPUT_STAGES.contains(&stage) => {
            crate::migrations::parse_legacy_json(request_json)
                .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.error))
        }
        Err(error) => Err(AnalysisError::new("INVALID_REQUEST", error.to_string())),
    }
}

fn business_operation(
    py: Python<'_>,
    path: &str,
    stage: &str,
    request_json: &str,
    operation: fn(&str, &Value) -> Result<Value, AnalysisError>,
) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| {
            let request = business_request(stage, request_json)?;
            operation(path, &request)
        })
        .map_err(raise)?;
    json_to_py(py, &result)
}

fn business_json_operation(
    py: Python<'_>,
    path: &str,
    stage: &str,
    request_json: &str,
    operation: fn(&str, &Value) -> Result<Value, AnalysisError>,
) -> PyResult<Py<PyBytes>> {
    business_bytes_operation(py, path, stage, request_json, |path, request| {
        operation(path, request).and_then(encode_json)
    })
}

fn business_bytes_operation(
    py: Python<'_>,
    path: &str,
    stage: &str,
    request_json: &str,
    operation: impl FnOnce(&str, &Value) -> Result<Vec<u8>, AnalysisError> + Send,
) -> PyResult<Py<PyBytes>> {
    let result = py
        .detach(|| {
            let request = business_request(stage, request_json)?;
            operation(path, &request)
        })
        .map_err(raise)?;
    Ok(PyBytes::new(py, &result).unbind())
}

macro_rules! business_operations {
    ($register:ident, $execute:ident, $output:ty; $($name:ident => $operation:path),* $(,)?) => {
        $(
            #[pyfunction]
            fn $name(py: Python<'_>, path: String, request_json: &str) -> PyResult<Py<$output>> {
                $execute(py, &path, stringify!($name), request_json, $operation)
            }
        )*

        fn $register(module: &Bound<'_, PyModule>) -> PyResult<()> {
            $(module.add_function(wrap_pyfunction!($name, module)?)?;)*
            Ok(())
        }
    };
}

business_operations! { register_business_operations, business_operation, PyAny;
    freeze_execution_input => crate::business_facts_ops::freeze_execution_input,
    freeze_execution_after_assets => crate::business_facts_ops::freeze_execution_after_assets,
    migration_current => crate::migrations::migration_current,
    migration_check => crate::migrations::migration_check,
    migration_upgrade => crate::migrations::migration_upgrade,
    migration_downgrade => crate::migrations::migration_downgrade,
    database_probe => crate::database_probe::probe,
    account_get => crate::accounts::account_get,
    account_list => crate::accounts::account_list,
    account_create => crate::accounts::account_create,
    account_create_replay => crate::accounts::account_create_replay,
    account_copy => crate::accounts::account_copy,
    account_update => crate::accounts::account_update,
    account_delete => crate::accounts::account_delete,
    account_dashboard_context => crate::accounts::account_dashboard_context,
    account_next_run_context => crate::accounts::account_next_run_context,
    account_portfolio_records => crate::accounts::account_portfolio_records,
    account_latest_portfolio => crate::accounts::account_latest_portfolio,
    portfolio_latest_account => crate::accounts::portfolio_latest_account,
    account_update_backtest => crate::accounts::account_update_backtest,
    account_stop_for_clear => crate::accounts::account_stop_for_clear,
    time_check_start => crate::time_check::start,
    time_check_get => crate::time_check::get,
    time_check_persist => crate::time_check::persist,
    time_check_save => crate::time_check::save,
    time_check_claim => crate::time_check::claim,
    connection_begin => crate::connection_state::connection_begin,
    connection_observe => crate::connection_state::connection_observe,
    connection_end => crate::connection_state::connection_end,
    connection_recover => crate::connection_state::connection_recover,
    connection_accounts => crate::connection_state::connection_accounts,
    notifications_claim => crate::notifications::claim,
    notifications_finish => crate::notifications::finish,
    notifications_recover => crate::notifications::recover,
    notifications_record_account => crate::notifications::record_account,
    notifications_record_system => crate::notifications::record_system,
    notifications_get_system => crate::notifications::get_system,
    notifications_preview => crate::notifications::preview,
    intent_get => crate::intents::intent_get,
    intent_active => crate::intents::intent_active,
    intent_list => crate::intents::intent_list,
    intent_recovery => crate::intents::intent_recovery,
    intent_insert => crate::intents::intent_insert,
    intent_coalesce => crate::intents::intent_coalesce,
    intent_cas => crate::intents::intent_cas,
    intent_finish => crate::intents::intent_finish,
    supplement_create_group => crate::supplements::create_group,
    supplement_step => crate::supplements::step,
    supplement_execution_steps => crate::supplements::execution_steps,
    supplement_execution_requests => crate::supplements::execution_requests,
    supplement_validate_requests => crate::supplements::validate_requests,
    supplement_cancel_groups => crate::supplements::cancel_groups,
    supplement_expire_group => crate::supplements::expire_group,
    supplement_recover => crate::supplements::recover_supplements,
    runtime_sync_enqueue => crate::runtime_sync::enqueue,
    runtime_sync_begin_attempt => crate::runtime_sync::begin_attempt,
    runtime_sync_finish_attempt => crate::runtime_sync::finish_attempt,
    runtime_sync_scan => crate::runtime_sync::scan,
    runtime_sync_scheduler_account => crate::runtime_sync::scheduler_account,
    runtime_sync_record_schedule_skip => crate::runtime_sync::record_schedule_skip,
    portfolio_operation => crate::portfolio_ops::portfolio_operation,
    target_operation => crate::target_ops::target_operation,
    persist_execute_record => crate::execution_ops::persist_execute_record,
    delete_execution_history => crate::execution_ops::delete_execution_history,
    recent_execution_records => crate::execution_ops::recent_execution_records,
    read_execution_input => crate::execution_ops::read_execution_input,
    read_execution_result => crate::execution_ops::read_execution_result,
    read_execution_detail => crate::execution_ops::read_execution_detail,
    asset_snapshot_write => crate::asset_ops::asset_snapshot_write,
    asset_snapshot_latest => crate::asset_ops::asset_snapshot_latest,
    asset_snapshots_read => crate::asset_ops::asset_snapshots_read,
    asset_input_observations => crate::asset_ops::asset_input_observations,
    append_execution_event => crate::audit_ops::append_execution_event,
    append_execution_artifact => crate::audit_ops::append_execution_artifact,
    append_execution_audit_batch => crate::audit_ops::append_execution_audit_batch,
    list_execution_events => crate::audit_ops::list_execution_events,
    list_execution_artifacts => crate::audit_ops::list_execution_artifacts,
    resolve_execution_artifact => crate::audit_ops::resolve_execution_artifact,
    analysis_context => crate::analysis_ops::context,
    analysis_calendar_save => crate::analysis_ops::save_calendar,
    analysis_publication_committed => crate::analysis_ops::publication_committed,
    execution_read => crate::execution_reads::execution_read,
    control_monitor_inputs => crate::control_monitor::control_monitor_inputs,
    control_monitor_refresh => crate::control_monitor::control_monitor_refresh,
    control_monitor_recover => crate::control_monitor::control_monitor_recover,
    control_quota_warnings => crate::control_monitor::control_quota_warnings,
    control_quota_pending => crate::control_monitor::control_quota_pending,
    control_load_baseline => crate::control_store::load_baseline,
    control_register_session => crate::control_store::register_session,
    control_flush_records => crate::control_store::flush_records,
    control_update_event_outcomes => crate::control_store::update_event_outcomes,
    save_order_submission => crate::submissions::save_order_submission,
    save_submission_monitor => crate::submissions::save_submission_monitor,
    order_execution_links => crate::submissions::order_execution_links,
    maintenance_performance_dedupe => crate::maintenance_ops::performance_dedupe,
    maintenance_dummy_repair => crate::maintenance_ops::dummy_repair,
    maintenance_replay_report => crate::maintenance_ops::replay_report,
    maintenance_replay_observe => crate::maintenance_ops::replay_observe,
}

business_operations! { register_bytes_operations, business_bytes_operation, PyBytes;
    account_list_json => crate::accounts::account_list_json,
    account_info_json => crate::accounts::account_info_json,
    account_connection_json => crate::accounts::account_connection_json,
    account_portfolio_records_json => crate::accounts::account_portfolio_records_json,
}

#[pyfunction]
#[pyo3(signature = (path, request_json, control=None))]
fn collect_evidence(
    py: Python<'_>,
    path: String,
    request_json: &str,
    control: Option<&EvidenceCancellation>,
) -> PyResult<Py<PyAny>> {
    let cancelled = control
        .map(|control| Arc::clone(&control.cancelled))
        .unwrap_or_else(|| Arc::new(AtomicBool::new(false)));
    let result = py
        .detach(|| {
            let request = business_request("collect_evidence", request_json)?;
            crate::evidence_ops::collect_evidence_with_control(&path, &request, &cancelled)
        })
        .map_err(raise)?;
    json_to_py(py, &result)
}

business_operations! { register_json_operations, business_json_operation, PyBytes;
    portfolio_query_json => crate::portfolio_ops::portfolio_query,
    control_audit_summary_json => crate::control_audit::control_audit_summary,
    control_audit_executions_json => crate::control_audit::control_audit_executions,
    control_audit_events_json => crate::control_audit::control_audit_events,
}

macro_rules! store_operations {
    ($register:ident, $execute:ident, $output:ty; $(
        $(#[$metadata:meta])*
        $name:ident($($argument:ident: $kind:ty),* $(,)?) => |$connection:ident| $task:expr;
    )*) => {
        $(
            #[pyfunction]
            $(#[$metadata])*
            fn $name(py: Python<'_>, store: &Store, $($argument: $kind),*) -> PyResult<Py<$output>> {
                store.$execute(py, |$connection| $task)
            }
        )*
        fn $register(module: &Bound<'_, PyModule>) -> PyResult<()> {
            $(module.add_function(wrap_pyfunction!($name, module)?)?;)*
            Ok(())
        }
    };
}

macro_rules! detached_operations {
    ($register:ident; $(
        $name:ident($($argument:ident: $kind:ty),* $(,)?) -> $output:ty => $operation:path;
    )*) => {
        $(
            #[pyfunction]
            fn $name(py: Python<'_>, $($argument: $kind),*) -> PyResult<$output> {
                py.detach(|| $operation($($argument),*)).map_err(raise)
            }
        )*
        fn $register(module: &Bound<'_, PyModule>) -> PyResult<()> {
            $(module.add_function(wrap_pyfunction!($name, module)?)?;)*
            Ok(())
        }
    };
}

store_operations! { register_store_values, value, PyAny;
    /// 同步计算账户绩效;费率与权重设置由 Rust 从账户行读取(旧 ts 归一为 cs)。
    #[pyo3(signature = (store, account_id, range=None, include_backtest=true, verify_source=false, expected_source_version=None))]
    account_performance(
        account_id: i64,
        range: Option<&str>,
        include_backtest: bool,
        verify_source: bool,
        expected_source_version: Option<i64>,
    ) => |connection| {
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
    };

    /// 读取已发布绩效结果;未就绪或版本过期时抛 NativeError。
    #[pyo3(signature = (store, account_id, range, include_backtest, logic_version, engine_version))]
    performance_result(
        account_id: i64,
        range: &str,
        include_backtest: bool,
        logic_version: &str,
        engine_version: &str,
    ) => |connection| {
        let request = json!({
            "account_id": account_id,
            "range": range,
            "include_backtest": include_backtest,
            "logic_version": logic_version,
            "engine_version": engine_version,
        });
        snapshots::performance_result(connection, &request)
    };

    order_submission_summary(
        account_id: i64,
        start_ms: i64,
        end_ms: i64,
    ) => |connection| crate::submissions::summary(connection, account_id, start_ms, end_ms);

    #[allow(clippy::too_many_arguments)]
    submission_monitor(
        account_id: i64,
        day: &str,
        timezone: &str,
        start_ms: i64,
        end_ms: i64,
        now_ms: i64,
    ) => |connection| crate::submissions::monitor(
        connection, account_id, day, timezone, start_ms, end_ms, now_ms,
    );

    /// Internal business evidence stays as objects, not an HTTP JSON round trip.
    execution_business_detail(
        account_id: i64,
        record_id: i64,
    ) => |connection| crate::business_detail::read(connection, account_id, record_id);

    /// 读取快照状态信封与已发布结果(含 daily_costs/events)。
    #[pyo3(signature = ( store, account_id, range, logic_version, engine_version))]
    snapshot(
        account_id: i64,
        range: &str,
        logic_version: &str,
        engine_version: &str,
    ) => |connection| {
        let request = json!({
            "account_id": account_id,
            "range": range,
            "logic_version": logic_version,
            "engine_version": engine_version,
        });
        snapshots::query(connection, &request)
    };

    /// 同一只读快照中的卡片输入；插件和实时状态由 Python 收尾。
    #[pyo3(signature = ( store, account_ids, logic_version, engine_version))]
    dashboard_inputs(
        account_ids: Vec<i64>,
        logic_version: &str,
        engine_version: &str,
    ) => |connection| crate::dashboard::dashboard_inputs(
        connection,
        &account_ids,
        logic_version,
        engine_version,
    );

    #[pyo3(signature = (store, account_ids, logic_version, engine_version))]
    summaries(
        account_ids: Vec<i64>,
        logic_version: &str,
        engine_version: &str,
    ) => |connection| snapshots::summaries_for_accounts(
        connection,
        &account_ids,
        logic_version,
        engine_version,
    );
}

store_operations! { register_store_bytes, json_bytes, PyBytes;
    #[allow(clippy::too_many_arguments)]
    order_submissions(
        account_id: i64,
        day: &str,
        timezone: &str,
        start_ms: i64,
        end_ms: i64,
        limit: i64,
    ) => |connection| crate::submissions::page(
        connection, account_id, day, timezone, start_ms, end_ms, limit,
    );

    #[allow(clippy::too_many_arguments)]
    submission_monitor_json(
        account_id: i64,
        day: &str,
        timezone: &str,
        start_ms: i64,
        end_ms: i64,
        now_ms: i64,
    ) => |connection| crate::submissions::monitor(
        connection, account_id, day, timezone, start_ms, end_ms, now_ms,
    );

    asset_snapshots(
        account_id: i64,
        skip: i64,
        limit: i64,
    ) => |connection| crate::assets::page(connection, account_id, skip, limit);

    #[pyo3(signature = (store, account_id, skip, limit, execution_id=None, record_id=None))]
    execution_records(
        account_id: i64,
        skip: i64,
        limit: i64,
        execution_id: Option<&str>,
        record_id: Option<i64>,
    ) => |connection| crate::executions::page(connection, account_id, skip, limit, execution_id, record_id);

    execution_record_detail(
        account_id: i64,
        record_id: i64,
    ) => |connection| crate::executions::detail(connection, account_id, record_id);

    #[pyo3(signature = (store, account_id, skip, limit, since=None, until=None))]
    journal(
        account_id: i64,
        skip: i64,
        limit: i64,
        since: Option<&str>,
        until: Option<&str>,
    ) => |connection| crate::journal::page(connection, account_id, skip, limit, since, until);

    #[pyo3(signature = (store, account_id, since, until, symbol=None))]
    activity_symbols(
        account_id: i64,
        since: &str,
        until: &str,
        symbol: Option<&str>,
    ) => |connection| crate::symbols::query(connection, account_id, since, until, symbol);

    /// 分页读取成本投影;快照版本失效时抛 SNAPSHOT_EXPIRED。
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = ( store, account_id, snapshot_id, range, dimension, sort, day, start, end, record_id, symbol, symbol_search, side, limit, cursor_scope, cursor, logic_version, engine_version))]
    query_costs(
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
        symbol_search: Option<&str>,
        side: Option<&str>,
        limit: i64,
        cursor_scope: &str,
        cursor: Option<&str>,
        logic_version: &str,
        engine_version: &str,
    ) => |connection| crate::costs::query_costs(
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
            "symbol_search": symbol_search,
            "side": side,
            "limit": limit,
            "cursor_scope": cursor_scope,
            "cursor": cursor,
            "logic_version": logic_version,
            "engine_version": engine_version,
        }),
    );

    #[pyo3(signature = (store, account_id, logic_version, engine_version))]
    status(
        account_id: i64,
        logic_version: &str,
        engine_version: &str,
    ) => |connection| crate::views::status(connection, account_id, logic_version, engine_version);

    #[pyo3(signature = (store, account_id, snapshot_id, range))]
    chart(
        account_id: i64,
        snapshot_id: &str,
        range: &str,
    ) => |connection| crate::views::chart(connection, account_id, snapshot_id, range);

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (store, account_id, snapshot_id, range, limit, offset, start, end))]
    execution_summary(
        account_id: i64,
        snapshot_id: &str,
        range: &str,
        limit: i64,
        offset: i64,
        start: Option<f64>,
        end: Option<f64>,
    ) => |connection| crate::views::execution_summary(
        connection,
        account_id,
        snapshot_id,
        range,
        limit,
        offset,
        start,
        end,
    );

    #[pyo3(signature = (store, account_id, snapshot_id, record_id, limit, offset))]
    execution_detail(
        account_id: i64,
        snapshot_id: &str,
        record_id: i64,
        limit: i64,
        offset: i64,
    ) => |connection| crate::views::execution_detail(
        connection,
        account_id,
        snapshot_id,
        record_id,
        limit,
        offset,
    );
}

detached_operations! { register_queue_operations;
    queue_enqueue(
        path: &str,
        account_id: i64,
        force: bool,
    ) -> () => crate::analysis_queue::enqueue;

    queue_status(
        path: &str,
        account_id: i64,
        logic: &str,
        engine: &str,
    ) -> String => crate::analysis_queue::status;

    queue_recover(
        path: &str,
        logic: &str,
        engine: &str,
    ) -> () => crate::analysis_queue::recover;

    queue_release(
        path: &str,
        account_id: i64,
        version: i64,
        token: &str,
    ) -> () => crate::analysis_queue::release;

    queue_fail(
        path: &str,
        account_id: i64,
        version: i64,
        token: &str,
        now: f64,
    ) -> () => crate::analysis_queue::fail;
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

/// Calculate and atomically publish using a separate writer connection.
#[pyfunction]
fn publish_store(py: Python<'_>, path: &str, request_json: &str) -> PyResult<Py<PyAny>> {
    business_operation(py, path, "publish_store", request_json, |path, request| {
        crate::native_publication::publish(std::path::Path::new(path), request)
    })
}

#[pyfunction]
fn close_database(py: Python<'_>, path: String) -> PyResult<()> {
    py.detach(|| crate::storage_connection::migration_guard(Path::new(&path)).map(drop))
        .map_err(raise)
}

#[pyfunction]
fn queue_claim(py: Python<'_>, path: &str, now: f64, token: &str) -> PyResult<Py<PyAny>> {
    let result = py
        .detach(|| crate::analysis_queue::claim(path, now, token))
        .map_err(raise)?;
    json_to_py(py, &result.unwrap_or(Value::Null))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("ANALYSIS_FACT_SCHEMA", 40)?;
    m.add("PUBLIC_HEAD", crate::migrations::PUBLIC_HEAD)?;
    m.add(
        "EVIDENCE_INPUT_STAGES",
        PyTuple::new(m.py(), EVIDENCE_INPUT_STAGES)?,
    )?;
    m.add_class::<Store>()?;
    m.add_class::<EvidenceCancellation>()?;
    m.add_function(wrap_pyfunction!(close_database, m)?)?;
    m.add_function(wrap_pyfunction!(queue_claim, m)?)?;
    m.add_function(wrap_pyfunction!(publish_store, m)?)?;
    register_queue_operations(m)?;
    register_store_values(m)?;
    register_store_bytes(m)?;
    register_business_operations(m)?;
    register_json_operations(m)?;
    register_bytes_operations(m)?;
    m.add_function(wrap_pyfunction!(collect_evidence, m)?)?;
    m.add_function(wrap_pyfunction!(publication_status, m)?)?;
    m.add("NativeError", m.py().get_type::<NativeError>())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonfinite_input_keeps_number_object_and_string_identity_only_for_fact_stages() {
        let input = r#"{"values":[NaN,Infinity,-Infinity,{"invalid_number":"nan"},"NaN"]}"#;
        for stage in EVIDENCE_INPUT_STAGES {
            let request = business_request(stage, input).unwrap();
            assert!(request["values"][0].is_number());
            assert_eq!(request["values"][0].to_string(), "NaN");
            assert_eq!(request["values"][1].to_string(), "Infinity");
            assert_eq!(request["values"][2].to_string(), "-Infinity");
            assert!(request["values"][3].is_object());
            assert_eq!(request["values"][3]["invalid_number"], "nan");
            assert_eq!(request["values"][4], "NaN");
            assert!(business_request(stage, r#"{"value":NaNx}"#).is_err());
        }
        for stage in [
            "account_create",
            "collect_evidence",
            "notifications_record_system",
        ] {
            assert_eq!(
                business_request(stage, input).unwrap_err().code,
                "INVALID_REQUEST"
            );
            let genuine = business_request(stage, r#"{"value":{"invalid_number":"nan"}}"#).unwrap();
            assert!(genuine["value"].is_object());
        }
    }
}
