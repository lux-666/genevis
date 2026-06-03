# 07-03 注释 review chain

任务状态：规划中；依赖 Phase 7 task 01、task 02，以及 Phase 6 task 06。

## 目标

建立注释 agent 所需的完整 review gate 链。该链不重新决定整条下游计划，而是从 Phase 6 task 06 在 `assembly_review` continue-downstream 分支产出的 `annotation.intake_plan` 开始执行。后续每个 gate 只负责自己的局部阶段决策，并复用 task 01 的通用 gate/resume/recommender/trace 机制。

先在 planning-only 方法之上搭出 chain skeleton，使 gate 顺序、wait/trigger 行为、recommendation、durable resume 和 run trace 能先被验证；真实执行 runner 再由 task 04-06 逐步填入。

## 当前状态（已验证）

- `annotation.intake_plan` artifact 已由 `app/review/annotation_intake.py` 实现，写入 `assembly_review/<session_id>/annotation_intake_plan_<ts>.json`，记录 `recommended_plan`、`final_plan`、`final_requested_stages`、`changed` 和 `source` 字段；`assembly_review` stage output 中的 `annotation_intake_plan_ref` 指向该文件。
- `app/review/gates.py` 中的 `ReviewGateDeclaration` registry 已经通用化：任意 stage 均可通过 `register_review_gate(...)` 声明 `decisions_allowed`、`decision_applier`、`prompt_bundle` 等静态行为；`resume_review_state` 已改为 registry 分发，未注册 gate 被拒绝。
- `app/stages/gene_annotation/nodes.py` 目前为 planning-only 实现：`plan_gene_methods` 按 evidence profile 列出 `candidate_methods` 和 `helper_methods`，`plan_evidence_merge_or_review` 写出 `gene_annotation_plan.json` 并记录 `review_triggers`（`evidence_merge_policy_required`、`rna_protein_evidence_conflict_review` 等），`commit_gene_annotation` 写出 `gene_annotation_bundle.json`。stage_output 中尚无 `candidate_ref` 字段，也无任何 gate 注册。
- `STAGE_ORDER` 为 `inventory → read_qc → genome_survey → assembly → assembly_review → hic_scaffolding → repeat_annotation → gene_annotation → function_annotation`；`gene_annotation` 是单一 stage，merge 操作在其 commit path 内进行，不新增独立 stage。

## 与 Phase 6 task 06 的耦合

Phase 6 task 06 是 annotation agent 的 intake boundary：

- `assembly_review` 的 continue-downstream 分支生成或确认计划 2；
- 计划 2 记录为 `annotation.intake_plan` artifact；
- 该 artifact 记录推荐计划、最终选择、是否被编辑、下游 `requested_stages` suffix、方法和参数；
- checkpoint update 只改写 `assembly_review` 之后的 suffix，并保持上游/已执行阶段不变。

本任务消费这个 intake plan：

- 不再从 `assembly_review` 裸跳到 annotation chain；
- 不重新询问整条下游阶段列表；
- 只在具体 gate 中做局部 decision，例如选择 gene method、确认 candidate evidence、确认 final gene set；
- 如后续 gate 需要改变未来阶段，必须复用同一套 suffix-rewrite helper，并记录新的局部 plan-change artifact。

## 目标链

```
assembly_review
  ↓
annotation intake plan              [Phase 6 task 06]
  ↓
hic_scaffolding                     [已有 gate/resume；由 task 01 纳入通用机制]
  ↓
repeat_annotation                   [task 02]
  ↓
mask review                         [task 02]
  ↓
gene-method planning review         [new — 本任务]
  ↓
run gene method(s)                  [planning-only first; real runners in task 04]
  ↓
candidate-evidence review           [new — 本任务]
  ↓
merge / EVM-weight review           [new — 本任务；在 gene_annotation commit path 内]
  ↓
final gene-set review + BUSCO       [new — 本任务]
  ↓
function_annotation                 [task 06 execution slices later]
```

## 为什么先做 planning-only chain

Gate 只需要 `ReviewPayload`（candidates、signals、decisions_allowed、trigger）即可工作；底层方法不必已经能真实执行。先在今天的 planning-only gene/function 阶段上搭出链，可以在投入 BRAKER3/MAKER/AUGUSTUS/Miniprot/GeMoMa/InterProScan/DIAMOND 等 runner 之前，验证：

