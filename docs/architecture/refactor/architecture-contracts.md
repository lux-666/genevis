# 统一资源与执行架构契约

状态：已实施，进入架构守护与减负重构阶段
最后更新：2026-07-22

本文是 `docs/architecture/refactor/` 下唯一的规范性架构文档。历史分析、决策讨论、修订说明和提交操作记录已经删除；若代码与本文冲突，以测试覆盖的当前代码为准，并应在同一变更中修正文档。

## 1. 状态所有权

`TopLevelState` 的跨阶段文件交接只使用 `resource_refs`。`stage_outputs` 是阶段私有、可检查点化的紧凑游标，可保存状态、指标、证据引用和阶段内部恢复信息，但下游阶段不得通过猜测键名或反向扫描阶段来寻找输入文件。

`ResourceRef` 的持久化形态为：

```python
{
    "resource_type": "assembly",
    "paths": ["/run/artifacts/assembly/draft.fasta"],
    "source_stage": "assembly",
    "metadata": {"candidate_bundle_refs": ["..."]},
}
```

约束：

- `resource_type` 必须与 `resource_refs` 表键一致；
- `paths` 至少包含一个非空字符串；
- 整张表必须是 JSON-only，最大 128 KiB；
- LangGraph reducer 对资源表采用“验证后整体替换”，stage node 返回 delta，不得原地修改输入 state；
- `metadata` 只存紧凑溯源信息，大对象、命令输出和指标表必须落 artifact/ledger 后保存引用。

`genome_size` 等标量属于参数或测量证据，不伪装成路径资源，也不进入 `resource_refs`。

## 2. 资源解析

所有阶段使用 `app.stages.resource_resolution.resolve_stage_resource`，优先级固定为：

1. 显式 run override；
2. `state.resource_refs[logical_resource]`；
3. 初始 asset registry fallback；
4. 返回 `found=False`。

解析器不扫描 `stage_outputs`，不接受 `prefer_stages` 或 `output_keys`。是否因缺失而阻塞由 preflight/调用阶段结合 `ResourceSpec.required` 决定，解析器本身保持无异常查询语义。

Inventory 同一逻辑资源中同时存在 clean/raw asset 时，按 `ResourceSpec.asset_types` 顺序选择同一优先类型；后续 stage 发布的中央引用始终优先于 registry，不能静默回退到较旧输入。

Stage commit 使用 `update_resource_ref(...)` 返回新的、已验证的完整资源表。它不修改传入 state。

## 3. 逻辑资源契约

| Stage | Requires | Produces/updates |
|---|---|---|
| inventory | — | `long_reads`, `short_reads`, `hic_reads`, `assembly`（按 manifest 实际存在） |
| read_qc | `long_reads` | `long_reads` |
| genome_survey | `long_reads` | 标量测量证据，不产生路径资源 |
| assembly | `long_reads` | `assembly` |
| assembly_dedup | `assembly` | `assembly` |
| assembly_review | `assembly` | `assembly` |
| hic_scaffolding | `assembly`，上下文需要时 `hic_reads` | `assembly` |
| t2t | `assembly` | `assembly` |
| repeat_annotation | `assembly` | `masked_assembly` |
| gene_annotation | `masked_assembly`；显式允许时回退 `assembly` | `predicted_proteins` |
| function_annotation | `predicted_proteins` | — |

组装候选 bundle、review decision、执行证据等仍是 artifact。它们可通过当前 `assembly` 引用的 metadata 关联，但不能成为另一套“最终文件”交接字段。

## 4. FOFN 边界

跨阶段长读输入的 canonical 形态是 `resource_refs["long_reads"].paths: list[str]`。

FOFN 仅是工具适配器或阶段内部 artifact：

- read_qc 可以为审计/工具调用生成 FOFN；
- NextDenovo 等需要 FOFN 的 adapter 在自己的 work directory 从中央路径列表生成；
- 下游阶段不得消费 `*_fofn_ref`、`assembly_input_ref` 等 stage-output handoff 键。

## 5. 执行控制与 Runner

