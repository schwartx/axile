//! 策略配置的原生快照直接供 Python guard 使用，不重建 Python 业务模型。

use std::collections::BTreeSet;

use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple, PyType,
};
use serde_json::Value;

use crate::control_policy::*;
use crate::python::json_to_py;

impl From<ConfigError> for PyErr {
    fn from(error: ConfigError) -> Self {
        Python::attach(|py| {
            let result = py
                .import("axile.executor.account_control.exceptions")?
                .getattr("AccountControlConfigError")?
                .call1((error.code, error.path, error.message))?;
            Ok::<_, PyErr>(PyErr::from_value(result))
        })
        .unwrap_or_else(|error| error)
    }
}

fn input(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    input_at(value, &mut Vec::new())
}

fn input_error(path: &[String], code: &'static str, message: &str) -> PyErr {
    let mut error = ConfigError::new(code, message);
    error.path = path.to_vec();
    error.into()
}

fn input_string(value: &Bound<'_, PyString>, path: &[String]) -> PyResult<String> {
    value.to_str().map(str::to_owned).map_err(|_| {
        input_error(
            path,
            "CONTROL_CONFIG_INVALID_TYPE",
            "配置字符串必须是有效的 Unicode",
        )
    })
}

fn input_at(value: &Bound<'_, PyAny>, path: &mut Vec<String>) -> PyResult<Value> {
    if path.len() > 32 {
        return Err(input_error(
            path,
            "CONTROL_CONFIG_NESTING_LIMIT",
            "配置嵌套过深或存在循环引用",
        ));
    }
    if value.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(fields) = value.cast::<PyDict>() {
        let mut result = serde_json::Map::new();
        for (key, value) in fields.iter() {
            let key = input_string(
                key.cast::<PyString>().map_err(|_| {
                    input_error(
                        path,
                        "CONTROL_CONFIG_INVALID_TYPE",
                        "配置对象的 key 必须是字符串",
                    )
                })?,
                path,
            )?;
            path.push(key.clone());
            let converted = input_at(&value, path);
            path.pop();
            result.insert(key, converted?);
        }
        return Ok(Value::Object(result));
    }
    if value.is_instance_of::<PyBool>() {
        return value.extract().map(Value::Bool);
    }
    if value.is_instance_of::<PyInt>() {
        return value
            .extract::<i64>()
            .map(|n| Value::Number(n.into()))
            .map_err(|_| {
                input_error(
                    path,
                    "CONTROL_CONFIG_OUT_OF_RANGE",
                    "整数超出 64 位有符号范围",
                )
            });
    }
    if value.is_instance_of::<PyFloat>() {
        return serde_json::Number::from_f64(value.extract()?)
            .map(Value::Number)
            .ok_or_else(|| {
                input_error(
                    path,
                    "CONTROL_CONFIG_INVALID_TYPE",
                    "配置不接受 NaN 或 Infinity",
                )
            });
    }
    if let Ok(value) = value.cast::<PyString>() {
        return input_string(value, path).map(Value::String);
    }
    if value.is_instance_of::<PyList>()
        || value.is_instance_of::<PyTuple>()
        || value.is_instance_of::<PySet>()
        || value.is_instance_of::<PyFrozenSet>()
    {
        let mut result = Vec::new();
        for (index, item) in value.try_iter()?.enumerate() {
            path.push(index.to_string());
            let converted = input_at(&item?, path);
            path.pop();
            result.push(converted?);
        }
        return Ok(Value::Array(result));
    }
    macro_rules! native_value {
        ($($name:ty),+) => {$(
            if let Ok(value) = value.extract::<PyRef<'_, $name>>() {
                return Ok(serde_json::to_value(&*value).expect("control policy serializes to JSON"));
            }
        )+};
    }
    native_value!(
        AccountControlTriggerBehavior,
        AccountControlRuleOverride,
        AccountControlRule,
        AccountControlScopeOverride,
        AccountControlScopePolicy,
        AccountControlOperationOverride,
        AccountControlOperationPolicy,
        AccountControlOverride,
        AccountControlPolicy,
        EffectiveAccountControlPolicy
    );
    Err(input_error(
        path,
        "CONTROL_CONFIG_INVALID_TYPE",
        "配置只接受 JSON 值或原生策略对象",
    ))
}

