<a id="langgraph-implementation-map"></a>

# LangGraph 实现映射

> **状态：** 当前实现映射，明确标记部分实现和欠账。
>
> **阅读场景：** 修改或扩展阶段前，确定真实存在的图节点、方法路线、参数流程和评审边界。
>
> **来源：** 冻结工作流 occurrence、编译图 builder、描述符注册表、方法适配器、聚焦测试与架构一致性审查。
>
> **下游：** 实现规划、代码评审、方法接入、精确架构契约更新。
>
> **依赖契约：** [运行时编排](topics/runtime-orchestration.md)、[阶段契约](reference/stage-contracts.md)、[描述符](reference/descriptors.md)、[参数推荐](reference/parameter-recommendation.md)、[案例契约](reference/casepack.md)、[架构一致性](reference/conformance.md)。
>
> **代码依据：** `app/workflow.py`、`app/graph/`、所有 `app/stages/*/builder.py`、`app/execution/`、`descriptors/registry.yaml` 及下方聚焦测试。

本页投影当前树中已接线、可执行、已验证、部分实现或缺失的能力。精确行为仍由链接的参考契约拥有。

<a id="how-to-read-status"></a>

## 状态读法

编译阶段不自动等于能力完成。每个单元沿四个独立轴分类。

| 轴 | 值 | 含义 |
|---|---|---|
| 接线 | `wired`、`unwired`、`not-applicable` | 能否通过图边、条件路线或注册方法路线到达 |
| 实现 | `executable`、`implemented-no-process`、`partial`、`placeholder`、`absent` | 真实 Runner 路径、有意无进程行为、不完整边界、只有命名外壳而无闭环、没有建模单元 |
| 验证 | `tested`、`partially-tested`、`untested` | 是否有针对性测试覆盖声明，不等于全部仓库质量门当前通过 |
| 欠账 | `none`、`known-gap`、`contract-drift` | 无记录限制、有界产品限制、一致性登记的契约漂移 |

“不需要”是能力决策，不是失败。例如 Inventory 不需要 LLM 参数推荐器。`placeholder` 指有命名接口或图位置却没有执行闭环；`absent` 指完全没有规范阶段、注册方法或图契约。

<a id="parent-graph"></a>

## 父图

父图是控制器循环，不是无条件线性流水线：选择当前不可变 `workflow_step`，调用对应子图，再回控制器。Occurrence 身份、结算、字面顺序、重复阶段、Review 目标和游标规则由[运行数据](reference/data-contract.md)及[阶段契约](reference/stage-contracts.md)拥有，本页只记录接线和证据。

父图没有生物学阶段之间的直接边，每个编译阶段返回 `route_controller`。子图拥有生物学选择与证据；父图记录阶段进入并调用共享结算；权威终结器拥有会话终态。操作者报告由执行器/投影边界生成，不由父控制器生成。

| 父图单元 | 接线 | 实现 | 验证 | 欠账 | 证据 |
|---|---|---|---|---|---|
| `route_controller`、`route_from_current_stage` | `wired` | `implemented-no-process` | `tested` | `none` | `app/graph/routing.py`；`tests/test_graph_routing.py`、`tests/test_routing_contracts.py` |
| 冻结 occurrence 路由与结算 | `wired` | `implemented-no-process` | `tested` | `none` | 每条阶段终态路径返回冻结 occurrence 绑定的明确 `workflow_step_outcome`；控制器/恢复共享边界校验它，先记录不可变交付再写单例阶段终态，拒绝游标/路由标记推断。`app/schemas/state.py`、`app/control/workflow_plan.py`、`app/control/occurrence_settlement.py`、`app/graph/builder.py`、`app/graph/routing.py`；`tests/test_graph_routing.py`、`tests/test_routing_contracts.py`、`tests/test_state_schema.py`、`tests/test_terminal_finalizer.py` |
| Review occurrence 位置策略 | `wired` | `implemented-no-process` | `tested` | `none` | RunConfig 保留可选字面 Review 位置，序列分析只绑定明确 occurrence。确定性、manifest 和 LLM 规划共用含 GapCloser、Base Polish、最终 Review 的可编辑完整推荐。`app/workflow.py`、`app/schemas/run_config.py`、`app/entrypoints/tui_intake.py`、`app/entrypoints/ask_planner.py`；`tests/test_run_config.py`、`tests/test_workflow_plan.py`、`tests/test_tui_intake.py`、`tests/test_ask_plan.py` |
| Base Polish 采纳边界 | `wired` | `executable` | `tested` | `known-gap` | HiFi Racon、成对证据、持久采纳/保留/停止；`app/stages/base_polish/`；`tests/test_base_polish.py` |
| SQLite/memory checkpoint 选择和持久恢复 | `wired` | `implemented-no-process` | `tested` | `none` | `app/graph/checkpointer.py`、`app/control/lifecycle.py`；`tests/test_checkpointer.py`、`tests/test_durable_resume.py` |
| 前台评审倒计时 | `wired` | `implemented-no-process` | `tested` | `none` | 冻结策略与注册 resolver 进入人工决策共用的类型化意图、持久准备、已验证应用和恢复协议。倒计时仅存在于原附着执行器，后续 review/resume 人工恢复。值与绑定由运行数据契约拥有。`app/review/application.py`、`app/control/resume_transaction.py`、`app/review/approval.py`；`tests/test_review_application.py`、`tests/test_foreground_review_countdown.py`、`tests/test_parameter_persistence_v2.py` |
| 权威终结、报告、checkpoint 压缩 | `wired` | `implemented-no-process` | `tested` | `none` | `finalize_authoritative_terminal()` 先证明 checkpoint 再写 Ledger 终态单例，随后尽力压缩。报告经 `ProjectionBoundary`，不得改变生命周期权威；独立重建全部报告不在契约内。`app/control/lifecycle.py`、`app/control/projections.py`、`app/graph/checkpoint_compaction.py`；`tests/test_terminal_finalizer.py`、`tests/test_projections.py`、`tests/test_checkpoint_compaction.py` |

