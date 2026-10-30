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
    #[serde(serialize_with = "serialize_time")]
    pub(crate) observed_at: i64,
    pub(crate) portfolio_daily_return: Option<f64>,
    pub(crate) portfolio_return: Option<f64>,
    pub(crate) record_id: Option<i64>,
    pub(crate) sizing_fallback: bool,
    pub(crate) target_portfolio_daily_return: Option<f64>,
    pub(crate) target_portfolio_return: Option<f64>,
}

fn serialize_time<S: serde::Serializer>(value: &i64, serializer: S) -> Result<S::Ok, S::Error> {
    let text = crate::time_us::format(*value).map_err(serde::ser::Error::custom)?;
    serializer.serialize_str(&text)
}
