这些文件只作为测试 oracle，不随生产 wheel 加载。

- `v0.22.0.sql` 保留标签 `bc70e4a3785eacedeaf4eadcda5a3109d3a3ffa6` 的原始结构，公开 head 为 `0033`。对应 JSON 固定标签、迁移链和 DDL 哈希；不要用当前版本覆盖这个文件。
- `public-0039.sql` 来自冻结源码 `509b3627ca5fc6ee5f5a72e91b5ebebed3cf9879` 的完整 Python 迁移链，包含 73 张表、45 个显式索引和 243 个触发器。对应 JSON 记录迁移依赖源码哈希和实际列声明。
- `public-0033-data.json` 使用同一冻结源码中的合成历史场景，保存 22 条执行记录、资产、目标、审计、通知 outbox 和分析结果；`seed` 是 0033 的 50 行输入，`expected` 是迁移后 0039 的 593 行结果。JSON 标量以规范字符串比较，保留 SQL NULL、JSON null 和整数/浮点数的区别。

`capture_baseline.py` 与 `capture_data.py` 只在隔离临时文件库运行旧迁移。重新生成时，把 `PYTHONPATH` 指向冻结源码，并分别传入 `--source`、`--revision 0039 --commit <commit>`、`--output`；数据脚本仅需要 source 和 output。脚本先核对实际导入的 `axile.__file__`。

日常测试通过 `tests.conftest.initialize_native_database(path)` 创建真实当前 schema，缺少原生能力时直接失败。`oracle_tables.py` 只为测试侧 SQLAlchemy 查询和显式夹具写入提供列类型，不能用它建表或代替原生事务。
