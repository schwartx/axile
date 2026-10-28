//! 0038 移除历史隐藏，保留全部执行事实和 worker 结算边界。
use super::sql;
use crate::error::AnalysisError;
use rusqlite::Connection;

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection.execute_batch(
        "DROP TRIGGER execution_history_visibility;
        ALTER TABLE executerecord DROP COLUMN history_visible;
        CREATE TRIGGER execution_result_staged AFTER UPDATE OF result_staged ON executerecord
        WHEN OLD.result_staged!=NEW.result_staged BEGIN
        INSERT INTO account_analysis(account_id,source_version) VALUES(NEW.account_id,1)
        ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL; END;
        UPDATE account_analysis SET source_version=source_version+1,requested=1,failures=0,retry_at=0,error=NULL;",
    ).map_err(sql)
}

pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection.execute_batch(
        "DROP TRIGGER execution_result_staged;
        ALTER TABLE executerecord ADD COLUMN history_visible INTEGER NOT NULL DEFAULT 1;
        CREATE TRIGGER execution_history_visibility AFTER UPDATE OF history_visible,result_staged ON executerecord
        WHEN OLD.history_visible!=NEW.history_visible OR OLD.result_staged!=NEW.result_staged BEGIN
        INSERT INTO account_analysis(account_id,source_version) VALUES(NEW.account_id,1)
        ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL; END;",
    ).map_err(sql)
}
