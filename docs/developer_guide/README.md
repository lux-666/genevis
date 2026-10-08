<a id="developer-guide-index"></a>

# 开发者指南索引

本页是 Gene Agent 代码、架构、描述符、测试与文档贡献者的导航层，不定义运行时契约。精确规则由链接的架构参考文档维护。

<a id="start-here"></a>

## 从这里开始

- [架构地图](../architecture/README.md)：专题导航、参考文档注册表、状态词汇与文档同步规则。
- [系统地图](../architecture/topics/system-map.md)：模块职责与三级图结构。
- [架构治理](../architecture/reference/governance.md)：权威来源、变更控制、边界规则和仓库质量政策。
- [实现地图](../architecture/langgraph-implementation-map.md)：当前图与方法接线、验证证据和明确的欠账。它是状态投影，不替代契约。
- [一致性缺口登记](../architecture/reference/conformance.md)：已接受设计与当前工作树的差距。

<a id="route-by-change"></a>

## 按改动类型导航

| 改动范围 | 接下来阅读 |
|---|---|
| 父图路由、冻结的阶段实例、终态行为 | [运行时编排](../architecture/topics/runtime-orchestration.md)，然后阅读[运行数据契约](../architecture/reference/data-contract.md) |
| 阶段输入、输出、方法或生物学边界 | [阶段契约](../architecture/reference/stage-contracts.md)，然后查看 `app/stages/` 中受影响的阶段 |
| 运行就绪检查、方法冻结、执行器、收集或重试 | [方法执行](../architecture/topics/execution.md)、[执行生命周期](../architecture/reference/execution-lifecycle.md)、[执行证据](../architecture/reference/execution-evidence.md) |
| 跨阶段文件、资源解析或组装交付 | [资源交接](../architecture/reference/resource-handoff.md) |
| 基因组大小、平台/reads 类型或物种身份 | [共享生物学事实](../architecture/reference/shared-facts.md)，然后阅读消费这些事实的阶段契约 |
| LLM 参数、审批或重试链 | [参数推荐](../architecture/reference/parameter-recommendation.md)及对应阶段参数工作流 |
| 人工关卡、评审或恢复 | [评审与恢复](../architecture/topics/review-and-resume.md)，然后阅读其链接的运行数据与执行参考契约 |
| 软件包/方法注册或环境能力 | [描述符契约](../architecture/reference/descriptors.md)、`descriptors/registry.yaml` 及其引用的描述符文件 |
| bootstrap 或主机配置 | [项目初始化](../architecture/topics/bootstrap.md)，然后查看提交到仓库的 `docs/examples/envs.yaml.example`、手工或 bootstrap 维护且被 Git 忽略的 `config/envs.yaml`，以及执行环境解析器 |

<a id="tests-and-contribution-entry"></a>

## 测试与贡献入口

- [测试套件](../../tests/)：针对边界的测试与完整回归覆盖。从所修改实现地图条目列出的测试文件开始。
- [项目结构测试](../../tests/test_project_scaffold.py)：阶段顺序、存储边界、架构入口和已退役规划路径的结构检查。
- [Python/开发元数据](../../pyproject.toml)：依赖与工具配置权威。
- [服务器验证助手](../../scripts/)：主机检查与回归检查；部署操作由用户指南导航。

修改非简单行为前，确定唯一权威参考契约，同步修改代码及针对性测试。仅在接线、状态、证据或欠账改变时更新相关专题和实现地图。不要新增第二份架构摘要、阶段报告或交接快照。
