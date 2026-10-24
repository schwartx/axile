use std::fmt;

use serde::Serialize;

/// Public error envelope shared with the Python process bridge.
#[derive(Debug, Serialize)]
pub struct AnalysisError {
    pub code: String,
    pub error: String,
    #[serde(skip)]
    source: Option<Box<dyn std::error::Error + Send + Sync>>,
}

impl AnalysisError {
    pub fn new(code: impl Into<String>, error: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            error: error.into(),
            source: None,
        }
    }

    pub fn caused_by(
        code: impl Into<String>,
        source: impl std::error::Error + Send + Sync + 'static,
    ) -> Self {
        Self {
            code: code.into(),
            error: source.to_string(),
            source: Some(Box::new(source)),
        }
    }
}

// Compatibility boundary for readers/codecs that still return legacy messages.
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
        Self::new(code, error)
    }
}

impl fmt::Display for AnalysisError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.error)
    }
}

impl std::error::Error for AnalysisError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source.as_deref().map(|source| source as _)
    }
}

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

    #[test]
    fn native_failure_preserves_source_and_public_envelope() {
        use std::error::Error;

        let source = std::io::Error::new(std::io::ErrorKind::PermissionDenied, "write denied");
        let failure = AnalysisError::caused_by("PUBLICATION_WRITE", source);
        assert_eq!(failure.code, "PUBLICATION_WRITE");
        assert_eq!(failure.to_string(), "write denied");
        assert_eq!(
            failure
                .source()
                .unwrap()
                .downcast_ref::<std::io::Error>()
                .unwrap()
                .kind(),
            std::io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            serde_json::to_value(&failure).unwrap(),
            serde_json::json!({"code":"PUBLICATION_WRITE","error":"write denied"})
        );
        assert!(AnalysisError::new("BUSINESS_FACTS", "invalid input")
            .source()
            .is_none());
    }
}
