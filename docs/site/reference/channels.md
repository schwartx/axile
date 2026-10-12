# 渠道与配置

Axile 公共核心内置 CTP、掘金和天勤。使用前需要安装对应依赖，确认 SDK 支持的运行平台，并选择账户模式。

## 安装渠道依赖

以下命令在独立 Axile 仓库根目录执行：

| 渠道 | 命令 | 说明 |
| --- | --- | --- |
| 天勤 | `uv sync --extra tqsdk` | 本指南使用的连接表单；支持实盘、快期模拟、本地模拟 |
| CTP | `uv sync --extra ctp` | 依赖 OpenCTP 原生模块，需检查平台和连接环境 |
| 掘金 | `uv sync --extra gm` | SDK 支持的平台运行；macOS 不安装 GM SDK |
| 全部 extras | `uv sync --extra all` | 仍受平台与第三方 SDK 限制 |

依赖要求见 [pyproject.toml](https://github.com/zengbin93/axile/blob/main/pyproject.toml)。渠道显示“暂不可用”时，先检查对应依赖是否已安装；反复提交连接表单无法解决依赖缺失。

## 账户连接模式

天勤三种模式共用天勤账号与密码。实盘模式另外填写期货公司和交易账户凭据；本地模拟另外填写初始资金。

本地模拟状态保存在当前 Worker 中，日盘或夜盘 Worker 重建后会重置。实盘或模拟由账户连接模式决定，运行环境 `environment=local` 只影响日志，不会把账户改成模拟账户。

## 高级设置

![系统高级设置中的数据库、日志和用户算法目录](../assets/screenshots/08-reference/03-advanced-settings.png)

| 设置 | 含义 |
| --- | --- |
| 数据库 | 主服务使用 SQLite；默认位于运行目录 |
| 运行环境 | 控制日志详细程度 |
| 日志目录与滚动 | 日志写入位置及切分周期 |
| 用户算法目录 | 启动时扫描并注册自定义算法的目录 |

主服务读取配置文件和代码默认值，不从 `.env` 读取业务配置。`AXILE_CONFIG_TOML` 用来指定配置文件路径，不能逐项覆盖配置。

保存需要重新读取的配置后，要重启服务。直接用 CLI 启动的，再执行一次启动命令；托管运行的，由外部 supervisor 重启。