`execution_mode="local"` 表示该阶段获准冻结并真实执行本地命令；缺失时走 planning/contract 分支。它不选择测试执行器。

所有可执行方法统一进入
[`../execution-lifecycle.md`](../execution-lifecycle.md) 定义的
`freeze -> run -> collect` 子图；stage 只拥有命令准备、领域产物收集、review
与 commit，不得新增 stage-local lifecycle 或手工 Runner/evidence 路径。

执行器只有一个 `Runner` protocol：

- 生产路径由 `run_frozen_attempt` 默认注入 `LocalRunner`；
- 测试可以显式注入 `FakeRunner` 或其他实现；
- stage 的 freeze/collect 逻辑与执行器实现解耦。

禁止把 `test`/`fake` 扩展为持久化的 `execution_mode` 值，否则运行配置会同时承担授权和依赖注入两种职责。

## 6. 方法选择边界

Descriptor registry 是方法可用性与 planner metadata 的唯一目录。TUI/run config 的显式 canonical method ID 优先，runtime 必须验证方法与 read type、参数和 descriptor 的一致性。

为保留无交互 planning 能力，未显式选择方法时 assembly planner 可以产生最多一个 primary 和一个 fallback，并将 `AssemblySelection` 作为审计记录；只有 `execution_mode="local"` 且所选方法参数完整时才会执行。`assembly_route` 当前保留为 read-type hint，不是第二份方法注册表。

这一约定取代已删除文档中“TUI 必须是唯一选择入口”和“runtime 自动选择必须全部删除”的冲突要求。

## 7. CWD 与打包

运行时代码通过 `app.configuration` 加载 packaged `descriptors`，不得直接构造 `Path("descriptors/registry.yaml")`。`descriptors` 是 wheel package，parameter catalog 与 registry 均从包根解析。

从仓库外目录运行必须成功：

```bash
cd /tmp
python -c 'from app.configuration import load_descriptor_registry; print(len(load_descriptor_registry().methods))'
gene-agent --help
```

## 8. 实施进度

截至 2026-07-22，本轮重构已经完成：

| 范围 | 状态 | 当前结果 |
|---|---|---|
| 提交审阅 | 完成 | 审阅 `7bfa0e3e98ded78511796f21ba59d90c2da4ea28`，修复重复 resolver、state 原地修改、空路径、无大小/JSON 校验和缺少测试等问题 |
| 中央资源交接 | 完成 | Inventory、read_qc、genome_survey、assembly、dedup、review、Hi-C、T2T、repeat、gene、function 已迁移到逻辑资源引用 |
| 旧 handoff 清理 | 完成 | 删除 `prefer_stages`/`output_keys` 扫描、`final_*_for_*` 字段和 read_qc workflow 级 `make_fofn` |
| 执行器收口 | 完成 | 仅保留一个 `Runner` protocol；生产默认 `LocalRunner`，测试替身通过依赖注入使用 |
| 执行生命周期收口 | 完成 | 所有可执行生产方法共享一个 freeze/run/collect graph、Runner/evidence kernel 和 collect failure boundary；参数化与直接执行仅保留 attempt ref 所有权差异 |
| 配置加载 | 完成 | descriptor registry 与 parameter catalog 使用 CWD-independent 加载；source checkout 在仓库外 smoke test 成功加载 18 个方法 |
| 文档收口 | 完成 | 当前工作树只保留规范、运行说明、状态和交接文档；过期设计与阶段记录由 Git 历史保留 |

2026-07-22 按当前代码和测试重新对账后的实施结果如下：

