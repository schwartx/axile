这些文件只作为测试 oracle，不随生产 wheel 加载。

- `v0.23.1.sql` 是当前升级入口的正式基线：由真实标签 `v0.23.1`（`4b874083bf9cc4cbc76f9e1c5a51ed7fdd4839ad`）执行到公开 head `0033` 后生成。对应 JSON 固定源码、DDL 哈希及列契约；含 36 张表、37 个索引和 15 个触发器。测试使用 `schema_contract.load_schema` 按依赖顺序加载 DDL，随后显式写入版本 `0033`，数据场景另外注入。
- `v0.22.0.sql` 保留标签 `bc70e4a3785eacedeaf4eadcda5a3109d3a3ffa6` 的原始结构，公开 head 为 `0033`。对应 JSON 固定标签、迁移链和 DDL 哈希；不要用当前版本覆盖这个文件。
- `public-0034.json` 的当前受管表列类型由实际原生新建库生成；退役的 `performance_*` 条目只服务独立分析计算参照，不代表生产表仍存在。真实表、索引和触发器必须由原生迁移创建。未发布中间版本的 DDL 夹具已删除。
- `public-0033-data.json` 使用同一冻结源码中的合成历史场景，保存 22 条执行记录、资产、目标、审计、通知 outbox 和分析结果；测试只比较业务事实与完整摘要、通知正文，不要求保留旧绩效缓存。`seed` 是 0033 的 50 行输入，`expected` 是开发期迁移生成的冻结业务事实期望；其中 `0039` 只记录来源，不表示继续支持该中间版本。JSON 标量以规范字符串比较，保留 SQL NULL、JSON null 和整数/浮点数的区别。

`capture_baseline.py` 与 `capture_data.py` 仅用于复现冻结源码的历史 oracle；其中旧修订号属于证据来源，不表示当前支持 PR 中间版本。它们只在隔离临时文件库运行旧迁移。重新生成时，把 `PYTHONPATH` 指向冻结源码，并分别传入 `--source`、`--revision 0033 --commit <tag-commit>`、`--output`；数据脚本仅需要 source 和 output。脚本先核对实际导入的 `axile.__file__`。

日常测试通过 `tests.conftest.initialize_native_database(path)` 创建真实当前 schema，缺少原生能力时直接失败。`oracle_tables.py` 只为测试侧 SQLAlchemy 查询和显式夹具写入提供列类型，不能用它建表或代替原生事务。

迁移收敛后的生产入口仅为空库和已发布的 `0033`；`0001`—`0032` 在改库前拒绝并提示先升级 `v0.23.1`。冻结旧版本源码及 Python capture 工具只用于复现测试证据，运行时迁移由 Rust 执行。新版 `0034` 的最终结构和业务结果应分别对照新建库、`0033` 升级库及收敛前 `next` 基线，不能仅比较迁移版本号或表数量。