- gate 顺序是否正确；
- wait 0 / wait ∞ / wait N / trigger 的行为是否一致；
- durable resume 是否能跨多个 annotation gates 工作；
- run trace 是否能清楚展示每个 gate 的请求、推荐、决策和决策者；
- annotation intake plan 是否能稳定驱动后续阶段，而不是被后续 gate 隐式重算。

## 范围

- 将下列新 gate 声明到 task 01 的通用机制中：
  - `mask_review`（由 task 02 提供 payload 和 artifact）；
  - `gene_method_planning_review`；
  - `candidate_evidence_review`；
  - `merge_review`（命名与 task 05 对齐，挂载在 `gene_annotation` commit path 内）；
  - `final_gene_set_review`。
- 每个 gate 定义 `decisions_allowed`、recommendation、`auto_decidable` trigger、默认 wait policy 和 decision applier。
- 将 `docs/stage_contracts.md` 中 `gene_annotation` 的 review triggers 映射成真实 trigger 逻辑：
  - RNA 和同源证据冲突；
  - 预测基因数量远超预期范围；
  - EVM 失败或丢失主要证据类别。
- 让 chain 在 planning-only 方法上端到端可运行，并且每个 gate 都通过 durable checkpoint/resume 继续。
- 在 run trace（Phase 6 task 04）中展示 annotation intake plan 以及每个 annotation gate 的 request、recommendation、decision、decided_by 和 downstream effect。

## 非目标

- 不实现真实 gene/function execution；这些属于 task 04-06。
- 不在本任务中实现 RepeatMasker；那是 task 02。
- 不把每个 gate 都升级成全局 replan 入口。默认只允许局部阶段决策；全局 downstream suffix 改写必须通过 Phase 6 task 06 定义的 helper 和 artifact 形态。
- 不把 interpretation agent 塞进 `function_annotation`；agent 3 仍是 task 07 的未来能力。

## 已决事项

以下问题已在设计阶段确定，不再开放：

1. **merge 的挂载位置**：merge 是 `gene_annotation` commit path 内的一个 gate，不是独立 stage。`STAGE_ORDER` 保持不变，`merge_review` 通过 `gene_annotation/merge_review` 注册到 gate registry，由 `gene_annotation` 的 node graph 在候选方法运行后触发。

2. **candidate gene models 的表示方式**：每个方法运行后，将输出 GFF3/GTF 路径以 `candidate_ref` 字段写入 `gene_annotation` stage_output，格式为 `candidate_refs: {method_id: gff3_path}`。`candidate_evidence_review` 的 `ReviewPayload.candidates` 列表直接引用这些路径，每个 candidate 包含 `method_id`、`candidate_ref`、`gene_count`（planning-only 时为 0）和 `conflict_signals`。

3. **BUSCO 的角色**：BUSCO 是 `final_gene_set_review` 的信号字段（`ReviewSignal.signal_name = "busco_completeness"`），不是独立 step 或独立 stage。planning-only 模式下 BUSCO signal status 为 `"missing"`，真实 runner 填入后变为 `"available"`。gate 的 `auto_decidable` 在 BUSCO signal 缺失时仍可为 `True`（以 planning-only 专用 trigger 触发），由 policy engine 决定是否 block。

4. **后续 gate 的 suffix-rewrite 权限**：仅 `gene_method_planning_review` 有权改变未来方法选择（在任何 runner 启动前）；`candidate_evidence_review`、`merge_review`、`final_gene_set_review` 只能对当前阶段做 accept / reject / stop 决策，不允许改写 `requested_stages` suffix。如需跨阶段变更，必须走 Phase 6 task 06 定义的 `rewrite_requested_stages_after_assembly_review` helper 并写 plan-change artifact。

## 数据一致性要求

- `annotation.intake_plan` 是 annotation chain 的起点和计划来源。
- 后续 gate 不得改写 `assembly_review` 或更上游阶段记录。
- 后续 gate 的局部选择必须写入对应 stage output 和独立 decision artifact。
- 如果某个后续 gate 改变未来阶段/方法/参数，必须写入 plan-change artifact，并在 ledger 中与普通 gate decision 区分。
- Planning-only gate 必须诚实标记 execution support，不能把未落地 runner 呈现为可执行。

## 设计方向

每个新 gate 遵循以下统一模式，与 task 01 已落地的 `assembly_review` 和 `hic_scaffolding` 对齐：

