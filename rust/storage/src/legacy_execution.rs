//! 冻结的旧执行展示字段，供快照读取与 main 降级恢复共用。

pub(crate) const RAW_FIELDS: &[&str] = &[
    "status",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
];
pub(crate) const SYMBOL_FIELDS: &[&str] = &[
    "status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
];
