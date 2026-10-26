//! 账户控制配置的类型、校验与策略合并；存储保留调用方的稀疏 JSON。

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{LazyLock, RwLock};

use schemars::JsonSchema;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Serialize)]
pub struct ConfigError {
    pub code: &'static str,
    pub path: Vec<String>,
    pub message: String,
}

impl ConfigError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            path: Vec::new(),
            message: message.into(),
        }
    }

    pub fn at(mut self, key: impl Into<String>) -> Self {
        self.path.insert(0, key.into());
        self
    }
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ConfigError {}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
#[cfg_attr(feature = "python", pyo3::pyclass(eq, eq_int, frozen, from_py_object))]
pub enum AccountControlTriggerBehavior {
    #[default]
    WAIT,
    BLOCK,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlRuleOverride {
    #[serde(default)]
    #[schemars(range(min = 0))]
    pub limit: Option<i64>,
    #[serde(default)]
    pub on_trigger: Option<AccountControlTriggerBehavior>,
    #[serde(default)]
    pub unlimited: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlRule {
    #[schemars(range(min = 0))]
    pub limit: i64,
    pub on_trigger: AccountControlTriggerBehavior,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlScopeOverride {
    #[serde(default)]
    pub per_minute: Option<AccountControlRuleOverride>,
    #[serde(default)]
    pub per_day: Option<AccountControlRuleOverride>,
    #[serde(default)]
    pub min_interval_ms: Option<AccountControlRuleOverride>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlScopePolicy {
    #[serde(default)]
    pub per_minute: Option<AccountControlRule>,
    #[serde(default)]
    pub per_day: Option<AccountControlRule>,
    #[serde(default)]
    pub min_interval_ms: Option<AccountControlRule>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlOperationOverride {
    #[serde(default)]
    pub priority: Option<i64>,
    #[serde(default)]
    #[schemars(range(min = 1, max = 99))]
    pub daily_warning_percent: Option<i64>,
    #[serde(default)]
    pub account: Option<AccountControlScopeOverride>,
    #[serde(default)]
    pub symbol: Option<AccountControlScopeOverride>,
}

fn default_priority() -> i64 {
    100
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlOperationPolicy {
    #[serde(default = "default_priority")]
    pub priority: i64,
    #[serde(default)]
    #[schemars(range(min = 1, max = 99))]
    pub daily_warning_percent: Option<i64>,
    #[serde(default)]
    pub account: AccountControlScopePolicy,
    #[serde(default)]
    pub symbol: Option<AccountControlScopePolicy>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlOverride {
    #[serde(default)]
    pub timezone: Option<String>,
    #[serde(default)]
    pub operations: BTreeMap<String, AccountControlOperationOverride>,
    #[serde(default)]
    pub groups: BTreeMap<String, AccountControlScopeOverride>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct AccountControlPolicy {
    pub timezone: String,
    #[serde(default)]
    pub operations: BTreeMap<String, AccountControlOperationPolicy>,
    #[serde(default)]
    pub groups: BTreeMap<String, AccountControlScopePolicy>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(feature = "python", pyo3::pyclass(get_all, frozen, from_py_object))]
pub struct EffectiveAccountControlPolicy {
    pub preset_key: String,
    pub timezone: String,
    #[serde(default)]
    pub operations: BTreeMap<String, AccountControlOperationPolicy>,
    #[serde(default)]
    pub groups: BTreeMap<String, AccountControlScopePolicy>,
    #[serde(default)]
    pub operation_groups: BTreeMap<String, BTreeSet<String>>,
}

// JSON 配置只接受对象，禁止 Serde 的结构体位置数组形式。
macro_rules! object_serde {
    ($($name:ident),+ $(,)?) => {$ (
        impl Serialize for $name {
            fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                Self::serialize(self, serializer)
            }
        }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                struct ObjectVisitor;
                impl<'de> serde::de::Visitor<'de> for ObjectVisitor {
                    type Value = $name;
                    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                        f.write_str("a configuration object")
                    }
                    fn visit_map<A: serde::de::MapAccess<'de>>(self, map: A) -> Result<$name, A::Error> {
                        $name::deserialize(serde::de::value::MapAccessDeserializer::new(map))
                    }
                }
                deserializer.deserialize_map(ObjectVisitor)
            }
        }
    )+};
}

object_serde!(
    AccountControlRuleOverride,
    AccountControlRule,
    AccountControlScopeOverride,
    AccountControlScopePolicy,
    AccountControlOperationOverride,
    AccountControlOperationPolicy,
    AccountControlOverride,
    AccountControlPolicy,
    EffectiveAccountControlPolicy,
);

#[derive(Default)]
pub struct Registry {
    operations: BTreeSet<String>,
    groups: BTreeSet<String>,
}

static REGISTRY: LazyLock<RwLock<Registry>> = LazyLock::new(|| RwLock::new(Registry::default()));

pub fn set_registry(operations: Vec<String>, groups: Vec<String>) {
    *REGISTRY.write().unwrap() = Registry {
        operations: operations.into_iter().collect(),
        groups: groups.into_iter().collect(),
    };
}

pub trait Validate {
    fn validate(&self) -> Result<(), ConfigError>;
}

pub fn parse<T: DeserializeOwned + Validate>(value: Value) -> Result<T, ConfigError> {
    let result: T = decode(value)?;
    result.validate()?;
    Ok(result)
}

pub fn decode<T: DeserializeOwned>(value: Value) -> Result<T, ConfigError> {
    serde_path_to_error::deserialize(value).map_err(|error| {
        let mut failure = ConfigError::new(
            "CONTROL_CONFIG_INVALID_STRUCTURE",
            error.inner().to_string(),
        );
        failure.path = error
            .path()
            .iter()
            .filter_map(|segment| match segment {
                serde_path_to_error::Segment::Map { key } => Some(key.clone()),
                serde_path_to_error::Segment::Seq { index } => Some(index.to_string()),
                serde_path_to_error::Segment::Enum { variant } => Some(variant.clone()),
                serde_path_to_error::Segment::Unknown => None,
            })
            .collect();
        failure
    })
}

fn timezone(value: &str) -> Result<(), ConfigError> {
    value.parse::<chrono_tz::Tz>().map(|_| ()).map_err(|_| {
        ConfigError::new(
            "CONTROL_CONFIG_INVALID_TIMEZONE",
            format!("无效的 timezone: {value}"),
        )
        .at("timezone")
    })
}

fn limit(value: i64) -> Result<(), ConfigError> {
    if value < 0 {
        return Err(ConfigError::new(
            "CONTROL_CONFIG_OUT_OF_RANGE",
            "limit must be greater than or equal to 0",
        )
        .at("limit"));
    }
    Ok(())
}

fn warning(value: Option<i64>) -> Result<(), ConfigError> {
    if value.is_some_and(|value| !(1..=99).contains(&value)) {
        return Err(ConfigError::new(
            "CONTROL_CONFIG_OUT_OF_RANGE",
            "daily_warning_percent 必须在 1..99 之间",
        )
        .at("daily_warning_percent"));
    }
    Ok(())
}

impl Validate for AccountControlRuleOverride {
    fn validate(&self) -> Result<(), ConfigError> {
        if let Some(value) = self.limit {
            limit(value)?;
        }
        if self.unlimited && (self.limit.is_some() || self.on_trigger.is_some()) {
            return Err(ConfigError::new(
                "CONTROL_CONFIG_CONFLICT",
                "unlimited 与 limit/on_trigger 互斥，不能同时设置",
            )
            .at("unlimited"));
        }
        Ok(())
    }
}

impl Validate for AccountControlRule {
    fn validate(&self) -> Result<(), ConfigError> {
        limit(self.limit)
    }
}

impl Validate for AccountControlScopeOverride {
    fn validate(&self) -> Result<(), ConfigError> {
        for (key, rule) in [
            ("per_minute", &self.per_minute),
            ("per_day", &self.per_day),
            ("min_interval_ms", &self.min_interval_ms),
        ] {
            if let Some(rule) = rule {
                rule.validate().map_err(|error| error.at(key))?;
            }
        }
        if let Some(rule) = &self.min_interval_ms {
            if rule.unlimited {
                return Err(ConfigError::new(
                    "CONTROL_CONFIG_PROTECTED_RULE",
                    "min_interval_ms 不允许设置为不限制（渠道硬保护不可解除）",
                )
                .at("unlimited")
                .at("min_interval_ms"));
            }
            if rule.limit.is_some_and(|value| value <= 0) {
                return Err(ConfigError::new(
                    "CONTROL_CONFIG_OUT_OF_RANGE",
                    "min_interval_ms.limit 必须大于 0",
                )
                .at("limit")
                .at("min_interval_ms"));
            }
        }
        Ok(())
    }
}

impl Validate for AccountControlScopePolicy {
    fn validate(&self) -> Result<(), ConfigError> {
        for (key, rule) in [
            ("per_minute", &self.per_minute),
            ("per_day", &self.per_day),
            ("min_interval_ms", &self.min_interval_ms),
        ] {
            if let Some(rule) = rule {
                rule.validate().map_err(|error| error.at(key))?;
            }
        }
        if self
            .min_interval_ms
            .as_ref()
            .is_some_and(|rule| rule.limit <= 0)
        {
            return Err(ConfigError::new(
                "CONTROL_CONFIG_OUT_OF_RANGE",
                "min_interval_ms.limit 必须大于 0",
            )
            .at("limit")
            .at("min_interval_ms"));
        }
        Ok(())
    }
}

impl Validate for AccountControlOperationOverride {
    fn validate(&self) -> Result<(), ConfigError> {
        warning(self.daily_warning_percent)?;
        for (key, scope) in [("account", &self.account), ("symbol", &self.symbol)] {
            if let Some(scope) = scope {
                scope.validate().map_err(|error| error.at(key))?;
            }
        }
        Ok(())
    }
}

impl Validate for AccountControlOperationPolicy {
    fn validate(&self) -> Result<(), ConfigError> {
        warning(self.daily_warning_percent)?;
        self.account
            .validate()
            .map_err(|error| error.at("account"))?;
        if let Some(scope) = &self.symbol {
            scope.validate().map_err(|error| error.at("symbol"))?;
        }
        Ok(())
    }
}

fn operation_key(registry: &Registry, key: &str, warning: Option<i64>) -> Result<(), ConfigError> {
    if !registry.operations.contains(key) {
        return Err(ConfigError::new(
            "CONTROL_CONFIG_UNKNOWN_OPERATION",
            format!("未注册的 operation key: {key}"),
        ));
    }
    if warning.is_some() && !matches!(key, "place_order" | "cancel_order") {
        return Err(ConfigError::new(
            "CONTROL_CONFIG_UNSUPPORTED_WARNING",
            "每日额度预警仅支持账户报单和撤单",
        )
        .at("daily_warning_percent"));
    }
    Ok(())
}

fn group_key(registry: &Registry, key: &str) -> Result<(), ConfigError> {
    if !registry.groups.contains(key) {
        return Err(ConfigError::new(
            "CONTROL_CONFIG_UNKNOWN_GROUP",
            format!("未注册的 group key: {key}"),
        ));
    }
    Ok(())
}

impl Validate for AccountControlOverride {
    fn validate(&self) -> Result<(), ConfigError> {
        if let Some(value) = &self.timezone {
            timezone(value)?;
        }
        let registry = REGISTRY.read().unwrap();
        for (key, operation) in &self.operations {
            operation_key(&registry, key, operation.daily_warning_percent)
                .and_then(|_| operation.validate())
                .map_err(|error| error.at(key).at("operations"))?;
        }
        for (key, scope) in &self.groups {
            group_key(&registry, key)
                .and_then(|_| scope.validate())
                .map_err(|error| error.at(key).at("groups"))?;
        }
        Ok(())
    }
}

fn validate_policy(
    timezone_name: &str,
    operations: &BTreeMap<String, AccountControlOperationPolicy>,
    groups: &BTreeMap<String, AccountControlScopePolicy>,
) -> Result<(), ConfigError> {
    timezone(timezone_name)?;
    let registry = REGISTRY.read().unwrap();
    for (key, operation) in operations {
        operation_key(&registry, key, operation.daily_warning_percent)
            .and_then(|_| operation.validate())
            .map_err(|error| error.at(key).at("operations"))?;
    }
    for (key, scope) in groups {
        group_key(&registry, key)
            .and_then(|_| scope.validate())
            .map_err(|error| error.at(key).at("groups"))?;
    }
    Ok(())
}

impl Validate for AccountControlPolicy {
    fn validate(&self) -> Result<(), ConfigError> {
        validate_policy(&self.timezone, &self.operations, &self.groups)
    }
}

impl Validate for EffectiveAccountControlPolicy {
    fn validate(&self) -> Result<(), ConfigError> {
        validate_policy(&self.timezone, &self.operations, &self.groups)?;
        validate_operation_groups(&self.operation_groups)
    }
}

fn validate_operation_groups(
    groups: &BTreeMap<String, BTreeSet<String>>,
) -> Result<(), ConfigError> {
    let registry = REGISTRY.read().unwrap();
    for (operation, groups) in groups {
        operation_key(&registry, operation, None)
            .map_err(|error| error.at(operation).at("operation_groups"))?;
        for group in groups {
            group_key(&registry, group)
                .map_err(|error| error.at(operation).at("operation_groups"))?;
        }
    }
    Ok(())
}

pub fn parse_override(value: Value) -> Result<Option<AccountControlOverride>, ConfigError> {
    if value.is_null() {
        return Ok(None);
    }
    parse(value).map(Some)
}

pub fn public_override(value: &Value) -> Result<Value, ConfigError> {
    Ok(serde_json::to_value(parse_override(value.clone())?)
        .expect("control policy serializes to JSON"))
}

fn resolve_rule(
    preset: Option<&AccountControlRuleOverride>,
    override_: Option<&AccountControlRuleOverride>,
) -> Option<AccountControlRule> {
    let mut limit = None;
    let mut on_trigger = AccountControlTriggerBehavior::WAIT;
    for rule in [preset, override_].into_iter().flatten() {
        if rule.unlimited {
            limit = None;
            on_trigger = AccountControlTriggerBehavior::WAIT;
            continue;
        }
        if rule.limit.is_some() {
            limit = rule.limit;
        }
        if limit.is_some() && rule.on_trigger.is_some() {
            on_trigger = rule.on_trigger.unwrap();
        }
    }
    limit.map(|limit| AccountControlRule { limit, on_trigger })
}

fn resolve_scope(
    preset: Option<&AccountControlScopeOverride>,
    override_: Option<&AccountControlScopeOverride>,
) -> AccountControlScopePolicy {
    AccountControlScopePolicy {
        per_minute: resolve_rule(
            preset.and_then(|s| s.per_minute.as_ref()),
            override_.and_then(|s| s.per_minute.as_ref()),
        ),
        per_day: resolve_rule(
            preset.and_then(|s| s.per_day.as_ref()),
            override_.and_then(|s| s.per_day.as_ref()),
        ),
        min_interval_ms: resolve_rule(
            preset.and_then(|s| s.min_interval_ms.as_ref()),
            override_.and_then(|s| s.min_interval_ms.as_ref()),
        ),
    }
}

fn resolve_operation(
    preset: Option<&AccountControlOperationOverride>,
    override_: Option<&AccountControlOperationOverride>,
) -> AccountControlOperationPolicy {
    let preset_symbol = preset.and_then(|op| op.symbol.as_ref());
    let override_symbol = override_.and_then(|op| op.symbol.as_ref());
    AccountControlOperationPolicy {
        priority: override_
            .and_then(|op| op.priority)
            .or_else(|| preset.and_then(|op| op.priority))
            .unwrap_or_else(default_priority),
        // 操作覆盖存在时，缺省预警表示关闭，不继承预设。
        daily_warning_percent: override_.or(preset).and_then(|op| op.daily_warning_percent),
        account: resolve_scope(
            preset.and_then(|op| op.account.as_ref()),
            override_.and_then(|op| op.account.as_ref()),
        ),
        symbol: if preset_symbol.is_none() && override_symbol.is_none() {
            None
        } else {
            Some(resolve_scope(preset_symbol, override_symbol))
        },
    }
}

pub fn resolve(
    preset_key: String,
    preset: &AccountControlOverride,
    override_: Option<&AccountControlOverride>,
    operation_groups: BTreeMap<String, BTreeSet<String>>,
) -> Result<EffectiveAccountControlPolicy, ConfigError> {
    validate_operation_groups(&operation_groups)?;
    let empty = AccountControlOverride::default();
    let override_ = override_.unwrap_or(&empty);
    let registry = REGISTRY.read().unwrap();
    Ok(EffectiveAccountControlPolicy {
        preset_key,
        timezone: override_
            .timezone
            .as_ref()
            .or(preset.timezone.as_ref())
            .map_or("Asia/Shanghai", String::as_str)
            .into(),
        operations: registry
            .operations
            .iter()
            .map(|key| {
                (
                    key.clone(),
                    resolve_operation(preset.operations.get(key), override_.operations.get(key)),
                )
            })
            .collect(),
        groups: registry
            .groups
            .iter()
            .map(|key| {
                (
                    key.clone(),
                    resolve_scope(preset.groups.get(key), override_.groups.get(key)),
                )
            })
            .collect(),
        operation_groups,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rules_require_json_objects_and_strict_values() {
        for value in [
            json!([]),
            json!([1, "wait", false]),
            json!({"limit": true}),
            json!({"limit": 1.0}),
            json!({"limit": -1}),
            json!({"limit": 1, "unlimited": true}),
        ] {
            assert!(parse::<AccountControlRuleOverride>(value).is_err());
        }
        for value in [
            json!([]),
            json!({"per_day": []}),
            json!({"min_interval_ms": {"limit": 0}}),
            json!({"min_interval_ms": {"unlimited": true}}),
        ] {
            assert!(parse::<AccountControlScopeOverride>(value).is_err());
        }
        assert_eq!(
            serde_json::to_value(parse::<AccountControlRuleOverride>(json!({"limit": 0})).unwrap())
                .unwrap(),
            json!({"limit": 0, "on_trigger": null, "unlimited": false})
        );
    }

    #[test]
    fn operation_presence_disables_warning_without_losing_quotas() {
        let preset = parse::<AccountControlOperationOverride>(json!({"priority": 10, "daily_warning_percent": 70, "account": {"per_day": {"limit": 7, "on_trigger": "block"}}})).unwrap();
        let override_ =
            parse::<AccountControlOperationOverride>(json!({"priority": 0, "symbol": {}})).unwrap();
        assert_eq!(
            resolve_operation(Some(&preset), None).daily_warning_percent,
            Some(70)
        );
        let policy = resolve_operation(Some(&preset), Some(&override_));
        assert_eq!(policy.priority, 0);
        assert_eq!(policy.daily_warning_percent, None);
        assert_eq!(policy.account.per_day.unwrap().limit, 7);
        assert!(policy.symbol.is_some());
    }

    #[test]
    fn unlimited_clears_the_rule_and_does_not_carry_trigger_behavior() {
        let preset = parse::<AccountControlRuleOverride>(json!({"unlimited": true})).unwrap();
        let behavior_only =
            parse::<AccountControlRuleOverride>(json!({"on_trigger": "block"})).unwrap();
        assert!(resolve_rule(Some(&preset), Some(&behavior_only)).is_none());
        let limited = parse::<AccountControlRuleOverride>(json!({"limit": 7})).unwrap();
        let policy = resolve_rule(Some(&preset), Some(&limited)).unwrap();
        assert_eq!(policy.limit, 7);
        assert_eq!(policy.on_trigger, AccountControlTriggerBehavior::WAIT);
    }
}