| 领域 | 状态 | 当前证据 |
|---|---|---|
| State 预算 | 已收口 | `StateBudgetCheckpointer` 同时覆盖 MemorySaver、SqliteSaver 和外部注入 saver 的完整 checkpoint/pending writes；64 KiB 目标告警、256 KiB 硬拒绝、4 KiB interrupt、16 项 route tail 与自由文本边界均有回归 |
| 终态压缩 | 已收口 | 初始运行和 resume 共用一个 terminal finalizer；ledger reconstruction 与 checkpoint-free artifact reachability 验证通过后，SQLite 只保留一个父链为空的顶层 terminal checkpoint；active、blocked 和验证失败的 run 保留原 checkpoint 链 |
| Storage audit | 已收口 | `app.storage.audit` 只读审计六层目录、checkpoint 字段、artifact 完整性/可达性、重复内容和 retained/reclaimable bytes；`gene-agent storage-audit --format json [--dry-run-manifest]` 已接入 |
| Durable resume/E2E | 已收口 | 真实 parent graph + SQLite 关闭/重开 resume，以及 existing assembly → review → RepeatMasker/provided annotation → function completion/failure E2E 已覆盖 |
| Artifact publication | 已收口 | durable producer 先写 `work/`，再直接调用既有 `publish_artifact_record` 或 `publish_or_recover_work_artifact`；shell 已原子发布的输出只调用 `register_artifact_records`。自动 publication 拒绝 `artifacts/` 来源，冲突回归同时验证旧文件和 index 不变；没有新增一层 JSON/helper 包装 |
| Resume 事务 | 已收口 | `app.control.resume_transaction.execute_resume_transaction` 统一拥有 snapshot 校验、decision 应用、annotation intake、lifecycle event、checkpoint update、invoke 和 terminal report；三个入口只保留 I/O，并统一记录带 phase 的 `resume_failed` |

## 9. 验收记录

本轮实测结果：

- `git diff --check` 通过；
- 资源、state、Runner、CWD 专项测试通过；
- `/private/tmp` 中成功加载 descriptor registry；`gene-agent storage-audit --help` smoke 通过；
- 执行生命周期、collect 与 stage routing 专项复验：118 tests passed；
- 全量 `unittest`：1081 tests passed，1 skipped；
- checkpoint/audit/durable resume/entrypoint 专项复验通过；
- checkpoint 回归确认 memory、SQLite 和注入 saver 均经过统一边界；terminal retention 失败、缺少 terminal event、active/blocked state 均不会删除历史，成功路径只保留一个父链为空的顶层 checkpoint；
- 新增文件通过完整 Ruff，全部变更 Python 文件通过 Ruff/Pyflakes `F` 检查。

持续验收时，以下查询在 Python 运行时代码中应无结果：

```bash
rg 'prefer_stages|output_keys' app --glob '*.py'
rg 'final_[a-z_]+_for_[a-z_]+' app --glob '*.py'
rg 'Path\("descriptors/registry\.yaml"\)' app --glob '*.py'
rg 'assembly_input_fofn|filtered_reads_fofn' app --glob '*.py'
```

还必须覆盖：资源优先级、表大小/JSON 验证、非原地更新、inventory → read_qc → assembly、下游 assembly 更新链、FakeRunner 注入和任意 CWD descriptor 加载。

## 10. 剩余减负重构

剩余工作只服务于减少重复实现、缩小模块职责和提高修改速度，不增加新的存储层、执行模式或产品能力：

1. **统一 run identity 所有权。** 内部接口逐步只传一个 typed run context/`SessionPaths`，删除同时接收 run root 与第二份 `session_id` 的旧签名。迁移必须在同一变更中删除旧调用，不保留兼容 adapter。
2. **按职责拆分大模块。** 优先拆出可独立理解和测试的副作用边界，例如输入收集、计划编辑、stage orchestration、review rendering 和 artifact persistence。移动完成后删除原实现、重复 helper 和镜像导出，不以 facade 保留整套旧 API。
3. **结构化 `ask_interactive` 字段元数据。** 用显式 metadata 取代字符串前缀、字段名猜测和分散条件分支；切换后删除旧识别逻辑、旧字段约定和对应兼容测试，不双写两种表示。
4. **固化重构护栏。** 把现有全量测试、durable SQLite resume、仓库外 CWD 加载和关键入口 E2E 固化为自动检查。新增测试只保护现有行为和删除旧实现后的单轨边界，不引入第二套测试专用运行架构。