1. 在 `app/review/gates.py` 底部调用 `register_review_gate(ReviewGateDeclaration(...))` 声明静态行为。
2. 在对应 stage 的 `nodes.py` 中添加一个 `build_<gate_id>_payload(state)` 函数，返回 `ReviewPayload`，其中 `auto_decidable` 由 trigger 条件决定。
3. 在 node graph 中将 payload 写入 `active_interrupt`，触发 `supervision_state = "blocked_review"`。
4. 添加 `apply_<gate_id>_resume_decision(state, request)` 函数作为 `decision_applier`，根据 `request.decision` 更新 stage_output 并推进状态。
5. Decision applier 将选择结果写入 stage_output 的专用字段（如 `selected_methods`、`accepted_candidate_refs`、`merge_weights`），同时写 decision artifact 并调用 `Ledger.record_event`。

## 任务

### 1. intake_plan 验证辅助函数

- [ ] 在 `app/review/annotation_intake.py` 中添加 `load_annotation_intake_plan(state) -> dict`：从 `stage_outputs["assembly_review"]["annotation_intake_plan_ref"]` 读取并校验 JSON；若引用缺失或文件不存在，抛出带明确消息的 `ValueError`。
- [ ] 在 `app/stages/gene_annotation/nodes.py` 的 `enter_gene_annotation` 节点中调用该辅助函数；校验失败时返回 `_fail_update(state, "annotation.intake_plan missing or invalid")`，触发 `fail_gene_annotation` 路由。

### 2. 注册 `gene_method_planning_review` gate

- [ ] 在 `app/review/gates.py` 中注册 `ReviewGateDeclaration(stage="gene_annotation", gate_id="gene_method_planning_review", decisions_allowed=("select_methods", "accept", "stop"), decision_applier="app.stages.gene_annotation.nodes:apply_gene_method_planning_review_decision", template_preferred_decision="select_methods")`。
- [ ] 在 `nodes.py` 中添加 `build_gene_method_planning_review_payload(state) -> ReviewPayload`：`candidates` 列出每个候选方法的描述和 evidence profile 支持度；`auto_decidable=True` 当且仅当只有一个候选方法且无冲突证据；`signals` 包含 evidence profile signal。
- [ ] 在 `plan_gene_methods` 节点后插入 payload 构建逻辑，将 payload 写入 `active_interrupt`，设置 `supervision_state = "blocked_review"`。
- [ ] 实现 `apply_gene_method_planning_review_decision(state, request)`：`decision="select_methods"` 时将 `request.metadata["selected_methods"]` 写入 `gene_annotation` stage_output 的 `confirmed_methods` 字段，并调用 `rewrite_requested_stages_after_assembly_review` 更新 suffix（仅此 gate 有权调用）；`decision="accept"` 时保留 `plan_gene_methods` 原有列表；`decision="stop"` 时标记 `supervision_state = "failed_terminal"`。
- [ ] Decision applier 写 `gene_annotation.method_planning_decision` artifact，调用 `Ledger.record_event("gene_method_planning_decided", ...)`。

### 3. 注册 `candidate_evidence_review` gate

- [ ] 在 `app/review/gates.py` 中注册 `ReviewGateDeclaration(stage="gene_annotation", gate_id="candidate_evidence_review", decisions_allowed=("proceed_merge", "request_rerun", "stop"), decision_applier="app.stages.gene_annotation.nodes:apply_candidate_evidence_review_decision", template_preferred_decision="proceed_merge")`。
- [ ] 在 `nodes.py` 中添加 `build_candidate_evidence_review_payload(state) -> ReviewPayload`：`candidates` 列出每个方法的 `candidate_ref`（planning-only 时为预期路径）、`gene_count`（planning-only 时为 0）；`auto_decidable=True` 当且仅当无冲突 trigger；trigger 条件：`rna_protein_evidence_conflict_review` 或预测基因数偏差超阈值时 `auto_decidable=False`。
- [ ] 在 gene method runner（planning-only stub）完成后触发该 gate：写入 `active_interrupt`，设置 `supervision_state = "blocked_review"`。
- [ ] 实现 `apply_candidate_evidence_review_decision(state, request)`：`decision="proceed_merge"` 时将 `candidate_refs` 写入 stage_output 的 `accepted_candidate_refs` 字段；`decision="request_rerun"` 时清除已完成 runner 状态并回到 method planning；`decision="stop"` 时标记 `failed_terminal`。
- [ ] Decision applier 写 `gene_annotation.candidate_evidence_decision` artifact，调用 `Ledger.record_event`。

