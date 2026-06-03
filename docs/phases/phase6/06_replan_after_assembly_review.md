# 06 组装审阅后的重新规划（组装 -> 注释 agent 边界）

任务状态：本地实现完成；依赖 Phase 7 task 01 的通用 gate registry。

## 方向：三个虚拟 agent

这是本任务所指向的较长期框架。该工作流会被理解为现有单一父图和 `STAGE_ORDER` 之上的三个*虚拟* agent。它们是规划/归属边界，不是独立进程或独立图，因此当前的路由、checkpoint 和 resume 机制保持不变。

- **组装 agent**：`inventory, read_qc, genome_survey, assembly, assembly_review`。负责制定*第一份*计划，产出经过审阅的组装结果，并拥有 `assembly_review` gate。
- **注释 agent**：下游注释阶段，包括 `hic_scaffolding, repeat_annotation, gene_annotation, function_annotation`。`hic_scaffolding` 归注释 agent 所有（已决定）。它在 intake 时制定*第二份*计划：接受哪一个组装、是否把控制权退回给组装 agent 重新运行，以及自身的阶段/方法/参数。
- **解读 agent**（未来能力，不在本任务范围内）：消费组装 + 注释输出，产出生物学解读/综合。目前还没有对应阶段；这里只是记录方向，让边界清楚。

这样，两份计划有明确负责人：

- **计划 1** = 组装 agent 的计划（到达经过审阅的组装）。
- **计划 2** = 注释 agent 的 intake 计划，在 `assembly_review` -> 注释交接处决定。

`assembly_review` gate **就是**组装 -> 注释的交接边界。它的 continue-downstream 分支 = 把经过审阅的组装交给注释 agent，随后由注释 agent 制定计划 2，并写入 `annotation.intake_plan` artifact。它的 rerun/bounce 分支 = 把控制权交还给组装 agent。本任务精确实现这个边界（agent 1 -> agent 2）。解读 agent（agent 3）不在本任务范围内。

## 目标

把 `assembly_review` 人工 gate 视为从组装 agent 到注释 agent 的交接，并让注释 agent 的 intake 成为一份真实计划。gate 决策和注释 agent 的 intake 计划是同一个动作，而不是两个独立提示：在 gate 处，操作者判断组装结果；而“continue downstream”分支*就是*注释 agent 制定计划 2，即选择要运行哪些阶段、方法和参数，或者把组装退回去重新运行。

心智模型是：组装 agent 的第一份计划只需要到达 `assembly_review`；人工在那里判断组装；如果决定继续，注释 agent 推荐下一批工作，人工确认或改写；工作流随后按这份计划恢复。Phase 7 task 03 后续消费这份 `annotation.intake_plan`，而不是重新决定整条 annotation chain。

## 本任务依赖的路由事实（已验证）

- `_resolve_execution_order`（`app/entrypoints/ask_planner.py`）只会拉入**上游**依赖。选择 `assembly` 得到 `requested_stages = [read_qc, assembly]`；选择 `assembly_review` 得到 `[read_qc, assembly, assembly_review]`。它不会自动添加下游阶段。
- `route_controller`（`app/graph/routing.py`）遵守这一点：当 `requested_stages = [read_qc, assembly]` 时，运行会在 `assembly` 后立即前进到 `done`，并且**不会**进入 review gate 或任何下游阶段。（“只选择 assembly 也会继续跑下游”并不是当前 router 层面的行为。）
- 对本任务的影响：replan 入口（`assembly_review` gate）只有在 `requested_stages` 包含 `assembly_review` 时才存在。因此第一份计划至少必须到达 `assembly_review`，replan 才可触达。停在 `assembly` 的计划没有 gate，也没有 replan；这是正确且保持不变的行为。
- 当前 existing-assembly E2E 测试中看到的 blocked `assembly_review` 状态来自提交 `c9bbd1b`（移除了 deterministic_safe gate bypass），不是路由缺陷。

## 与任务 03、04、05 的关系

本任务直接建立在现有原语之上，不替代它们：

