//! Shared numerical output point, independent of stored result layouts.
// Field order matches serde_json::Map's sorted wire order, including nulls.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct Point {
    pub(crate) account_daily_return: Option<f64>,
    pub(crate) account_equity: Option<f64>,
    pub(crate) account_return: Option<f64>,
    pub(crate) date: String,
    pub(crate) difference: Option<f64>,
    pub(crate) execution_id: Option<String>,
    pub(crate) observed_at: String,
    pub(crate) portfolio_daily_return: Option<f64>,
    pub(crate) portfolio_return: Option<f64>,
    pub(crate) record_id: Option<i64>,
    pub(crate) sizing_fallback: bool,
    pub(crate) target_portfolio_daily_return: Option<f64>,
    pub(crate) target_portfolio_return: Option<f64>,
}
