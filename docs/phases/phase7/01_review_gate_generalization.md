# 07-01 通用化 review-gate 机制

任务状态：已落地（registry 基础机制完成）。

## 目标

把 review-gate 机制从少数硬编码阶段中抽出来，使任意注释阶段都能声明自己的 gate、推荐器和触发条件，而不 fork 逻辑。这是 Phase 6 task 06 的 annotation intake gate，以及 Phase 7 后续 annotation review chain 的共同前置。

## 当前状态（已验证）

- Policy engine 已经是通用的：`app/review/policy.py` 的 `evaluate(...)` 会根据 `mode`（`strict`/`auto`）、`max_blocked_seconds` 和 `ReviewPayload.auto_decidable` 返回 `AUTO_DECIDE_NOW` / `BLOCK_INDEFINITE` / `BLOCK_WITH_TIMEOUT` / `BLOCK_PENDING_CONFIRM`。
- 持久 timeout 已实现：`app/control/lifecycle.py` 的 `blocked_timeout_seconds` 路径会检查 blocked session 是否超时。
- `ReviewPayload` 已经是阶段无关 schema：包含 `stage`, `gate_id`, `reason`, `candidates`, `signals`, `decisions_allowed`, `auto_decidable`, `report_refs` 等字段。
- `assembly_review` 已有完整 resume 路径；`hic_scaffolding` 也已经有 gate/resume 分支和测试。因此当前问题不是“只有 assembly_review 存在”，而是机制还没有完全通用化。
- LLM recommender 已改为通过 gate registry 查询 prompt bundle，并支持 `stage/gate_id` 查找。
- `resume_review_state` 已改为 registry 分发；未知 gate 在 registry 层被拒绝。
- 交互式 CLI 已改为 registry 驱动：`assembly_review` / `hic_scaffolding` 保留专用 prompt，其它注册 gate 走通用 prompt。

## 与 Phase 6 task 06 的关系

Phase 6 task 06 不应该自己发明一套 replan/resume 机制。它应成为本任务通用 gate registry 的一个消费者：

- gate 名称/阶段：`assembly_review` 上的 annotation intake gate；
- branch：只在 `continue-downstream` 分支进入 annotation intake；
- 结果：写入 `annotation.intake_plan` artifact，并更新后续 `requested_stages` suffix；
- resume：仍通过同一条 durable resume/update-state 路径。

这样 Phase 7 task 03 可以把 annotation intake plan 当作 annotation review chain 的起点，而不是重新解析 `assembly_review` 决策。

## 范围

- 定义一个轻量 gate declaration/registry：每个 gate 声明 stage/gate id、`decisions_allowed`、policy defaults、`auto_decidable` 计算方式、recommender prompt bundle、decision applier。
- 通用化 recommender bundle lookup：按 stage/gate 查询，而不是硬编码单个 literal map。
- 通用化 resume dispatch：新增 gate 的 decision application 通过 registry 查找，而不是继续增长 `resume_review_state` 的 `if/elif`。
- 通用化交互式 review 入口：`collect_gate_decision` 根据 gate declaration 选择 prompt/render adapter；没有专用 prompt 时使用通用 decision form。
- 把现有 `assembly_review` 和 `hic_scaffolding` 纳入同一 registry，但保持现有行为不变。
- 为 Phase 6 task 06 的 `annotation_intake` 和 Phase 7 task 03 的 annotation gates 留出声明位。

## 非目标

- 不在本任务中新增 annotation review chain 的具体 gates；那是 task 03。
- 不在本任务中实现 RepeatMasker/mask review；那是 task 02。
- 不改变 wait/trigger 语义；它们已经存在且正确。
- 不改变 `assembly_review` 的业务决策语义。
- 不构建 Web UI 或全屏 TUI。

## 设计方向

推荐使用“程序化 registry + 小型声明对象”，不要先做复杂 descriptor 系统：

- `ReviewGateDeclaration` 描述 gate 的静态部分：`stage`, `gate_id`, `decisions_allowed`, `prompt_bundle`, `policy_defaults`, `decision_applier`。
- 动态触发由阶段 node 构建 `ReviewPayload` 时给出：`auto_decidable`, `signals`, `candidates`, `report_refs`。
- `resume_review_state` 只做通用校验，然后从 registry 找 applier。
- `review_interactive` 先从 `active_interrupt.stage/gate_id` 找 declaration，再渲染 brief 和 prompt。
- 专用 gate（如 `assembly_review`）可以保留专用 prompt；普通 annotation gates 走通用 prompt。

## 数据一致性要求

- Registry 只描述行为入口，不保存运行状态。
- `ReviewPayload` 仍然是每次 gate request 的权威 payload。
- Gate decision artifact、recommendation artifact、resume ledger event 仍然分开记录。
- 未注册 gate 必须被拒绝，不能 fallback 成任意可执行路径。
- 纳入 registry 后，`assembly_review` 和 `hic_scaffolding` 的现有测试应保持通过。

## 任务

- [x] 定义 `ReviewGateDeclaration` 和 gate registry。
- [x] 把 `assembly_review` 注册为现有行为的第一个消费者。
- [x] 把 `hic_scaffolding` 注册到同一机制，承认它已经有 gate/resume 路径。
- [x] 将 recommender bundle lookup 改为 registry 驱动。
- [x] 将 `resume_review_state` 的阶段分发改为 registry 驱动。
- [x] 将 `collect_gate_decision` 的支持阶段检查改为 registry 驱动。
- [x] 为无专用 prompt 的 gate 提供通用 decision prompt/render adapter。
- [x] 添加测试：现有 `assembly_review` / `hic_scaffolding` 行为保持不变；未知 gate 被拒绝；registry gate 能完成 recommend/resume 路径。

## 验收

本任务完成时：

- 新 gate 可以通过 registry 声明，而不修改 `resume_review_state` 的硬编码分支；
- recommender prompt bundle 可以按 stage/gate 注册；
- `assembly_review` 和 `hic_scaffolding` 继续按现有语义运行；
- 后续 Phase 6 task 06 和 Phase 7 task 03 能复用同一 gate/resume/trace 机制。