- 任务 03（`review_interactive.py`, `resume_review_state`）仍然是权威的 gate 决策 + 持久 resume 路径。计划 2（注释 intake）是同一个决策的 continue-downstream 分支，并通过同一套 resume 机制在一步内应用。bounce-to-rerun 决策复用现有 `rerun` 组装操作路径。
- 任务 04（`inspect --trace`, `build_decision_brief`）提供操作者在交接处制定计划 2 之前需要阅读的证据。
- 任务 05（`build_run_plan_summary`, `_render_plan_summary`）提供计划 2 预览复用的计划渲染器，其形态与计划 1 的预览一致。

三个 agent 的框架只是这些原语之上的*命名/归属*层，而不是重写。任务 03 和 04 仍欠的 HPC 验证不受本任务阻塞，应独立推进。

## 与 Phase 7 task 01/03 的关系

- Phase 7 task 01 先把 review-gate 机制通用化。本任务应作为该 gate registry 的消费者，而不是继续扩展硬编码 `resume_review_state` 分支。
- 本任务产出的 `annotation.intake_plan` 是 Phase 7 task 03 annotation review chain 的起点。
- Phase 7 task 03 只做后续局部 gate 决策（mask review、gene-method planning review、candidate-evidence review、merge review、final-gene-set review），默认不重新询问整条下游阶段列表。
- 如果后续 annotation gate 需要改变未来阶段/方法/参数，应复用本任务定义的 suffix-rewrite helper 和 artifact 形态，并记录独立 plan-change artifact。

## 本任务复用的当前机制

- 父图已经能在运行时跳过阶段：`route_controller`（`app/graph/routing.py`）读取 `state["requested_stages"]`，并将 `current_stage` 推进到不在该列表中的阶段之后。这意味着 replan 只需要在 blocked checkpoint 上重写 `requested_stages`（以及匹配的方法/参数状态）；其余由 router 完成。
- 交互式 gate 已经以进程内循环运行：`_interactive_gate_loop`（`app/entrypoints/run.py`）会在 session 为 `blocked_human` 时循环执行 `collect_gate_decision -> resume_review_state -> graph.update_state -> graph.invoke`。Replan 是该循环一次迭代中的额外步骤，位于 resume 决策和下一次 `graph.invoke` 之间。
- `gene-agent review`（`run_interactive_review`）是 Python 进程已经退出后的跨进程等价入口。由于长 HPC 运行会在新进程中 resume，replan 也必须从这个入口工作。

## 范围

- `assembly_review` 决策和下游 replan 是同一个流程。把 gate 的 `decisions_allowed` 映射为三类分支：
  - **continue-downstream**（例如 `accept`, `pass`, `continue_low_confidence`）：进入剩余阶段的 replan。
  - **assembly-operation**（`purge`, `polish`, `rerun`）：对组装本身采取动作；这些分支不进入下游 replan（运行会像今天一样回到 assembly/review 循环）。
  - **terminate**（`stop_human_review`）：不做 replan，直接结束。
  只有 continue-downstream 分支触发 replan；本任务不把 replan 添加到其它 gate。
- 对于 continue-downstream 决策，展示从当前持久状态和 manifest 推导出的剩余（下游）阶段推荐计划，使用与 ask flow 和最终 `Run Plan` 确认相同的计划词汇。
- 允许操作者原样接受推荐，或**覆盖**下游计划：剩余阶段选择、方法和参数会整体替换，而不只是裁剪。操作者可以添加原始 `requested_stages` 中没有的下游阶段，也可以删除原本包含的阶段。
- 通过替换 `requested_stages` 的下游部分及匹配的方法/参数状态，把改写后的计划应用到 blocked checkpoint，然后通过现有持久 resume 路径继续。
- 同时支持进程内 gate 循环（`tui` / `run --interactive`）和跨进程 `review` 命令。
- 把 annotation intake 记录为可持久、可审计的 `annotation.intake_plan` artifact + ledger event，并与 gate 决策 artifact 区分开。原样接受推荐时也写入该 artifact，标记 `changed: false`；覆盖下游计划时标记 `changed: true` 并记录 prior/recommended/final plan。

## 非目标