### 4. 注册 `merge_review` gate

- [ ] 在 `app/review/gates.py` 中注册 `ReviewGateDeclaration(stage="gene_annotation", gate_id="merge_review", decisions_allowed=("accept_weights", "adjust_weights", "stop"), decision_applier="app.stages.gene_annotation.nodes:apply_merge_review_decision", template_preferred_decision="accept_weights")`。
- [ ] 在 `nodes.py` 中添加 `build_merge_review_payload(state) -> ReviewPayload`：`candidate_comparison_context` 包含 EVM weight table（planning-only 时为占位结构）；`auto_decidable=True` 当且仅当无 `evidence_merge_policy_required` trigger 且无主要证据类别丢失；`signals` 包含各证据类别的存在状态。
- [ ] 在 merge/EVM 准备完成后（`plan_evidence_merge_or_review` 节点扩展处）触发该 gate：写入 `active_interrupt`，设置 `supervision_state = "blocked_review"`。
- [ ] 实现 `apply_merge_review_decision(state, request)`：`decision="accept_weights"` 时将默认 EVM 权重写入 stage_output 的 `merge_weights` 字段；`decision="adjust_weights"` 时从 `request.metadata["merge_weights"]` 读取调整后权重；`decision="stop"` 时标记 `failed_terminal`；不允许改写 `requested_stages`。
- [ ] Decision applier 写 `gene_annotation.merge_decision` artifact，调用 `Ledger.record_event`。

### 5. 注册 `final_gene_set_review` gate

- [ ] 在 `app/review/gates.py` 中注册 `ReviewGateDeclaration(stage="gene_annotation", gate_id="final_gene_set_review", decisions_allowed=("accept", "reject", "stop"), decision_applier="app.stages.gene_annotation.nodes:apply_final_gene_set_review_decision", template_preferred_decision="accept")`。
- [ ] 在 `nodes.py` 中添加 `build_final_gene_set_review_payload(state) -> ReviewPayload`：`signals` 包含 `ReviewSignal(signal_name="busco_completeness", status="missing")`（planning-only）或 `status="available"` 加 `metric_refs`（真实 runner）；`auto_decidable=True` 当 BUSCO signal 缺失（planning-only 专用 trigger）；`auto_decidable=False` 当 BUSCO completeness 低于阈值。
- [ ] 在 merge 完成后（`commit_gene_annotation` 前）触发该 gate：写入 `active_interrupt`，设置 `supervision_state = "blocked_review"`。
- [ ] 实现 `apply_final_gene_set_review_decision(state, request)`：`decision="accept"` 时标记 `gene_annotation` stage_output `status="committed"`，继续调用原 `commit_gene_annotation` 路径；`decision="reject"` 时清除 merge 结果并回到 candidate-evidence review；`decision="stop"` 时标记 `failed_terminal`；不允许改写 `requested_stages`。
- [ ] Decision applier 写 `gene_annotation.final_gene_set_decision` artifact，调用 `Ledger.record_event`。

### 6. 构建 node graph 路由

- [ ] 在 `app/stages/gene_annotation/routing.py` 中添加路由条件函数，按 stage_output status 分发到各 gate block 节点（`"methods_planned"` → method planning review；`"candidates_ready"` → candidate evidence review；`"merge_ready"` → merge review；`"merge_accepted"` → final gene set review）。
- [ ] 在 `app/stages/gene_annotation/builder.py` 中将上述 gate block 节点和路由条件接入 node graph，保持 `fail_gene_annotation` 仍为终态节点。

### 7. Planning-only chain 端到端联通

- [ ] 在 `app/stages/gene_annotation/nodes.py` 中添加 planning-only stub 节点 `run_gene_methods_planning_only(state)`：对 `confirmed_methods`（来自 method planning review 决策）中每个方法，在 stage_output 的 `candidate_refs` dict 中写入预期输出路径（文件不存在），`gene_count=0`，标记 `execution_support="planning_only"`。
- [ ] 确认所有 gate 的 `auto_decidable=True` 路径在 planning-only 模式下均可无阻通过 policy engine（`mode="auto"` 时不 block），使 chain 在测试和 CI 中无需人工干预即可跑通。

### 8. Run trace 集成

