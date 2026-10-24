//! 0041 将摘要与通知引用展开为独立快照；失败由迁移事务整体回滚。

use super::*;
use crate::business_detail;
use crate::migrations::{notification_refs, snapshot_refs};

pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    let mut after = 0;
    loop {
        let found = rows(connection,
            "SELECT e.* FROM executerecord e JOIN execution_extension x ON x.record_id=e.id
             WHERE e.id>?1 AND CASE WHEN json_valid(x.extra) THEN
             json_type(x.extra,'$.server_notification_context.\"$axile_notification_context\"') END IS NOT NULL
             ORDER BY e.id LIMIT 100", [after])?;
        if found.is_empty() {
            break;
        }
        for record in found {
            after = integer(&record, "id")?;
            let extensions = business_detail::load(
                connection,
                "execution_extension",
                "record_id",
                &Value::from(after),
            )
            .map_err(error)?;
            let mut result = serde_json::json!({"extra":null});
            result["extra"] = extensions[0]["extra"].clone();
            notification_refs::hydrate(connection, &record, result.as_object_mut().unwrap())
                .map_err(error)?;
            update(
                connection,
                "execution_extension",
                &Object::from_iter([("extra".into(), result["extra"].clone())]),
                "record_id=?",
                &[Value::from(after)],
            )?;
        }
    }
    let mut after = 0;
    loop {
        let found = rows(
            connection,
            "SELECT id,content FROM execution_artifact WHERE id>?1
             AND lower(artifact_type)='execution_summary' AND CASE WHEN json_valid(content) THEN
             json_extract(content,'$.business_facts_ref.view') END='result' ORDER BY id LIMIT 100",
            [after],
        )?;
        if found.is_empty() {
            break;
        }
        for artifact in found {
            after = integer(&artifact, "id")?;
            let content = json_value(&artifact["content"])?;
            let reference = snapshot_refs::parse_artifact_reference(&content)?
                .ok_or_else(|| error("摘要引用缺失"))?;
            snapshot_refs::detach_execution_summaries(
                connection,
                reference["record_id"].as_i64().unwrap(),
            )?;
        }
    }
    guards::drop_snapshot_references(connection)
}

pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    // 0040 已能读取完整正文，无需重新压缩或改变冻结内容。
    guards::restore_snapshot_references(connection)
}
