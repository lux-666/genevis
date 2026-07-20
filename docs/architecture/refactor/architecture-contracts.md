# 统一资源与执行架构契约

状态：已实施，进入架构守护与 Data v2 收尾阶段
最后更新：2026-07-20

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

截至 2026-07-20，本轮重构已经完成：

| 范围 | 状态 | 当前结果 |
|---|---|---|
| 提交审阅 | 完成 | 审阅 `7bfa0e3e98ded78511796f21ba59d90c2da4ea28`，修复重复 resolver、state 原地修改、空路径、无大小/JSON 校验和缺少测试等问题 |
| 中央资源交接 | 完成 | Inventory、read_qc、genome_survey、assembly、dedup、review、Hi-C、T2T、repeat、gene、function 已迁移到逻辑资源引用 |
| 旧 handoff 清理 | 完成 | 删除 `prefer_stages`/`output_keys` 扫描、`final_*_for_*` 字段和 read_qc workflow 级 `make_fofn` |
| 执行器收口 | 完成 | 仅保留一个 `Runner` protocol；生产默认 `LocalRunner`，测试替身通过依赖注入使用 |
| 配置加载 | 完成 | descriptor registry 与 parameter catalog 使用 CWD-independent 加载；source checkout 在仓库外 smoke test 成功加载 18 个方法。wheel 构建/安装尚未形成可重复验收 |
| 文档收口 | 完成 | 本文同时承载规范、进度、验收记录和后续路线；过期分析、阶段计划、修订说明及独立进度页已删除 |

2026-07-20 又按实际代码和测试做了一次对账，不能把设计文档中的“未完成”直接当作代码现状。结论如下：

| 领域 | 实际状态 | 证据与缺口 |
|---|---|---|
| Checkpoint 预算 | 已收口 | 一个 saver decorator 同时覆盖 MemorySaver、SqliteSaver 和外部注入 saver 的完整 checkpoint/pending writes；64 KiB 目标告警、256 KiB 硬拒绝、4 KiB interrupt、16 项 route tail 与自由文本边界均有回归，终态 SQLite 在 retention 证明后压成一个独立 checkpoint |
| Storage audit | 只读运维已收口 | `app.storage.audit` 只读审计六层目录、checkpoint 字段、artifact 完整性/可达性、重复内容和 retained/reclaimable bytes；`gene-agent storage-audit --format json [--dry-run-manifest]` 已接入且无 `--apply`。删除执行与 artifact-index 压缩仍是独立后续变更 |
| Durable resume/E2E | 已有实质覆盖 | 已有真实 parent graph + SQLite 关闭/重开 resume 测试，以及 existing assembly → review → RepeatMasker/provided annotation → function completion/failure E2E；缺口是 raw reads 起点经过 read_qc/genome_survey/assembly 的整链入口测试 |
| Artifact publication | 已收口 | durable producer 先写 `work/`，再直接调用既有 `publish_artifact_record` 或 `publish_or_recover_work_artifact`；shell 已原子发布的输出只调用 `register_artifact_records`。自动 publication 拒绝 `artifacts/` 来源，冲突回归同时验证旧文件和 index 不变；没有新增一层 JSON/helper 包装 |
| Resume 事务 | 已收口 | `app.control.resume_transaction.execute_resume_transaction` 统一拥有 snapshot 校验、decision 应用、annotation intake、lifecycle event、checkpoint update、invoke 和 terminal report；三个入口只保留 I/O，并统一记录带 phase 的 `resume_failed` |
| CI/打包 | 未固化 | 仓库没有 CI workflow；当前环境缺少 `hatchling`，因此 wheel 构建无法在现有开发环境复现，不能把 source-checkout 的 CWD smoke test 等同于 wheel 安装验收 |
| 模块复杂度 | 高，但不是首个正确性修复 | `gene_annotation/nodes.py` 2364 行、`assembly_review/nodes.py` 2022 行、`ask_interactive.py` 1817 行、`control/lifecycle.py` 1542 行；`ask_interactive` 的字符串前缀识别是真实 TODO，但当前风险低于缺失的 raw-read E2E/CI 和删除型 GC 安全边界 |