- 本任务不向 `assembly_review` 以外的任何 gate 添加 replan。`hic_scaffolding` 和其它 gate 保持当前单一决策流程。
- 不允许 replan 改写 `assembly_review` 处或其上游的阶段。Replan 只管理严格位于 `assembly_review` 下游的阶段；已经执行的上游阶段（`read_qc`, `assembly`, review 本身）绝不重新运行，也不会从记录中删除。
- 不改变 assembly-operation 分支（`purge`/`polish`）的行为；它们作用于组装本身，不进入计划 2。`rerun` 也保持当前行为，但在交接处被命名为“退回组装 agent”的路径（无新机制，只是命名）。
- 不在本任务中新增下游 annotation gates。Phase 7 会引入多个 annotation gates；本任务只负责 `assembly_review` 处的 annotation intake/replan。
- 不改变阶段执行语义、方法 registry 或 ask flow 的自动解析规则。
- 不构建 Web UI 或全屏 curses TUI。
- 测试不要求真实终端。

## 设计方向

优先添加一个轻量编排步骤，而不是新子系统：

1. **分支分类。** 收集 `assembly_review` 决策时，将所选决策分类为 continue-downstream / assembly-operation / terminate。只有 continue-downstream 进入步骤 2-4。这只是对 gate 现有 `decisions_allowed` 的小型映射，不是新的决策 UI。

2. **推荐构建器（只读）。** 给定当前持久状态（已执行阶段、已注册 artifacts、manifest assets），产出推荐的下游计划：`assembly_review` 之后各阶段的 `requested_stages` 及其 methods/params。复用 ask planner 现有推荐逻辑（`_recommended_stages_from_manifest`, `auto_resolve_plan`, `_resolve_execution_order`），而不是发明一套并行 recommender。唯一新增行为是把结果限制在 `assembly_review` 下游阶段。

3. **Replan 提示 + 预览（覆盖语义）。** 使用任务 05 的渲染器（`_render_plan_summary`，preview mode）渲染推荐计划，并允许操作者接受或改写。复用 ask flow 的交互式阶段/方法/参数提示，而不是新写一套。操作者确认后的选择会*替换*下游计划；原样接受则保留推荐。

4. **状态应用（唯一真正新增的 mutation）。** 将确认后的下游计划翻译为 checkpoint update：保留 `requested_stages` 的上游/已执行前缀，替换 `assembly_review` 之后的后缀为新选择；设置对应的下游 method/parameter 字段。该 update 使用 gate 循环已经用于 resume 决策的同一个 `graph.update_state` 调用，因此 resume 仍是单个原子步骤。随后 `route_controller` 会在下一次 router pass 中遵守新的 `requested_stages`（已验证）。

5. **审计记录。** 持久化一个 `annotation.intake_plan` artifact（所选 stages/methods/params + 推荐内容 + 先前下游计划 + `changed` 标记）和一个 ledger event，并与 gate 决策 artifact 分离，这样运行 trace（任务 04）可以展示“operator accepted annotation intake plan”或“operator edited annotation intake plan”。

Replan 步骤必须是**可跳过的**：原样接受推荐时，行为必须与继续使用原始下游计划完全相同。即使是 no-op，也写入 `annotation.intake_plan` artifact，并标记 `changed: false`，这样 Phase 7 task 03 有稳定的 provenance 输入。Replan 是 continue-downstream 分支的增量能力，绝不能成为破坏非交互或 decision-file 路径的强制新提示。

## 操作者体验要求

在 continue-downstream `assembly_review` 决策中，操作者应该能够：

- 看到哪些阶段已经执行、哪些仍待执行；
- 看到剩余阶段的推荐计划（阶段、带 local/deferred 标签的方法、共享参数、方法参数、warnings），格式与最终 `Run Plan` 确认一致；
- 一步接受推荐（在不改动的情况下继续执行）；
- 覆盖剩余阶段选择、方法和参数，包括添加原始计划中没有的下游阶段；
- 在工作流恢复前看到剩余阶段计划的最终确认。

非交互路径和 decision-file 路径必须继续保持不变：从 decision file 恢复的 session 会沿用原始 `requested_stages`，不出现 replan 提示。