macro_rules! model {
    ($name:ident, {$($methods:tt)*}) => {
        #[pymethods]
        impl $name {
            #[new]
            #[pyo3(signature = (**kwargs))]
            fn new(_py: Python<'_>, kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
                let value = kwargs.map_or_else(|| Ok(serde_json::json!({})), |value| input(value.as_any()))?;
                parse(value).map_err(PyErr::from)
            }

            #[classmethod]
            fn model_validate(_class: &Bound<'_, PyType>, value: &Bound<'_, PyAny>) -> PyResult<Self> {
                if let Ok(value) = value.extract::<PyRef<'_, Self>>() { return Ok(value.clone()); }
                parse(input(value)?).map_err(PyErr::from)
            }

            #[pyo3(signature = (*, mode = "python", exclude = None))]
            fn model_dump(&self, py: Python<'_>, mode: &str, exclude: Option<BTreeSet<String>>) -> PyResult<Py<PyAny>> {
                let _ = mode;
                let mut value = serde_json::to_value(self).expect("control policy serializes to JSON");
                if let Some(exclude) = exclude { for key in exclude { value.as_object_mut().unwrap().remove(&key); } }
                json_to_py(py, &value)
            }

            #[pyo3(signature = (*, deep = false, update = None))]
            fn model_copy(&self, deep: bool, update: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
                let _ = deep;
                let Some(update) = update else { return Ok(self.clone()); };
                let mut value = serde_json::to_value(self).unwrap();
                value.as_object_mut().unwrap().extend(input(update.as_any())?.as_object().unwrap().clone());
                parse(value).map_err(PyErr::from)
            }

            fn __deepcopy__(&self, _memo: &Bound<'_, PyAny>) -> Self { self.clone() }
            $($methods)*
        }
    };
}

model!(AccountControlRuleOverride, {});
model!(AccountControlRule, {});
model!(AccountControlScopeOverride, {});
model!(AccountControlScopePolicy, {});
model!(AccountControlOperationOverride, {});
model!(AccountControlOperationPolicy, {});
model!(AccountControlOverride, {});
model!(AccountControlPolicy, {
    fn operation(&self, key: &str) -> Option<AccountControlOperationPolicy> {
        self.operations.get(key).cloned()
    }
    fn group(&self, key: &str) -> Option<AccountControlScopePolicy> {
        self.groups.get(key).cloned()
    }
    fn groups_for(&self, _operation: &str) -> Vec<String> {
        Vec::new()
    }
    fn operations_in(&self, _group: &str) -> Vec<String> {
        Vec::new()
    }
});
model!(EffectiveAccountControlPolicy, {
    fn operation(&self, key: &str) -> Option<AccountControlOperationPolicy> {
        self.operations.get(key).cloned()
    }
    fn group(&self, key: &str) -> Option<AccountControlScopePolicy> {
        self.groups.get(key).cloned()
    }
    fn groups_for(&self, operation: &str) -> Vec<String> {
        self.operation_groups
            .get(operation)
            .map(|groups| groups.iter().cloned().collect())
            .unwrap_or_default()
    }
    fn operations_in(&self, group: &str) -> Vec<String> {
        self.operation_groups
            .iter()
            .filter(|(_, groups)| groups.contains(group))
            .map(|(key, _)| key.clone())
            .collect()
    }
});

#[pymethods]
impl AccountControlTriggerBehavior {
    #[getter]
    fn value(&self) -> &'static str {
        match self {
            Self::WAIT => "wait",
            Self::BLOCK => "block",
        }
    }

    fn __str__(&self) -> &'static str {
        self.value()
    }
}

#[pyfunction]
fn control_registry(operations: Vec<String>, groups: Vec<String>) {
    set_registry(operations, groups);
}

fn override_input(value: &Bound<'_, PyAny>) -> PyResult<Option<AccountControlOverride>> {
    if value.is_none() {
        return Ok(None);
    }
    if let Ok(value) = value.extract::<PyRef<'_, AccountControlOverride>>() {
        return Ok(Some(value.clone()));
    }
    parse_override(input(value)?).map_err(PyErr::from)
}

#[pyfunction]
fn resolve_control_policy(
    preset_key: String,
    preset: PyRef<'_, AccountControlOverride>,
    override_: &Bound<'_, PyAny>,
    operation_groups: &Bound<'_, PyAny>,
) -> PyResult<EffectiveAccountControlPolicy> {
    let groups = decode(input(operation_groups)?)
        .map_err(|error| PyErr::from(error.at("operation_groups")))?;
    resolve(
        preset_key,
        &preset,
        override_input(override_)?.as_ref(),
        groups,
    )
    .map_err(PyErr::from)
}

#[pyfunction]
fn control_override_schema(py: Python<'_>) -> PyResult<Py<PyAny>> {
    let schema = serde_json::to_value(schemars::schema_for!(AccountControlOverride)).unwrap();
    // 内联本地引用，FastAPI 无须再声明另一套嵌套策略类型。
    fn inline(value: &Value, root: &Value) -> Value {
        match value {
            Value::Object(fields) => {
                if let Some(reference) = fields.get("$ref").and_then(Value::as_str) {
                    return inline(
                        root.pointer(reference.strip_prefix('#').unwrap()).unwrap(),
                        root,
                    );
                }
                Value::Object(
                    fields
                        .iter()
                        .filter(|(key, _)| !matches!(key.as_str(), "$defs" | "$schema"))
                        .map(|(key, value)| (key.clone(), inline(value, root)))
                        .collect(),
                )
            }
            Value::Array(items) => {
                Value::Array(items.iter().map(|item| inline(item, root)).collect())
            }
            _ => value.clone(),
        }
    }
    json_to_py(py, &inline(&schema, &schema))
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<AccountControlTriggerBehavior>()?;
    module.add_class::<AccountControlRuleOverride>()?;
    module.add_class::<AccountControlRule>()?;
    module.add_class::<AccountControlScopeOverride>()?;
    module.add_class::<AccountControlScopePolicy>()?;
    module.add_class::<AccountControlOperationOverride>()?;
    module.add_class::<AccountControlOperationPolicy>()?;
    module.add_class::<AccountControlOverride>()?;
    module.add_class::<AccountControlPolicy>()?;
    module.add_class::<EffectiveAccountControlPolicy>()?;
    module.add_function(wrap_pyfunction!(control_registry, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_control_policy, module)?)?;
    module.add_function(wrap_pyfunction!(control_override_schema, module)?)?;
    Ok(())
}
