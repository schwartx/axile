# 内部数据对象与输入校验

前端、服务端、worker 和数据库使用同一套内部协议。第一方 Python 数据对象使用标准库 dataclass，不继承 Pydantic，也不在构造、赋值或复制时自动校验。CTP、Binance、GM、TQ 的 SDK 返回值由各渠道适配器转换；字段清洗、行情可用性检查和下单前的交易约束继续保留。

## 对象与载荷

- 已有对象在同一进程中直接传递。快照需要独立修改时用 `copy_data(value, deep=True)`，更新普通对象可用标准库 `dataclasses.replace()`。
- HTTP、IPC 和存储出口用 `to_payload(value, mode="json")` 导出字段。内部字段的 `field(metadata={"exclude": True})`、公开投影和嵌套字段裁剪继续生效。
- 本程序生成的字典用 `from_payload(Type, value)` 恢复嵌套对象。恢复不重复运行业务校验；传入已经符合类型的对象时复用它。
- 枚举、日期、缺省值及派生字段的出口行为由序列化函数负责。非有限数值事实保留原值，任意元数据中的非有限数值导出为 JSON `null`。

不要把对象先导成完整字典，只为读取其中少数字段。条件判断直接访问属性；只在实际需要载荷的边界序列化。

## 配置和执行入口

编辑请求通过 `parse_input(Type, raw)` 解析。msgspec 根据类型注解及 `Meta` 检查输入结构和字段范围，然后调用对象的 `validate()` 检查业务组合约束。账户保存、算法参数解析、组合计算结果和外部 SDK 数据转换分别在所属入口检查，内部读取与转发不再重做校验。

HTTP 请求体通过 `Depends(json_body(Type))` 中的 `Body` 声明交给 FastAPI/Pydantic 解析，保留 JSON Content-Type 与对象结构检查；随后复用 `parse_input()` 检查配置字段和业务约束。查询参数使用 `Depends(query_params(Type))`。内部 dataclass 不交给框架重复构建模型。HTTP 参数错误仍返回 422；鉴权、字段白名单、凭据脱敏和数据库事务边界保持原有职责。

PATCH 对象用 `msgspec.UNSET` 区分省略字段与显式 `null`。`to_payload(..., exclude_unset=True)` 只导出实际更新项，`false`、`0` 和空数组都是明确提供的值。

## 算法参数与前端表单

参数默认值、字段约束和表单说明保持在同一份 dataclass 声明中：

```python
from dataclasses import dataclass
from typing import Annotated

from msgspec import Meta

from axile import BaseAlgorithmParams, register_algorithm


@dataclass(kw_only=True)
class SliceParams(BaseAlgorithmParams):
    slices: Annotated[int, Meta(ge=1, le=100, title="分片数")] = 4


@register_algorithm("MY-SLICES", params_class=SliceParams)
def execute(executor, algorithm_input):
    ...
```

注册时从参数类型生成 JSON Schema 和默认值，前端继续消费现有 `params_schema`、`default_params`。运行时得到普通参数对象。追单组合等业务规则保留为 `validate()` 方法，在参数入口调用。

## SDK 调用迁移

原来的 `model_validate()`、`model_dump()`、`model_copy()` 不再是 Python 数据对象的方法。对应入口为 `parse_input()` 或 `from_payload()`、`to_payload()`、`copy_data()`。构造 dataclass 时应传入已经确定类型的字段；嵌套字典需要先恢复对象，不能依赖构造器隐式转换。用户算法的参数子类也必须显式使用 `@dataclass(kw_only=True)`。

账户控制配置的 Rust 快照继续保留其原生 `model_validate()`、`model_dump()` 和 `model_copy()` 接口；这些类型本来就不继承 Pydantic。此改动不迁移数据库，也不修改外部 SDK 的模型实现。
