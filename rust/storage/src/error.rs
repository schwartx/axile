use std::fmt;

use serde::Serialize;

/// Public error envelope shared with the Python process bridge.
#[derive(Debug, Serialize)]
pub struct AnalysisError {
    pub code: String,
    pub error: String,
}

impl AnalysisError {
    pub fn new(code: impl Into<String>, error: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            error: error.into(),
        }
    }
}

impl From<String> for AnalysisError {
    fn from(error: String) -> Self {
        let code = error
            .split_once(':')
            .map(|(prefix, _)| prefix)
            .filter(|prefix| {
                !prefix.is_empty()
                    && prefix.len() <= 64
                    && prefix.bytes().all(|c| c.is_ascii_uppercase() || c == b'_')
            })
            .unwrap_or("ANALYSIS_ERROR")
            .to_owned();
        Self { code, error }
    }
}

impl fmt::Display for AnalysisError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.error)
    }
}

impl std::error::Error for AnalysisError {}

#[cfg(test)]
mod tests {
    use super::AnalysisError;

    #[test]
    fn preserves_known_error_code_without_rewriting_message() {
        let error = AnalysisError::from("SNAPSHOT_EXPIRED: stale snapshot".to_owned());
        assert_eq!(error.code, "SNAPSHOT_EXPIRED");
        assert_eq!(error.error, "SNAPSHOT_EXPIRED: stale snapshot");
    }

    #[test]
    fn does_not_treat_database_error_prose_as_a_public_code() {
        let error = AnalysisError::from("no such table: execution".to_owned());
        assert_eq!(error.code, "ANALYSIS_ERROR");
    }
}
