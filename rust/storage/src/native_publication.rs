//! Native calculation, bounded disk staging and atomic SQLite publication.
//!
//! Only a small receipt crosses the Python boundary; result rows stay in Rust.

pub(crate) mod copy;
mod staging;
pub(crate) mod task;
mod writer;

use std::path::Path;

#[cfg(test)]
use serde_json::json;
use serde_json::Value;

use crate::error::AnalysisError;
use crate::{publication, with_read_snapshot, writer_scope};

use staging::Staging;
use task::Task;

/// Calculate outside the durable writer transaction, then compare-and-publish.
pub fn publish(database: &Path, request: &Value) -> Result<Value, AnalysisError> {
    let task = Task::parse(request)?;
    let connection = writer_scope::open_writer(database)?;
    if let Some(receipt) = writer::already_published(&connection, &task)? {
        return Ok(receipt);
    }
    let staged = Staging::new(&connection)?;
    let result = with_read_snapshot(database, |source| {
        if let Some(reason) = writer::stale_reason(source, &task)? {
            return Ok(Err(reason));
        }
        let complete = publication::write(source, &connection, &task)?;
        Ok(Ok(complete))
    })?;
    let complete = match result {
        Ok(complete) => complete,
        Err(reason) => {
            // The staging transaction never wrote main. End it before obtaining
            // main's writer slot so no old main reader is upgraded into a writer.
            connection
                .execute_batch("ROLLBACK;")
                .map_err(|error| AnalysisError::caused_by("PUBLICATION_STAGE", error))?;
            return writer::skip(&connection, &task, reason);
        }
    };
    let complete = staged.finish(complete, task.account_id, task.source_version)?;
    writer::publish(&connection, &task, &complete)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_store_request_never_opens_database() {
        let error = publish(
            Path::new("/nonexistent.sqlite"),
            &json!({"command":"publish_store"}),
        )
        .unwrap_err();
        assert_eq!(error.code, "INVALID_REQUEST");
    }
}