- [ ] 在 `app/reports/trace_writer.py`（或对应 trace 模块）中，确认 `annotation.intake_plan` artifact 被识别为独立 trace 条目，显示 `recommended_plan`、`final_plan` 和 `changed` 字段。
- [ ] 确认每个 annotation gate 的 trace 条目包含 `gate_id`、`reason`、`auto_decidable`、`decision`、`decided_by`、`rationale_artifact_ref`（如有）字段；planning-only gate 标记 `execution_support="planning_only"`。

### 9. 测试覆盖

- [ ] `tests/test_annotation_intake.py`：`annotation_intake_plan_ref` 缺失时 `enter_gene_annotation` 返回 `failed` status，不进入后续 gate。
- [ ] `tests/test_annotation_intake.py`：`annotation_intake_plan_ref` 指向不存在文件时同样拒绝进入 chain。
- [ ] `tests/test_gene_annotation.py`（或新文件）：planning-only chain 从 `enter_gene_annotation` 到 `commit_gene_annotation` 全程 auto-decide，最终 stage_output `status="completed"`。
- [ ] 同上：`gene_method_planning_review` gate 在 `mode="strict"` 下 block，注入 resume decision 后继续，并且 `candidate_refs` 中只保留 decision 选定的方法。
- [ ] 同上：`candidate_evidence_review` gate 在 `rna_protein_evidence_conflict_review` trigger 存在时 `auto_decidable=False`；注入 `proceed_merge` decision 后 stage_output 中 `accepted_candidate_refs` 非空。
- [ ] 同上：`merge_review` gate 在 `evidence_merge_policy_required` trigger 存在时 `auto_decidable=False`；注入 `adjust_weights` decision 后 `merge_weights` 写入 stage_output。
- [ ] 同上：`final_gene_set_review` gate 在 planning-only（BUSCO signal missing）时 `auto_decidable=True`；注入 `accept` decision 后 chain 正常提交并推进到 `function_annotation`。
- [ ] 同上：在 `gene_method_planning_review` 处中断，写 checkpoint，重新加载 state，注入 resume decision，验证 chain 继续且上游阶段（`repeat_annotation`、`hic_scaffolding`）stage_output 未被改写。
- [ ] `tests/test_run_trace.py`：planning-only chain 完成后 trace 包含 `annotation.intake_plan` 条目和四个 annotation gate 条目，每个条目的 `gate_id`、`decision`、`decided_by` 字段非空。
- [ ] 同上：`gene_method_planning_review` 使用 `select_methods` decision 时 trace 条目的 `downstream_effect` 字段记录 `confirmed_methods` 变化。

### 10. stage_contracts.md trigger 对齐

- [ ] 检查 `docs/stage_contracts.md` 中 `gene_annotation` 的 review triggers 列表，确认 `rna_protein_evidence_conflict_review`、`evidence_merge_policy_required`、`species_or_training_policy_required`、`unmasked_genome_fallback_requires_review` 与 `nodes.py` `_review_triggers` 函数及各 gate 的 `auto_decidable` 条件一致；若有出入则更新文档（不改代码）。

## 验收

本任务完成时：

- `enter_gene_annotation` 节点调用 `load_annotation_intake_plan`：缺失或损坏的 `annotation.intake_plan` 使 chain 在入口以 `failed` 状态停止，不进入任何 gate；
- `gene_method_planning_review`、`candidate_evidence_review`、`merge_review`、`final_gene_set_review` 四个 gate 均通过 `register_review_gate(ReviewGateDeclaration(...))` 注册，可在 registry 中按 `(stage, gate_id)` 查找；
- planning-only annotation chain（`mode="auto"`）从 `enter_gene_annotation` 到 `commit_gene_annotation` 全程 auto-decide，所有测试通过，无需人工干预；
- `mode="strict"` 下在任意 gate 处注入 resume decision 后 chain 继续，上游阶段 stage_output 不被改写（由 durable resume 测试验证）；
- run trace 包含 `annotation.intake_plan` 条目以及四个 gate 的 `gate_id`、`reason`、`decision`、`decided_by` 字段；
- `gene_method_planning_review` 是唯一可调用 `rewrite_requested_stages_after_assembly_review` 的 gate，其余三个 gate 的 decision applier 不包含 suffix 改写逻辑；
- task 04-06 可通过替换 planning-only stub 节点和填充真实 runner 来落地执行，不需要修改 gate 声明或 chain 主干路由。
