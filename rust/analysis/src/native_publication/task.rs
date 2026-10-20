//! Small, validated control request shared by calculation and commit.

use chrono::{DateTime, NaiveDateTime};
use serde_json::{json, Value};

use crate::error::AnalysisError;

#[derive(Debug)]
pub(super) struct Task {
    pub account_id: i64,
    pub source_version: i64,
    pub snapshot_id: String,
    pub claim_token: String,
    pub logic_version: String,
    pub engine_version: String,
    pub computed_at: String,
    pub weight_type: String,
    pub fee_rate: f64,
    pub trade_channel: String,
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}

fn string(request: &Value, name: &str, limit: usize) -> Result<String, AnalysisError> {
    request[name]
        .as_str()
        .filter(|value| !value.is_empty() && value.len() <= limit)
        .map(str::to_owned)
        .ok_or_else(|| invalid(format!("{name} must be a nonempty bounded string")))
}

fn integer(request: &Value, name: &str) -> Result<i64, AnalysisError> {
    request[name]
        .as_i64()
        .filter(|value| *value > 0)
        .ok_or_else(|| invalid(format!("{name} must be a positive integer")))
}

impl Task {
    pub(super) fn parse(request: &Value) -> Result<Self, AnalysisError> {
        if request["command"].as_str() != Some("publish_store")
            || request["protocol"].as_u64() != Some(1)
        {
            return Err(invalid("publish_store requires protocol 1"));
        }
        let snapshot_id = string(request, "snapshot_id", 128)?;
        let claim_token = string(request, "claim_token", 128)?;
        if snapshot_id != claim_token {
            return Err(invalid("snapshot_id and claim_token must match"));
        }
        let computed_at = string(request, "computed_at", 64)?;
        if DateTime::parse_from_rfc3339(&computed_at).is_err()
            && NaiveDateTime::parse_from_str(&computed_at, "%Y-%m-%dT%H:%M:%S%.f").is_err()
        {
            return Err(invalid("computed_at must be an ISO timestamp"));
        }
        let (weight_type, fee_rate) = settings(&request["settings"])?;
        Ok(Self {
            account_id: integer(request, "account_id")?,
            source_version: integer(request, "expected_source_version")?,
            snapshot_id,
            claim_token,
            logic_version: string(request, "logic_version", 128)?,
            engine_version: string(request, "engine_version", 128)?,
            computed_at,
            weight_type,
            fee_rate,
            trade_channel: string(request, "trade_channel", 128)?,
        })
    }

    pub(super) fn receipt(
        &self,
        published: bool,
        data_until: Value,
        reason: Option<&str>,
    ) -> Value {
        json!({
            "protocol":1,"command":"publish_store","published":published,
            "snapshot_id":self.snapshot_id,"account_id":self.account_id,
            "source_version":self.source_version,"data_until":data_until,"reason":reason,
        })
    }
}

pub(super) fn settings(value: &Value) -> Result<(String, f64), AnalysisError> {
    let weight = value["backtest_weight_type"]
        .as_str()
        .filter(|value| matches!(*value, "cs" | "ts"))
        .ok_or_else(|| invalid("settings.backtest_weight_type must be cs or ts"))?;
    let fee = value["backtest_fee_rate"]
        .as_f64()
        .filter(|value| value.is_finite() && (0.0..1.0).contains(value))
        .ok_or_else(|| invalid("settings.backtest_fee_rate must be finite and in [0,1)"))?;
    Ok((weight.to_owned(), fee))
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(super) fn request() -> Value {
        json!({"command":"publish_store","protocol":1,"account_id":1,
            "expected_source_version":2,"snapshot_id":"token","claim_token":"token",
            "logic_version":"18","engine_version":"equity-ledger-1",
            "computed_at":"2026-10-20T10:00:00+08:00","trade_channel":"gm",
            "settings":{"backtest_weight_type":"ts","backtest_fee_rate":0.0}})
    }

    #[test]
    fn identity_and_types_are_checked_before_database_access() {
        Task::parse(&request()).unwrap();
        let mut value = request();
        value["claim_token"] = json!("another");
        assert_eq!(Task::parse(&value).unwrap_err().code, "INVALID_REQUEST");
        value = request();
        value["expected_source_version"] = json!(true);
        assert!(Task::parse(&value).is_err());
    }
}