## 数据一致性要求

- 推荐构建必须只读。
- Replan 只替换 `requested_stages` 中 `assembly_review` 之后的后缀。上游/已执行前缀逐字保留；replan 绝不能把已执行阶段标记为重新执行，也绝不能删除已经运行过的阶段记录。
- 允许添加原本未请求的下游阶段（覆盖语义），但其上游依赖按构造已经满足，因为到 `assembly_review` 为止的所有内容都已经运行。
- Replan 状态 update 必须经过同一条已验证的 resume 路径；replan 不得写入会被 resume validation 拒绝的 checkpoint。
- 除非方法 registry 报告某个方法可执行，否则该方法不能在 replan 中被选择（与任务 05 相同的 guard）。
- `annotation.intake_plan` artifact 和 ledger event 必须与 gate 决策 artifact 区分开，这样审计者能分辨“决定了什么”和“计划了什么”。
- 原样接受推荐必须产出与继续原始下游计划完全一致的行为，并写入 `annotation.intake_plan` artifact，标记 `changed: false`。

## 任务

- [x] 将 `assembly_review` 的 `decisions_allowed` 映射为 continue-downstream / assembly-operation / terminate 分支，并确认只有 continue-downstream 应进入 replan。
- [x] 确认下游 replan 必须重写的精确状态字段（`requested_stages` 后缀，加上 router 和 stages 读取的方法/参数字段），并确认在 blocked checkpoint 上重写这些字段后，resume 时会被 `route_controller` 遵守。
- [x] 决定并记录原样接受（accept-as-is）时写入 `annotation.intake_plan` artifact，标记 `changed: false`。
- [x] 添加只读的下游推荐构建器，复用 ask planner 推荐逻辑，并将范围限制在 `assembly_review` 之后的阶段。
- [x] 添加 replan 提示/预览，复用任务 05 渲染器和 ask 的交互式阶段/方法/参数提示，并采用覆盖语义。
- [x] 添加状态应用逻辑：替换 blocked checkpoint 上的下游 `requested_stages` 后缀 + 下游 method/param 状态，并通过现有持久 resume 路径继续。
- [x] 在 continue-downstream 分支中把 replan 接入进程内 gate 循环（`_interactive_gate_loop`），并限制为只对 `assembly_review` 生效。
- [x] 在同一分支中把 replan 接入跨进程 `review` 命令。
- [x] 让 replan 可跳过，原样接受时行为与今天完全一致；保持非交互/decision-file 路径没有 replan 提示。
- [x] 持久化独立的 `annotation.intake_plan` artifact + ledger event，并在 run trace 中展示（任务 04）。
- [x] 添加测试：推荐是下游专属且只读；accept-as-is 等同当前行为；覆盖式 replan 会改变下游 `requested_stages`（包括添加原本未请求的阶段）和下游 methods/params；上游/已执行阶段不会重新运行或被删除；非交互/decision-file 路径不变；`annotation.intake_plan` artifact + ledger event 被独立记录。
- [x] 针对持久 sqlite blocked `assembly_review` session 验证 replan 循环（先 local smoke，待任务 03/04 的 HPC 验证存在后再做 HPC）。

## 验收

本任务只有在以下条件满足时才算完成：

- 对 continue-downstream `assembly_review` 决策，操作者会看到剩余阶段的推荐计划，并且可以接受或覆盖；
- 覆盖式 replan 会改变后续运行的下游 stages/methods/params（包括添加原始计划中没有的阶段），恢复后的工作流会遵守新的 `requested_stages`；
- 上游/已执行阶段绝不因 replan 重新运行或被删除；
- 原样接受 replan 会精确复现今天的下游行为；
- 非交互和 decision-file resume 路径保持不变；
- `annotation.intake_plan` 被记录为独立 artifact + ledger event，并在 run trace 中可见；
- replan 同时支持进程内 gate 循环和 `review` 命令。

## 本地实现记录（2026-06-02）

