# Phase 8：Assembly Agent —— 数据类型驱动的 T2T 组装代理

状态：Slice 1 已实施（task 01-02 落地）；LongReadCandidateAgent / HiCEvidenceAgent / T2TFinishingAgent 仍待后续切片实现。

## 当前实现状态

已落地：

- task 01：schema 扩展、assembly skill-like knowledge pack 目录、`load_skill_context`、Cyclone alias、hifiasm ONT shared parameter 映射。
- task 02：`resolve_extended_inputs`、`infer_input_profile`、`select_route`、`apply_route_decision`、`assembly/route_selection` review gate，以及 assembly 子图中的 route gate 接线。
- route selection 默认遵守系统 strict policy；只有显式 `review_policy.mode = "auto"` 且推荐置信度满足阈值时才自动采纳。
- LLM route selector 不可用时返回 `insufficient_data` + `confidence=0.0`，进入人工/补数据 review，不使用启发式自动 fallback。

仍待实现：

- task 03-06 的三路线 candidate runner、QC decision gate、Hi-C evidence、T2T finishing、最终子图清理。
- 当前子图仍在 route selection 之后回到既有 Flye candidate planning path；Phase 8 的完整多候选/T2T 子图尚未替换旧路径。

## 为什么做这个 phase

现有 assembly stage 是一条 Flye-only 的线性流水线，决策靠人。Phase 8 把它重构成一个真正的 Assembly Agent：感知输入数据类型（Cyclone/ONT/HiFi/Hi-C）、通过可插拔知识库推荐路线、调度正确的工具、评估 QC 结果，并把"该怎么办"的专家判断从人身上转移到 LLM + 知识库。

**核心设计原则**

- **数据驱动，不是工具驱动**：先判断"手上有什么数据"，再决定"用什么工具"。HiFi 是与 Cyclone/ONT 并列的一级路线，不是 ONT 失败后的 fallback。
- **知识可插拔**：专家经验存在 skill-like knowledge pack 中：`rules.yaml` 提供机器可读 guardrail，`SKILL.md` 提供专家推理流程，`cases.yaml` 提供可选案例样本。节点读包但不硬编码阈值，knowledge pack 可以独立更新，不需要改代码。
- **LLM 职责明确**：input profiling、route recommendation、gate decision、review task generation。LLM 不直接修改 genome，不做不可逆操作。
- **节点 API-style**：每个节点是一个可独立调用的能力（profile / select_route / run_candidate / evaluate_qc / decision_gate / generate_review_tasks / apply_human_decision），而不是只能在流水线中存在的步骤。
- **TUI 协作**：LLM 推荐路线和理由，TUI 暴露给用户，用户可接受或覆盖。

## 三层子代理结构

```
Assembly Agent
  ├─ InputProfiler         识别数据类型、覆盖度、目标等级，LLM 推荐路线
  ├─ LongReadCandidateAgent  根据路线运行组装工具，QC，decision gate
  ├─ HiCEvidenceAgent       Hi-C scaffold 证据、contact map、错连检查（可选）
  └─ T2TFinishingAgent      端粒/gap/QC 综合，生成 review tasks，final gate
```

三层子代理都在 assembly stage 内部，对外接口（→ assembly_review）不变。

## 对外接口不变

`commit_assembly_plan` 写入 `stage_outputs.assembly` 的 committed_keys 结构不变，`assembly_review` stage 不感知此次重构。

## Phase 8 任务列表

- `[x] 01_schema_and_knowledge_packs.md` — 新 schema + knowledge pack 基础设施
- `[x] 02_input_profiler.md` — 数据类型识别 + LLM 路线推荐
- `[ ] 03_long_read_candidate_agent.md` — 三路线候选组装 + QC + decision gate
- `[ ] 04_hic_evidence_agent.md` — Hi-C scaffold 证据 + 错连检查
- `[ ] 05_t2t_finishing_agent.md` — T2T readiness + review tasks + final gate
- `[ ] 06_subgraph_wiring_and_cleanup.md` — 子图连线 + 旧代码清理

## 与现有 hic_scaffolding stage 的关系

Phase 8 的 `HiCEvidenceAgent` 在 assembly stage 内部做证据收集和错连检查，输出供 T2TFinishingAgent 参考。现有独立的 `hic_scaffolding` stage 处理的是染色体级支架化，是下游注释的前置。两者不重叠：
- Assembly stage HiCEvidenceAgent：验证组装质量、检测错连
- hic_scaffolding stage：染色体支架化，面向注释输入

## 路由总图

```
enter_assembly
  ↓
profile                   InputProfiler
  ↓
select_route              LLM 推荐 + TUI 暴露
  ├─ cyclone_route ─┐
  ├─ ont_route ─────┼→ run_candidate → evaluate_qc → decision_gate
  └─ hifi_route ────┘
                         ├─ adopt_draft ──────────────────→ commit
                         ├─ enter_hic (has_hic) ──────────→ HiCEvidenceAgent
                         │                                       ↓
                         │                              hic_decision_gate
                         │                    ├─ t2t_finishing ──┐
                         │                    └─ adopt_draft ────→ commit
                         ├─ t2t_finishing ────────────────→ T2TFinishingAgent
                         │                                       ↓
                         │                         generate_review_tasks
                         │                                       ↓
                         │                           final_gate (human, strict-T2T)
                         │                                       ↓
                         │                                     commit
                         └─ block_human ──────────────────→ block
```