全仓 Ruff 仍有 3188 个既有格式、import 顺序和行长问题；本轮新增文件通过完整 Ruff，全部变更 Python 文件通过 `F` 规则。

## 9. 验收记录

本轮实测结果：

- `git diff --check` 通过；
- 资源、state、Runner、CWD 专项测试通过；
- `/private/tmp` 中成功加载 descriptor registry；`gene-agent storage-audit --help` smoke 通过；
- 全量 `unittest`：1039 tests passed，1 skipped；
- artifact publication 与 stage 专项复验：207 tests passed；
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

## 10. 后续路线

按实际正确性风险和依赖排序，下一阶段应依次推进：

1. **已完成：修 durable artifact 不可变性。** Durable producer 统一从 `work/` 经过既有 publication primitive；已经由 runner/shell 发布的文件只注册。没有增加新的通用 JSON helper。重复执行、publication 后 checkpoint 前恢复、冲突后旧 artifact/index 不变均有回归覆盖。
2. **已完成：统一 resume transaction。** “snapshot 校验 → decision → 可选 annotation intake → lifecycle event → update checkpoint → invoke → terminal/report”由一个 control service 拥有；三个入口只负责 I/O。`update_state` 抛错/返回 `False`、invoke 抛错、无 checkpoint、重复 decision 和事件顺序进入同一失败语义。
3. **已完成：完整 checkpoint 边界与终态压缩。** `StateBudgetCheckpointer` 统一包住 MemorySaver、SqliteSaver 和外部注入 saver 的 `put`/`put_writes`；完整 state 使用 64 KiB 目标告警和 256 KiB 硬拒绝，`active_interrupt` 4 KiB，route tail 16，`review_notes`/`last_error` 有界，并统一拒绝非 JSON、非有限浮点数和内嵌 artifact body。初始运行和 resume 共用一个 terminal finalizer，入口不再各自实现 report、失败 ledger、checkpoint 补写和 compaction event。终态压缩前会从 ledger 重建 terminal projection，并在移除 checkpoint roots 的条件下证明 state 中 required artifact 仍可达；随后 SQLite 仅保留一个父链为空的顶层 terminal checkpoint，同时删除该 thread 的子图 namespace 历史。active/blocked run 和证明失败的 terminal run 均保留原 checkpoint 链。
4. **已完成只读 storage audit CLI；删除型 GC 继续暂缓。** `gene-agent storage-audit --session-id ... --data-dir ... --format json` 暴露既有 audit，可用 `--dry-run-manifest` 同时输出纯 manifest；完整性错误非零退出，CLI 不提供 `--apply`。manifest 只把可再生 `reports/` 列为自动清理候选，`work/` 仅报告 retained/conditional bytes，不进入删除候选。删除执行器、删除前 path/hash/size 复核和 artifact-index 最后压缩仍是后续独立变更。
5. **补 E2E 的真正空白并建立 CI。** 新增 raw reads → inventory → read_qc → genome_survey → assembly → review 的入口级工具-shim E2E；再把现有全量测试、SQLite resume、wheel 构建/临时安装、仓库外 CWD smoke 和新增代码 Ruff 固化为 CI。现有 assembly 后半程 E2E 不应重复建设。
6. **最后拆大模块。** Resume transaction 已按副作用边界抽出；其余大模块仍按职责拆分，不按行数机械切文件。最后再处理 `ask_interactive` structured field metadata。

优先级 1–3 是数据正确性与恢复语义，应该先完成；4–5 是可运维和防回归能力；6 是长期可维护性。Ruff 历史清理可并行分批推进，但不应挤占前三项。
