# 08 · 进阶参考

目标：在读者完成入门路线后，提供准确的函数契约、Context 查询和扩展入口。

![Context API](../../site/assets/screenshots/08-reference/02-context-api.png)

图说：统一 Context 提供账户资产、持仓、行情、未完成订单和成交查询。先查这里，再使用依赖具体渠道的完整 executor。

| 图片 | 位置 | 说明重点 |
| --- | --- | --- |
| [函数契约与上下文](../../site/assets/screenshots/08-reference/01-function-contract.png) | 前端 `/docs/custom-calc` | 何时执行、样例与账户的差异、真实交易副作用边界 |
| [Context API](../../site/assets/screenshots/08-reference/02-context-api.png) | 同页 `#api` | 字段、方法、类型和缓存范围 |
| [高级设置](../../site/assets/screenshots/08-reference/03-advanced-settings.png) | `/settings/advanced` | 数据库、日志、算法目录配置；重启生效 |
| [公共算法目录](../../site/assets/screenshots/08-reference/04-public-algorithms.png) | `/accounts/1/edit/algorithm`，展开主交易算法 | 当前渠道可选的 POV、SINGLE-MAKER、TARGET-POS-TASK、TWAP |

可直接复用的源码和既有资料：

- Context：`axile/server/context.py`。
- 函数编译与返回值检查：`axile/server/portfolio_function.py`。
- 样例隔离运行：`axile/server/portfolio_runner.py`。
- 内置函数文档的数据源：`ui/src/pages/docs/customCalcMarkdown.ts`。
- 算法参数和运行机制：[期货目标持仓](../../../axile/executor/algorithms/defaults/ctp_target_pos_task/README.md)、[单边挂单](../../../axile/executor/algorithms/defaults/single_maker/README.md)、[TWAP](../../../axile/executor/algorithms/defaults/twap/README.md)、[POV](../../../axile/executor/algorithms/defaults/pov/README.md)。
- 自定义算法注册与加载：`axile/executor/algorithms/core/base.py`、`axile/executor/algorithms/core/loader.py`。
- 可替换时钟：`axile/executor/algorithms/utils/clock.py`。

建议把“写一个新的下单算法”单列为开发进阶教程：注册入口、参数模型、渠道与槽位、查询与下单、终止处理、结果与审计、时钟与测试。本批素材展示公共算法目录和现有实现参考，没有新增或验证一个自定义下单算法。

内嵌服务直接打开 `/docs/custom-calc` 当前返回 404。上述两张内置文档图通过已加载 SPA 的正常路由渲染采集，没有修改页面内容；复现细节与上线前待办见 [采集发现](../support/findings.md)。