- 新增 `app/review/annotation_intake.py`，集中实现分支分类、下游推荐构建、覆盖式 suffix rewrite、checkpoint update、`annotation.intake_plan` artifact 和 `annotation_intake_planned` ledger event。
- `accept` / `pass` / `continue_low_confidence` 进入 annotation intake；`purge` / `polish` / `rerun` 和 `stop_human_review` 不进入 replan。
- 进程内 `_interactive_gate_loop` 和跨进程 `gene-agent review` 都先无副作用收集 annotation intake；只有 intake 确认成功后才调用 `resume_review_state` 写入 gate 决策，并把计划 2 合并进同一次 `graph.update_state`。
- `gene-agent resume --decision-file` 有显式回归测试覆盖：该路径不会调用 annotation intake helper，也不会误触发交互式计划 2。
- 推荐计划严格限制为 `assembly_review` 之后的阶段。若原计划已有下游 suffix，则默认接受该 suffix 以保持当前行为；若原计划停在 `assembly_review`，则从 manifest 推荐下游 annotation 计划。
- 修复记录（2026-06-02）：annotation intake 现在先以无副作用方式收集确认，只有确认成功才调用 `resume_review_state`，避免 intake 取消后留下 assembly review durable decision；下游 replan 会清理整个 downstream suffix keyed state（当前包括 `stage_outputs` 和 `skip_reasons`）；`annotation.intake_plan` 已加入 trace/inspect relevant artifact 路径，并在 trace artifact cap 下强制保留。
- 本地验证：
  - `conda run -n gene_agent python -m unittest tests.test_annotation_intake -v`
  - `conda run -n gene_agent python -m unittest tests.test_annotation_intake tests.test_review_interactive tests.test_entrypoints tests.test_review_resume tests.test_review_gates -v`
  - `conda run -n gene_agent python -m unittest tests.test_annotation_intake tests.test_review_interactive tests.test_entrypoints tests.test_run_trace -v`
  - `conda run -n gene_agent python -m unittest discover -s tests -p 'test*.py' -v`

## 已决事项

- 工作流被理解为三个虚拟 agent（组装 / 注释 / 解读），这是现有单图之上的规划/归属层，而不是独立进程或独立图。本任务只实现组装 -> 注释边界；解读 agent 是未来工作。
- 两份计划有明确负责人：计划 1 = 组装 agent（到达经过审阅的组装），计划 2 = 在 `assembly_review` 交接处进行的注释 agent intake。
- Gate 决策和计划 2 是一个流程，不是两个提示。计划 2 只会通过 `assembly_review` 决策的 continue-downstream 分支进入，并记录为 `annotation.intake_plan`。Assembly-operation 分支（`purge`/`polish`/`rerun`）和 terminate 分支（`stop_human_review`）保持当前行为；`rerun` 是“退回组装 agent”的路径。
- 计划 2 使用**覆盖**语义：操作者可以添加原始 `requested_stages` 中没有的下游阶段，也可以删除原本有的阶段。只重写 `assembly_review` 之后的后缀；已执行前缀保留。
- `hic_scaffolding` 属于**注释 agent**（已决定）。agent 边界画在 `assembly_review` 之后：从 `hic_scaffolding` 开始的一切都属于计划 2。
- 解读 agent（agent 3）是**未来的全新能力**，不会改装到 `function_annotation` 上。本任务范围外。
- 目前 agent 边界仍然是 `STAGE_ORDER` 上的**隐式分组**；不向 state 或 run-config 添加 agent 字段。如果多 agent 计划 UX 需要，后续任务可以将其形式化。
- Phase 7 task 03 消费本任务产出的 `annotation.intake_plan`，后续 gate 默认只做局部阶段决策。

## 待决问题（方向层面，实施前解决）

- 方向层面无待决问题。实施时问题（精确状态字段、suffix-rewrite helper 的 API）已在上方任务中跟踪。

## Handoff 记录要求

实现或验证完成后，更新 `docs/handoff.md`，记录：

- 用于 replan 验证的 session id 和 data dir；
- gate 决策及 replan 选择（推荐 vs. 已编辑）；
- resume 之后得到的 `requested_stages` 和下游状态；
- 确认已执行阶段没有重新运行；
- `annotation.intake_plan` artifact ref 和 ledger event；
- 任何 UX 缺口或后续任务。
