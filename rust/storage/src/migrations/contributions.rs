//! Add rebuildable contribution observations without rewriting business facts.
use super::sql;
use crate::error::AnalysisError;
use rusqlite::Connection;

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection
        .execute_batch(include_str!("contributions.sql"))
        .map_err(sql)
}
pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection
        .execute_batch("DROP TABLE performance_contribution;")
        .map_err(sql)
}