<a id="cross-cutting-implementation-boundaries"></a>

## 跨切面实现边界

以下边界横跨多个阶段图。表格标识实现所有者与验证面，链接的参考文档拥有精确行为。

| 边界 | 当前实现 | 代码与测试证据 | 契约所有者 |
|---|---|---|---|
| 公共结构化运行预检与整次冻结 | 结构化/TUI 校验并冻结字面 occurrence；固定名单由注册表默认推导，Assembly 可省略而延迟选择。发布前从未压缩/gzip 输入探测一个逻辑字节，拒绝空或不可读成员；bzip2/xz 在端到端执行支持前拒绝预检。 | `app/entrypoints/run.py`、`app/entrypoints/tui.py`、`app/schemas/manifest.py`、`app/storage/frozen_inputs.py`；`tests/test_run_command.py`、`tests/test_frozen_inputs.py`、`tests/test_manifest.py` | [运行时](topics/runtime-orchestration.md)、[数据](reference/data-contract.md) |
| 附着与分离进度 | 一个纯 reducer 仅消费冻结计划、规范 occurrence 终态和匹配门禁；附着在结算后调用，分离检查调用同一函数。游标、轨迹尾顺序、skip 原因不作进度输入。分离可恢复性还要求已验证冻结输入与当前会话可读 checkpoint 行，不能只看 SQLite 文件存在。 | `app/control/projections.py`、`app/control/lifecycle.py`、`app/graph/builder.py`；`tests/test_projections.py`、`tests/test_graph_routing.py`、`tests/test_run_command.py`、`tests/test_review_resume.py`、`tests/test_inspect_read_model.py` | [运行时](topics/runtime-orchestration.md)、[数据](reference/data-contract.md) |
| 已验证冻结上下文与轨迹权威 | 生产 run/resume 校验一份冻结事务，将内存 `FrozenInputBundle` 注入 Inventory；验证和注册消费规范 manifest、RunConfig、声明、路径。Trace 从新验证 bundle 导出步骤，否则仅使用带 occurrence 的 Ledger/产物事实。 | `app/entrypoints/run.py`、`app/graph/builder.py`、`app/stages/inventory/nodes.py`、`app/reports/trace_writer.py`；`tests/test_frozen_inputs.py`、`tests/test_run_trace.py`、`tests/test_resource_resolution.py` | [运行时](topics/runtime-orchestration.md)、[数据](reference/data-contract.md) |
| Occurrence、方法、尝试事实完整性 | 显式 occurrence 结算，阶段终态追加单例；尝试终态在 Ledger 追加锁内互斥，trace 保守投影规范终态并显露冲突。共享生命周期/规划结算为有效方法与 occurrence 记录一个类型化处置；延迟 Assembly 先验证 `primary_method == assembly_selection.primary_method`。 | `app/execution/lifecycle.py`、`app/execution/method_outcomes.py`、`app/control/occurrence_settlement.py`、`app/storage/ledger.py`、`app/graph/builder.py`、`app/reports/trace_writer.py`；`tests/test_execution_lifecycle.py`、`tests/test_terminal_finalizer.py`、`tests/test_ledger_v2.py`、`tests/test_graph_routing.py`、`tests/test_run_trace.py`、`tests/test_read_qc.py`、`tests/test_genome_survey.py` | [生命周期](reference/execution-lifecycle.md)、[数据](reference/data-contract.md) |
| 本地进程分发 | `LocalRunner` 以 argv 调用冻结解释器/脚本，可选 `conda run`，始终 `shell=False`；两条路径都拒绝非空 `env_setup` shell 片段。 | `app/execution/local_runner.py`；`tests/test_execution_runner.py`、`tests/test_env_resolver.py` | [执行](topics/execution.md)、[生命周期](reference/execution-lifecycle.md) |
| 交互 Inventory-only 规划 | Ask/TUI 区分未回答 `None` 和显式空列表；纯文本、questionary、LLM 草稿均接受 `[]`，跳过生物学规划问题，显示 `Inventory only`，冻结 `requested_stages: []`。 | `app/schemas/ask_plan.py`、`app/entrypoints/ask_interactive.py`、`app/entrypoints/tui.py`；`tests/test_ask_plan.py`、`tests/test_ask_interactive.py`、`tests/test_tui.py` | [TUI](topics/tui-and-planning.md)、[运行时](topics/runtime-orchestration.md) |
| 当前候选连续性与不可变交付 | 每个已实现组装生产者声明 occurrence 交付。共享结算在追加 `resource_delivered` 前验证候选/主产物所有权；Review 要求冻结生产者终态、唯一交付、当前投影、解析候选包一致。复用候选必须显式 reused 交付。 | `app/control/workflow_plan.py`、`app/control/occurrence_settlement.py`、`app/graph/builder.py`、`app/storage/ledger.py`、`app/storage/assembly_candidates.py`、`app/stages/{inventory,assembly,assembly_dedup,assembly_review,hic_scaffolding,gapcloser,t2t,base_polish}/nodes.py`；`tests/test_inventory.py`、`tests/test_assembly.py`、`tests/test_assembly_dedup.py`、`tests/test_assembly_review.py`、`tests/test_hic_scaffolding.py`、`tests/test_gapcloser.py`、`tests/test_t2t.py`、`tests/test_base_polish.py` | [资源](reference/resource-handoff.md)、[阶段](reference/stage-contracts.md) |
| Read QC 闭合证据包 | SeqKit 对每个输入规模发布一份直接全读段集汇总，拥有长度/质量指标；逐文件行仅交叉检查。汇总/摘要来源引用真实输入/汇总；注册表冻结来源。零保留过滤保留证据但失败，不替换 `long_reads`。 | `app/stages/read_qc/method_application/support.py`、`seqkit_stats.py`、`seqkit_length_filter.py`、`app/stages/read_qc/nodes.py`；`tests/test_read_qc.py`、`tests/test_frozen_inputs.py` | [阶段](reference/stage-contracts.md)、[资源](reference/resource-handoff.md) |
| 阶段资源解析 | 适配器经 `resource_resolution.py` 查询规范 `ResourceSpec`，无独立规划预检 API 或重复需求表。确认运行前只读检查固定闭包；延迟 Assembly 从冻结快照解析选择，并在构建尝试前阶段预检。 | `app/execution/run_readiness.py`、`app/execution/env_resolver.py`、`app/stages/resource_specs.py`、`app/stages/resource_resolution.py`；`tests/test_run_readiness.py`、`tests/test_project_scaffold.py`、阶段测试 | [资源](reference/resource-handoff.md)、[Bootstrap](topics/bootstrap.md) |
| Genome Survey 逻辑输入 | 仅暴露 `short_reads`、`long_reads`，优先短读段族；clean/raw 优先级归资源规格。只有明确基因组大小支持文档 skip 路线时可无读段。 | `app/stages/genome_survey/nodes.py`、`state.py`、`app/stages/resource_specs.py`；`tests/test_genome_survey.py`、`tests/test_genome_size.py` | [阶段](reference/stage-contracts.md) |
| 可执行产物依赖 | Jellyfish/GenomeScope2 以已索引 `ArtifactRefJson` 交接 k-mer 产物，消费者无路径字符串升级分支。 | `app/stages/genome_survey/method_application/jellyfish.py`、`genomescope2.py`；`tests/test_genome_survey.py` | [数据](reference/data-contract.md) |
| 描述符加载 | 注册表、包、方法、规划器、IO、条件需求、资源提示 YAML 全部拒绝未声明键；注册表是唯一方法清单，加载类型化记录。 | `app/schemas/descriptors.py`；`tests/test_configuration.py` | [描述符](reference/descriptors.md) |
| 外部案例消费 | 唯一 v5 派生紧凑交换、实时目录、共享材料查询；跨方法证据进入方法选择与仅目标参数建议。无运行级案例副本或旧消费者后备，Hi-C/T2T 待扩展。 | `app/knowledge/assembly_cases.py`、`assembly_retrieval.py`、`assembly_runtime.py`、`v5_projection.py`；`tests/test_assembly_case_consumption.py`；[软件覆盖缺口](reference/conformance.md#assembly-software-explanation-coverage) | [案例](reference/casepack.md) |
| 注册执行类别 | `local` 方法绑定导入的生命周期/图对象；`planner_only` 的真实路线证明无进程。 | `tests/test_registry_executable_invariant.py`、`descriptors/registry.yaml` | [描述符](reference/descriptors.md) |
| AutoHiC 输出收集 | 进程成功后、首次新发布前，要求四项冻结产品或匹配尝试的索引恢复记录。缺产物收集失败；后续发布失败可留下无成功语义事件的索引孤儿。 | `app/stages/hic_scaffolding/method_application/support.py`；`tests/test_hic_scaffolding.py` | [AutoHiC](topics/autohic.md)、[阶段](reference/stage-contracts.md) |

<a id="stage-node-inventory"></a>

## 阶段节点清单

以下对具体节点组分类。嵌套方法在方法清单展开，不以一个阶段颜色隐藏细节。

<a id="inventory-and-input-preparation"></a>

### Inventory 与输入准备

| 阶段节点/能力组 | 接线 | 实现 | 验证 | 欠账 | 证据 |
|---|---|---|---|---|---|
| Inventory `enter -> validate_inputs -> register_assets -> commit` | `wired` | `implemented-no-process` | `tested` | `none` | 冻结输入注入和注册候选包装保留压缩内容逻辑非空；已有组装在结算声明不可变复用交付。`app/stages/inventory/builder.py`、`nodes.py`；`tests/test_inventory.py`、`tests/test_frozen_inputs.py`、`tests/test_resource_resolution.py`、`tests/test_manifest.py` |
| Inventory `review_gate`、`fail` | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/inventory/builder.py`；`tests/test_inventory.py`、`tests/test_review_resume.py` |
| Read QC `enter`、`resolve_raw_long_reads`、规划、`commit`、`fail` | `wired` | `implemented-no-process` | `tested` | `none` | 冻结注册表来源解析直接资源；过滤成功发布 read-set manifest，零保留保持证据并失败，不替换 `long_reads`。`app/stages/read_qc/builder.py`、`nodes.py`；`tests/test_read_qc.py` |
| Read QC `seqkit_stats` | `wired` | `executable` | `tested` | `none` | 直接输入流汇总拥有全读段长度/N50/质量，逐文件行仅交叉检查，来源跟随真实输入。`app/stages/read_qc/method_application/support.py`、`seqkit_stats.py`；`tests/test_read_qc.py` |
| Read QC `seqkit_length_filter` 及有意 skip | `wired` | `executable` 与 `implemented-no-process` skip | `tested` | `none` | 过滤发布 read-set manifest 与直接过滤后汇总；零保留无下游交接；规划 skip 为无进程 raw 透传。`app/stages/read_qc/method_application/seqkit_length_filter.py`、`app/stages/read_qc/nodes.py`；`tests/test_read_qc.py` |
| Genome Survey 输入解析、`select_survey_mode` | `wired` | `implemented-no-process` | `tested` | `none` | 阶段拥有 short/long 解析与明确大小 skip；`app/stages/genome_survey/nodes.py`、`app/stages/resource_specs.py`；`tests/test_genome_survey.py`、`tests/test_genome_size.py` |
| Genome Survey `jellyfish -> genomescope2` | `wired` | `executable` | `tested` | `none` | 已索引 k-mer `ArtifactRefJson` 交接，无字符串兼容；`app/stages/genome_survey/method_application/`；`tests/test_genome_survey.py` |
| Genome Survey `write_full_survey_result`、明确先验 skip、`commit`、`fail` | `wired` | `implemented-no-process` | `tested` | `none` | provided-only 持久化完成结果；选定方法的类型化持久处置独立于 occurrence 终态。`app/stages/genome_survey/builder.py`、`nodes.py`；`tests/test_genome_survey.py`、`tests/test_genome_size.py` |

<a id="assembly-production-review-and-chromosome-finishing"></a>

### Assembly 生产、评审和染色体精修

| 阶段节点/能力组 | 接线 | 实现 | 验证 | 欠账 | 证据 |
|---|---|---|---|---|---|
| Assembly 输入/profile：`resolve_extended_inputs -> infer_input_profile -> select_route` | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/assembly/builder.py`；`tests/test_assembly.py`、`tests/test_multiplatform_long_reads.py` |
| Assembly 选择：`build_assembly_candidate_set -> select_primary` | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/assembly/nodes.py`；`tests/test_assembly.py`、`tests/test_assembly_candidate_bundle.py` |
| Assembly `recommend_parameters` 与必需审批阻塞 | `wired` | `implemented-no-process` 模型/证据流程 | `tested` | `known-gap` | schema-3 已实现，方法知识深度不均；`tests/test_parameter_recommendation_v3_contract.py`、`tests/test_phase11_parameter_approval.py` |
| Assembly `flye`、`nextdenovo`、`hifiasm`、`hifiasm_ont` | `wired` | `executable` | `tested` | 见方法条目 | `app/stages/assembly/method_application/`；`tests/test_assembly.py` |
| Assembly `execution_retry_gate`、`commit`、`fail`、`block` | `wired` | `implemented-no-process` | `tested` | `none` | 完成需 executed candidate 并声明物化交付；仅规划/非本地不能推进，拒绝/失败显式结算。`app/stages/assembly/parameters/workflow.py`、`app/stages/assembly/nodes.py`；`tests/test_assembly.py`、`tests/test_phase11_parameter_approval.py`、`tests/test_review_resume.py` |
| Dedup 基线或 `QUAST -> recommend -> review -> purge_dups -> QUAST` 循环 | `wired` | `executable` | `tested` | `known-gap` | 本地执行加明确 purge 授权可物化交付；缺批准/授权则透传完成，purge 处置 skipped 并显式 reused 交付；无物化候选则 not_started/失败且无交付。顾问循环真实工具验收仍缺。`app/stages/assembly_dedup/`；`tests/test_assembly_dedup.py`、`tests/test_dedup_recommendation_loop.py` |
| Assembly Review 可复用子图、候选解析、`quast -> busco` | `wired` | `executable` | `tested` | `none` | 评估前要求冻结生产者完成终态、唯一交付、当前引用、索引候选/主输出一致。`app/stages/assembly_review/`、`app/storage/assembly_candidates.py`；`tests/test_assembly_review.py`、`tests/test_assembly_candidate_bundle.py` |
| Assembly Review 规则、参数质量、近缘物种、比较节点 | `wired` | `implemented-no-process` | `tested` | `none` | 参数质量诊断留在当前 Review，不写 Assembly 重试游标/参数证据，继续近缘物种/人工评审。`app/stages/assembly_review/nodes.py`；`tests/test_phase10_parameter_catalog.py`、`tests/test_related_species_evidence.py`、`tests/test_related_species_statistics.py`、`tests/test_assembly_review_statistics_integration.py` |
| Assembly Review `human_gate` 与采纳/恢复事务 | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/assembly_review/builder.py`、`app/control/resume_transaction.py`；`tests/test_review_resume.py` |
| Hi-C `resolve_hic_inputs` 与需求边界 | `wired` | `implemented-no-process` | `tested` | `none` | `app/stages/hic_scaffolding/`；`tests/test_hic_scaffolding.py` |
| Hi-C `recommend_autohic_parameters` 与审批/重试 | `wired` | `implemented-no-process` 模型/证据流程 | `tested` | `none` | `app/stages/hic_scaffolding/parameters/`；`tests/test_hic_scaffolding.py`、`tests/test_parameter_resolution_validation.py` |
| Hi-C `autohic` 方法与收集 | `wired` | `executable` | `tested` | `none` | 真实 Runner 和 FASTA/.hic/.assembly/HTML 严格校验，含回放与后期发布失败覆盖。`app/stages/hic_scaffolding/method_application/`；`tests/test_hic_scaffolding.py` |
| Hi-C `chromosome_assembly_review`、阻塞、采纳恢复 | `wired` | `implemented-no-process` | `tested` | `none` | 候选/当前资源替换声明 materialized 或 reused 交付，共享结算对照索引产物验证。`app/stages/hic_scaffolding/builder.py`、`nodes.py`；`tests/test_hic_scaffolding.py`、`tests/test_assembly_candidate_bundle.py`、`tests/test_review_resume.py` |
| GapCloser HiFi 执行与采纳 | `wired` | `executable` | `tested` | `known-gap` | TGS-GapCloser 无校正路线、原生坐标校验、持久人工采纳、materialized/reused 交付；代表样本验收待完成。`app/stages/gapcloser/`；`tests/test_gapcloser.py` |
| T2T 输入/基序：`resolve_inputs -> resolve_telomere_motif` | `wired` | `implemented-no-process`，条件模型调用 | `tested` | `none` | `app/stages/t2t/parameters/workflow.py`；`tests/test_t2t_motif.py` |
| T2T 单轮 schema-3 基线/旧记录、策略审批、`prepare_parameters` | `wired` | `implemented-no-process` 模型/证据流程 | `tested` | `none` | `app/stages/t2t/parameters/`；`tests/test_t2t.py`、`tests/test_parameter_recommendation_v3_contract.py` |
| T2T 扫描与 minimap2 比对 | `wired` | `executable` | `tested` | `none` | `scan_assembly`、`prepare_reads`、`scan_reads`、`alignment`；`app/stages/t2t/nodes.py`；`tests/test_t2t.py` |
| T2T 补丁、验证、报告、采纳门、commit/fail | `wired` | `implemented-no-process` | `tested` | `none` | 候选与当前资源替换声明 materialized/reused 交付，共享结算验证索引产物。`apply_patches`、`validate_patched`、`report`、`gate_review`；`app/stages/t2t/nodes.py`；`tests/test_t2t.py`、`tests/test_assembly_candidate_bundle.py`、`tests/test_review_resume.py` |
| Base Polish | `wired` | `executable` | `tested` | `known-gap` | HiFi Racon、成对 minimap2 证据、人工交接；`tests/test_base_polish.py` |

<a id="registered-method-inventory"></a>

## 注册方法清单

`descriptors/registry.yaml` 当前包含 20 个注册方法：19 个 `local` 和 1 个 `planner_only`。可执行不变量将每个本地 ID 绑定到导入生命周期、嵌套方法图和外层阶段节点。唯一 `planner_only` 是有意无进程选择，真实规划到阶段路线有测试。注册表当前**没有仅描述符占位方法**。注册表和每层嵌套 YAML 都严格加载。

下面适配器/生命周期和收集列只描述进程接线，不覆盖上方跨切面证据与结算边界。

| 注册方法 | 描述符 | 适配器/生命周期 | 收集 | 验证 | 限制或欠账 |
|---|---|---|---|---|---|
| `read_qc.long_read_stats.seqkit_stats` | 已注册 | `executable` | 已实现 | `tests/test_read_qc.py` | 直接全读段汇总、逐文件一致性检查、类型化终态处置 |
| `read_qc.length_filter.seqkit_seq` | 已注册 | `executable` | 已实现 | `tests/test_read_qc.py` | 零保留使 occurrence 失败且无交接；需批准 `min_len`；执行结算类型化结果 |
| `read_qc.length_filter.skip` | 已注册 | `implemented-no-process`（`planner_only`） | 不适用 | `tests/test_read_qc.py`、`tests/test_ask_plan.py` | 有意 raw 透传，完成无进程处置 |
| `genome_survey.kmer_count.jellyfish` | 已注册 | `executable` | 已实现 | `tests/test_genome_survey.py` | 类型化 k-mer 产物发布 |
| `genome_survey.model.genomescope2` | 已注册 | `executable` | 已实现 | `tests/test_genome_survey.py` | 类型化 k-mer 依赖 |
| `assembly.candidate.flye` | 已注册且有目录 | `executable` | 已实现 | `tests/test_assembly.py`、`tests/test_configuration.py`、`tests/test_assembly_case_consumption.py` | 使用共享实时案例消费者；真实检索覆盖与推荐收益尚需测量 |
| `assembly.candidate.hifiasm` | 已注册且有目录 | `executable` | 已实现 | `tests/test_assembly.py`、`tests/test_hifiasm_evidence_case.py` | 目录和机制指导已提供；案例数量不代表已测推荐收益 |
| `assembly.candidate.hifiasm_ont` | 已注册且有目录 | `executable` | 已实现 | `tests/test_assembly.py`、`tests/test_hifiasm_numeric_retrieval.py` | 共享消费者；需区分输入模式与状态，不能将 HiFi 案例默认视为 ONT 匹配 |
| `assembly.candidate.nextdenovo` | 已注册且有目录 | `executable` | 已实现 | `tests/test_assembly.py`、`tests/test_assembly_case_consumption.py` | 共享库可消费原生 NextDenovo 案例与设置；真实覆盖和生物学收益尚需测量 |
| `assembly_dedup.purge_dups` | 已注册 | `executable` | 已实现 | `tests/test_assembly_dedup.py` | 持久 completed/skipped/not_started 处置独立于 occurrence 和交付 |
| `assembly_dedup.eval.quast` | 已注册 | `executable` | 已实现 | `tests/test_dedup_recommendation_loop.py` | 顾问模式的来源/候选比较；共享生命周期，处置延迟至循环结算 |
| `assembly_review.eval.quast` | 已注册 | `executable` | 已实现 | `tests/test_assembly_review.py` | 可复用 Review 评估器，包括请求时最终成对 Review；不等于独立 QV |
| `assembly_review.eval.busco` | 已注册 | `executable` | 已实现 | `tests/test_assembly_review.py`、`tests/test_database_resolution.py` | manifest NCBI 学名选择兼容已装 lineage；RunConfig lineage 仅断言，路径/版本/来源取冻结主机注册表 |
| `hic_scaffolding.candidate.autohic` | 已注册且有目录 | `executable` | 已实现 | `tests/test_hic_scaffolding.py` | 收集必须有全部四项评审产品 |
| `gapcloser.tgs_gapcloser` | 已注册 | `executable` | 已实现 | `tests/test_gapcloser.py` | 仅 HiFi，人工采纳；坐标一致性不是 QV |
| `base_polish.racon` | 已注册 | `executable` | 已实现 | `tests/test_base_polish.py` | 仅 HiFi；成对读段一致性不是独立 QV；采纳或保留源 |
| `t2t.alignment.minimap2` | 已注册且有目录 | `executable` | 已实现 | `tests/test_t2t.py` | 无额外记录限制 |

`tests/test_registry_executable_invariant.py` 从导入 `MethodExecutionLifecycle` 绑定导出本地 ID，证明覆盖每个本地方法，并验证共享 freeze/run/collect 与外层路线。从生产规划目录导出 planner-only 集合，沿 `read_qc.length_filter.skip` 到 raw 提交，证明不创建过滤冻结尝试、Runner 调用或方法尝试事件。两集合互斥，并集等于完整注册表。

<a id="parameter-capability-map"></a>


| `gapcloser.eval.quast` | local | `app/stages/finishing/advisor.py:quast_lifecycle` | 源与全部尝试同设置 QUAST，持久表与执行引用 |
| `t2t.eval.quast` | local | `app/stages/finishing/advisor.py:quast_lifecycle` | 同上；阈值与候选支持证据反馈 |
| `base_polish.eval.quast` | local | `app/stages/finishing/advisor.py:quast_lifecycle` | 同上；Racon 前后证据反馈 |

## 参数能力映射

参数能力不是布尔值。目录、推荐、审批、冻结绑定、重试和生物学知识覆盖是独立事实。

| 范围 | 静态目录 | 推荐与不可变证据 | 人工审批 | 冻结尝试绑定 | 重试 | 当前限制 |
|---|---|---|---|---|---|---|
| Assembly：Flye | 已实现 | 已实现，共享案例消费者 | 顾问模式必需 | 已实现 | 已实现 | 可消费原生/跨方法案例；实际覆盖和收益未测 |
| Assembly：Hifiasm | 已实现 | 已实现，共享案例消费者 | 顾问模式必需 | 已实现 | 已实现 | 机制指导较丰富，但不能代替代表样本验收 |
| Assembly：Hifiasm-ONT | 已实现 | 已实现，共享案例消费者 | 顾问模式必需 | 已实现 | 已实现 | 必须保持 ONT 模式、读段状态与案例绑定差异 |
| Assembly：NextDenovo | 已实现 | 已实现，共享案例消费者 | 顾问模式必需 | 已实现 | 已实现 | 有原生来源设置；不能机械翻译外部工具参数 |
| AutoHiC | 已实现 | 已实现 | 顾问模式必需 | 已实现 | 已实现 | 缺声明产品时收集拒绝；终态收集失败无自动重试 |
| T2T/minimap2 阈值 | 已实现 | 基序、源端部测量、过滤前分布/拒绝原因及全部候选支持数 | 逐动作人工批准 | 独立尝试与冻结输入 | 有界顾问循环；另有初始 schema-3 基线 | 实际补丁/保留源；无独立 QV |
| GapCloser | 已实现 | 缺口与 QUAST/填补证据顾问 | 逐动作人工批准 | 每轮独立冻结 | max_attempts 有界参数尝试 | HiFi-only TGS-GapCloser；另有单轮基线 |
| Base Polish | 已实现 | QUAST、成对 PAF/FASTA/版本证据顾问 | 逐动作人工批准 | 每轮从原组装独立冻结 | max_attempts 有界配置尝试 | 每候选一轮 HiFi Racon；无独立 QV |
| Assembly Dedup | purge 策略、预设、`end_only` 有界配置 | 已实现结构化顾问与累积 QUAST 证据；另有确定性基线 | `dedup_recommendation`，每轮提案或既有候选选择均需人工决策 | 每次尝试独立冻结并从原始草稿执行 | `max_attempts` 有界循环 | 独立任务契约，非 schema-3 L2；真实工具验收待完成 |
| Read QC、Survey、Review | 注册表默认方法与静态/操作者参数 | 无 schema-3 参数推荐；Review 可另有决策推荐 | 按各阶段门禁 | 可执行方法使用共享尝试冻结 | 按阶段契约 | 默认方法内部拥有，不暴露为用户方法选择字段 |
| Inventory | 不需要 | 不需要 | 仅输入歧义评审 | 不适用 | 不适用 | 不应推断存在推荐器 |

Assembly 路线选择也使用共享 StructuredRecommender 调用/解析边界，仍保留 schema-v1 路线记录。Assembly、AutoHiC 及 T2T 单轮消费者的 schema-3 链已实现；三者显式确定性基线以策略记录批准空 L2 初始尝试，T2T 要求已提供基序并禁用阈值/基序模型，详见[推荐策略](reference/recommendation-policy.md)。Dedup 和三个 finishing 顾问使用独立类型化任务输出，逐动作人工批准并保留全部尝试。默认 finishing 的持久任务契约不使用 schema-3 参数 manifest；T2T 的 schema-3 单轮执行/手工补丁路径通过 deterministic_baseline 保留，缺少模式的新调用不进入旧 LLM 路径，保留既有响应恢复与已批准执行消费者。剩余缺口是独立准确率证据、方法范围与代表样本验证。

<a id="assembly-dedup-validation-map"></a>

## Assembly / Dedup 验证边界

各方法共享冻结输入、方法确认、参数审批、执行生命周期和候选交接。`tests/test_assembly_dedup_parent_recovery.py` 从默认 `assembly_route: auto` 开始，覆盖人工方法确认、参数审批之间的 SQLite 关闭重开及 CLI 恢复；多文件含空格路径，工具只执行一次。fixture 验证不代表工具版本适配、组装质量或服务器分离进程验收。

| 方法 | 已接入输入模式 | 代码验证与知识覆盖 | 仍待真实验收 |
|---|---|---|---|
| hifiasm | PacBio HiFi；补充 ONT/Hi-C 须显式绑定 | 默认 auto 确认后恢复；完整多文件 HiFi 和显式单文件覆盖；既有机制指导 | 实际 primary/haplotype GFA、倍性、版本与补充输入 |
| hifiasm-ONT | ONT raw/HQ、CycloneSEQ 映射 `--ont` | 三类输入的父图恢复、命令模式、GFA/候选交接 | 读段质量/读长/覆盖与版本支持；CycloneSEQ 适用性须独立验证 |
| Flye | nano-raw、nano-hq、pacbio-raw、pacbio-hifi；CycloneSEQ 映射 nano-raw | 全模式多文件父图恢复；覆盖截取/overlap 指导引用官方文档并冻结 | 各模式真实工具与样本；corrected、meta 模式尚未接入 |
| NextDenovo | ont（含 ONT HQ/CycloneSEQ 映射）、clr、hifi | 各模式父图恢复、FOFN、原生 run.cfg/产物交接；固定 FASTA 格式、配置/FOFN 完整性校验、原生路径字符约束；cutoff 与 v2.5.2 HiFi 自纠错官方指导 | 实际纠错/组装目录、HiFi 行为、版本和失败恢复；corrected 输入尚未接入 |
| Dedup read_depth | Assembly 候选的原始读段；外部组装的当前注册长读段 | 两轮 SQLite/CLI 恢复；外部 FASTA ID 不当读段 ID，缺读段明确失败 | 各平台 preset、深度/cutoff 合理性；当前比对读段不能推断为外部组装原始来源 |
| Dedup self_alignment | 已物化组装 | 两轮恢复、无覆盖工具调用；采纳早先候选或保留源，旧候选哈希不变，最终选择不重跑 | 自比对/边界截取与过度去冗余证据；N50 不能替代完整性和准确性 |

contig 模板显式启用 Dedup advisor/json_schema/三次尝试上限；审核策略沿用既有配置。三轮循环、响应回放、预算/重复配置、源候选保护仍由 `tests/test_dedup_recommendation_loop.py` 覆盖。独立 QUAST/BUSCO、生物学质量及推荐收益计为待验，不能由 fixture 关闭。

新菌种的 PacBio 必须先确定 HiFi/CLR，再复用规范 manifest、contig RunConfig 和 `gene-agent run`/`resume`。Illumina DNA 已进入 Read QC/Genome Survey，短读段存在时优先用于 k-mer 调查；没有新增短读段组装或 polish。Hi-C 可提供推荐上下文，仅明确批准的 hifiasm phasing 或独立 AutoHiC 阶段实际消费读对。ONT/CycloneSEQ 没有对应输入时单列待验，不能由 PacBio 菌种代验。模板中的大小、倍性、学名均是示例，执行时按材料替换。

GapCloser、T2T、Base Polish 的已报告 smoke 与阶段/串联验收欠账由[一致性记录](reference/conformance.md)维护。代表性样本验收须保留输入身份、工具版本、运行目录、实际命令、评估和采纳记录。

<a id="finishing-capabilities-and-remaining-product-gaps"></a>

## 精修能力与剩余产品缺口

可执行精修路线有明确输入限制，仍需代表性服务器验收。`placeholder` 表示没有执行闭环，不能据此声称生物学能力已运行。

| 产品能力 | 接线 | 实现 | 验证 | 欠账 | 证据与所有者 |
|---|---|---|---|---|---|
| 染色体 scaffolding 后补缺口 | `wired` | `executable` | `tested` | `known-gap` | HiFi-only TGS-GapCloser；代表样本验收待完成；`tests/test_gapcloser.py`；[阶段](reference/stage-contracts.md#8-gapcloser) |
| 自动碱基抛光 | `wired` | `executable` | `tested` | `known-gap` | 仅 HiFi，代表样本验收待完成；`tests/test_base_polish.py`；[阶段](reference/stage-contracts.md#10-base-polish) |
| Base Polish 后最终 Review | `wired` | `executable` | `tested` | `known-gap` | 源/抛光候选成对 QUAST/BUSCO；[阶段](reference/stage-contracts.md#10-base-polish) |
| clean 读段/已有组装的 TUI 中途入口 | 公共 TUI 中 `unwired` | 已接受设计 `placeholder` | `partially-tested` | `known-gap` | [TUI 中途入口](topics/tui-midstage-entry.md) |

最终 Review 是可复用 `assembly_review` 的第二次 occurrence，不是独立“最终 QA”阶段。保留源组装合法。操作者提供的抛光组装不能证明方法已执行。维护规则由[架构治理](reference/governance.md)拥有。
