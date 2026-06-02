# 07-03 注释 review chain

任务状态：规划中；依赖 Phase 7 task 01、task 02，以及 Phase 6 task 06。

## 目标

建立注释 agent 所需的完整 review gate 链。该链不重新决定整条下游计划，而是从 Phase 6 task 06 在 `assembly_review` continue-downstream 分支产出的 `annotation.intake_plan` 开始执行。后续每个 gate 只负责自己的局部阶段决策，并复用 task 01 的通用 gate/resume/recommender/trace 机制。

先在 planning-only 方法之上搭出 chain skeleton，使 gate 顺序、wait/trigger 行为、recommendation、durable resume 和 run trace 能先被验证；真实执行 runner 再由 task 04-06 逐步填入。

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
gene-method planning review         [new]
  ↓
run gene method(s)                  [planning-only first; real runners in task 04]
  ↓
candidate-evidence review           [new]
  ↓
merge / EVM-weight review           [new; implementation detail resolved with task 05]
  ↓
final gene-set review + BUSCO       [new]
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
  - `merge_review` / `evm_weight_review`（命名在实施时与 task 05 对齐）；
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

## 数据一致性要求

- `annotation.intake_plan` 是 annotation chain 的起点和计划来源。
- 后续 gate 不得改写 `assembly_review` 或更上游阶段记录。
- 后续 gate 的局部选择必须写入对应 stage output 和独立 decision artifact。
- 如果某个后续 gate 改变未来阶段/方法/参数，必须写入 plan-change artifact，并在 ledger 中与普通 gate decision 区分。
- Planning-only gate 必须诚实标记 execution support，不能把未落地 runner 呈现为可执行。

## 任务

- [ ] 定义 annotation chain 如何读取和校验 `annotation.intake_plan`。
- [ ] 将 `hic_scaffolding` 的现有 gate/resume 路径纳入 chain trace，作为 annotation agent 的第一个可选局部 gate。
- [ ] 接入 task 02 的 `mask_review` gate，并将其输出的 recommended gene input 传给 gene annotation planning。
- [ ] 添加 `gene_method_planning_review`：基于 evidence profile 和 candidate methods 选择要运行的 gene method(s)。
- [ ] 添加 `candidate_evidence_review`：比较候选 gene models，决定是否进入 merge。
- [ ] 添加 `merge_review` / `evm_weight_review`：暴露 merge/EVM 权重决策，但不在更早阶段提前调权。
- [ ] 添加 `final_gene_set_review`：消费 final gene set + BUSCO 信号并决定是否提交。
- [ ] 让 chain 在 planning-only 模式下端到端运行，所有 gates 都能 block/resume/auto-decide。
- [ ] 在 run trace 中展示 intake plan 与所有 annotation gates。
- [ ] 添加测试：intake plan 缺失时拒绝进入 chain；planning-only chain 可完成；每个 gate 的 wait/trigger 行为正确；resume 后不会重跑上游阶段；trace 能区分 intake plan、gate decision 和 plan-change artifact。

## 验收

本任务完成时：

- Phase 6 task 06 产出的 `annotation.intake_plan` 会驱动 annotation chain；
- mask review、gene-method planning review、candidate-evidence review、merge review、final gene-set review 都通过 task 01 的通用 gate 机制声明；
- planning-only annotation chain 可以完整走通并 durable resume；
- run trace 能解释每个 gate 为什么出现、推荐了什么、最终谁做了什么决定；
- 真实执行任务（task 04-06）可以逐步替换 planning-only candidate，而不改 gate 链主干。

## 待决问题

- “merge” 是独立 stage/gate，还是 `gene_annotation` commit path 内的 gate；这会影响 merge review 挂载位置。
- 多方法 candidate gene models 在 merge 前如何表示，才能让 candidate-evidence review 做可比对。
- BUSCO 是 final-gene-set review 的 signal，还是独立 step。
- 后续 gate 是否允许局部改写未来 suffix；如果允许，哪些 gate 有权限。
