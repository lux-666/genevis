# 10 Natural-language Draft Plan Approval

任务状态：待设计/待实现。

## 目标

把 `gene-agent tui` / interactive ask 的入口整理成一条清晰、可解释、可纠错的
自然语言规划链路：

```text
自然语言请求
  -> LLM 解析稳定意图
  -> planner 生成初始 draft plan
  -> operator 同意或拒绝 draft plan
  -> 同意则补齐方法/参数并最终确认
  -> 拒绝则进入手改环节
```

用户输入一句话后，系统首先应该展示“我准备对哪些文件做哪些阶段”的计划草案，
并问 operator 是否同意。只有 operator 不同意，才进入 stage/method/parameter
手改流程。

## 第一性原则

- **自然语言入口的职责是生成可审阅计划，不是直接进入表单。**
- **LLM 负责理解用户目标，planner 负责把目标变成可执行草案。**
- **草案不是最终事实。** 草案必须能被接受、拒绝、编辑；最终执行仍以确认后的
  ask plan / run config 为准。
- **用户目标边界就是 ask 计划的执行边界。**
  - 用户目标：operator 想做哪些 stage，例如 `["assembly", "gene_annotation"]`。
  - 执行顺序：这些被选择 stage 在 `STAGE_ORDER` 中的稳定顺序；planner 不再
    默默补齐未选择的上游 stage。
- **输入文件必须是一等信息。** plan 草案和最终确认都必须展示即将处理的 manifest
  assets / paths。
- **没有明确目标时不要假装有 plan。** 如果 LLM 没解析出 workflow goal，应进入
  stage selection，而不是根据 raw reads 默认推断全流程或 assembly。

## 当前问题

当前实现里有四个概念混在一起：

- `EntryRequest.requested_stages`：LLM 解析出的用户目标。
- `AskPlanState.selected_stages`：有时表示用户已确认的目标，有时又被当成自动草案。
- `resolved_execution_order`：用户选择 stage 的稳定执行顺序。
- `auto_resolve_plan()`：既承担默认方法选择，又间接决定是否出现 plan preview。

这种结构导致 UI 行为不稳定：

- raw long reads 曾经会被无条件当作 assembly 计划。
- assembly-only 请求能出现 auto plan preview，但多阶段目标会直接进入 stage
  selection。
- 用户拒绝 auto plan 后，默认 stage 曾经回退到 manifest 推荐的全链路，而不是用户
  自然语言目标。
- preview 展示过晚或不完整，operator 不容易先判断“系统理解我想干什么”。

这些不是单点渲染问题，而是缺少一个正式的 **Draft Plan** 层。

## 目标结构

### 1. Intent Parse

LLM 解析用户自然语言为 `EntryRequest`，只抽取稳定事实：

- 输入路径或 manifest。
- sample/session/data-dir 等稳定字段。
- platform / asset type / genome size 等稳定 hint。
- `requested_stages`：用户 workflow 目标。

Intent parse 不解析：

- method choice。
- thresholds。
- database choice。
- review decision。
- resume/retry/rerun state。

### 2. Draft Plan Build

新增一个明确的 draft plan 构建层，例如：

```python
build_initial_plan_draft(ask_plan: AskPlanState) -> AskPlanDraft | None
```

> **注：** 最终实现未引入 `AskPlanDraft`，返回 `AskPlanState | None`，详见下文"建议的数据边界"。

Draft plan 至少包含：

- `target_stages`：来自 `EntryRequest.requested_stages` 的用户目标边界。
- `resolved_execution_order`：按 `STAGE_ORDER` 排序后的目标 stage。
- `stage_roles`：local-executable / planned-only 等展示角色；`dependency` 仅用于
  兼容旧 plan 或外部手工构造的 plan。
- `default_methods`：planner 可以安全建议的方法。
- `pending_method_choices`：仍需 operator 选择的方法。
- `pending_parameters`：仍需确认或填写的参数。
- `input_assets`：从 manifest 回读的输入文件。
- `warnings`：缺少 Hi-C、annotation 输入、planned-only 等提示。

如果没有明确 `requested_stages`，返回 `None`，进入手改。

### 3. Draft Plan Approval

TUI/ask 初始交互应统一为：

```text
Plan Draft
  Inputs
  Target stages
  Execution order
  Methods: selected/default/pending
  Parameters: inferred/pending
Accept this plan? [y/N]
```

- `yes`：把 draft plan 落入 `AskPlanState`，继续 method/parameter clarification 和最终
  `Run Plan` 确认。
- `no`：进入手改；stage selection 的默认勾选来自 draft `target_stages`，不是 manifest
  全链路推荐。
- `no target`：不展示假 draft，直接 stage selection。

### 4. Final Run Plan

最终 `Run Plan` 仍是执行前的权威确认。Draft preview 只说明系统当前理解和建议，
不能替代最终确认。

## 建议的数据边界

新增轻量模型，避免继续复用 `AskPlanState` 表达未确认草案：

```python
class AskPlanDraft(BaseModel):
    target_stages: list[RequestableStage]
    resolved_execution_order: list[RequestableStage]
    selected_methods: dict[str, str | list[str]] | None = None
    method_parameters: dict[str, dict[str, Any]] | None = None
    shared_parameters: SharedParameters | None = None
    input_assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    pending: dict[str, Any] = {}
```

`AskPlanState` 表示已保存的交互状态；`AskPlanDraft` 表示尚未接受的计划草案。

> **实现决策（未采纳独立模型）：** 上面的 `AskPlanDraft` 字段逐一映射后发现，
> `target_stages`/`resolved_execution_order`/`selected_methods`/`method_parameters`/
> `shared_parameters` 五个字段与 `AskPlanState` 完全同名、同类型、同义；`input_assets`/
> `warnings`/`pending` 三个则是从 manifest 与 plan 动态推导的展示值（`build_run_plan_summary()`
> 已经算）。硬建独立模型只会得到一个与 `AskPlanState` 同构的壳，外加 accept 时逐字段
> 搬运的 `draft_to_state()`——这才是过度封装。最终 `build_initial_plan_draft()` 直接返回
> 一个**未确认的候选 `AskPlanState`**，"草案 vs 已确认状态" 的边界由调用方纪律保证
> （见该函数 docstring 的 contract），而非新类型。详见下文 `## 实现要点`。
> 残留的语义裂缝：同一字段在草案语境是"推荐值"、在确认语境是"operator 选定值"，
> 当前靠 docstring 约定区分；若未来这层约定不够，再考虑加只读 property alias，
> 而不是现在就引入未被消费的抽象。

## Scope

- 重构 ask/TUI 初始规划链路，显式引入 draft plan 层。
- 让所有明确 workflow goal 都进入同一套 draft preview + accept/reject 流程。
- 保留 LLM-first intent parse；不要在 Python 里做自然语言关键词兜底。
- 保留手改能力；reject 后进入现有 stage/method/parameter clarification。
- 统一 preview 与最终 Run Plan 的输入文件展示。
- 明确 selected target stages 与 ordered execution boundary 的区别。

## Non-goals

- 不新增新的自然语言命令语法。
- 不让 LLM 选择方法、阈值、数据库或 review decision。
- 不改变 graph stage 的真实执行语义。
- 不把 draft preview 变成最终确认。
- 不在本任务内重做 questionary UI 或开发 web UI。
- 不默认 raw reads 必然要 assembly 或全流程。

## 实施任务

- [x] 定义 `AskPlanDraft` 或等价内部结构，并写清楚它与 `AskPlanState` 的边界。
- [x] 实现 `build_initial_plan_draft()`：
  - [x] 从 `entry_request.requested_stages` 读取目标。
  - [x] 按 `STAGE_ORDER` 排序用户选择的 stage，不自动扩展上游依赖。
  - [x] 回读 manifest assets 作为 `input_assets`。
  - [x] 填入安全默认方法或标记 pending。
  - [x] 生成 warnings / pending fields。
- [x] 实现 draft accept/reject：
  - [x] accept 将 draft 合并进 `AskPlanState`。
  - [x] reject 保留 draft target stages 作为 stage selection 默认值。
  - [x] no-goal 直接进入 stage selection。
- [x] 收敛或替换 `auto_resolve_plan()`：
  - [x] 不再由它决定是否出现 plan 环节。
  - [x] assembly-only Flye 默认选择作为 draft plan 的普通规则存在。
- [x] 调整 TUI/ask 渲染：
  - [x] 先展示 `Plan Draft`。
  - [x] 再问 `Accept this plan?`。
  - [x] 最后仍展示最终 `Run Plan`。
- [x] 更新 tests，覆盖自然语言目标、reject、无目标、输入文件展示和边界停止。

## 验收

- 输入：
  ```text
  /path/bht.Nanopore.pass.first500.fa 做一下组装和 gene 注释
  ```
  首屏必须展示 draft plan，而不是直接进入 Stage Selection。
- 该 draft plan 至少展示：
  - 输入 FASTA 路径。
  - target stages：`assembly`, `gene_annotation`。
  - execution order：只包含用户目标 `assembly`, `gene_annotation`，按
    `STAGE_ORDER` 展示。
  - method/default/pending 状态。
  - 待确认参数。
- operator 选择 `yes` 后进入后续 clarification，不重新询问 stage selection。
- operator 选择 `no` 后进入 stage selection，默认勾选仍是 LLM 解析出的用户目标
  stages，而不是 manifest 推荐全链路。
- raw long reads + 无明确目标时，不展示假 plan，不默认 assembly，直接手选。
- `requested_stages=["assembly"]` 最终执行只包含 `assembly`，不会自动补
  `read_qc/assembly_review/repeat/gene/function`。
- plan preview 和最终 Run Plan 都能展示 manifest 中的实际 input assets。
- 测试覆盖 questionary 与 plain-text fallback 的关键路径。

## 建议测试

- Intent:
  - “做一下组装” -> `requested_stages=["assembly"]`（由 LLM/prompt fixture 或 mock 输出覆盖）。
  - “做一下组装和 gene 注释” -> `["assembly", "gene_annotation"]`。
  - 只写 “用 Flye” 不应被当作 stage goal。
- Draft planner:
  - assembly-only draft 展开为 `read_qc -> assembly`。
  - assembly + gene_annotation draft 展开到 gene 需要的依赖，但 target 仍只有用户目标。
  - 无 requested stages 返回 `None`。
- Interactive:
  - 有 draft 时先显示 `Plan Draft`，再问 accept/reject。
  - accept 跳过 stage selection。
  - reject 进入 stage selection，默认勾选 target stages。
  - 输入 assets 显示完整路径。
- Routing:
  - requested/execution boundary 保证 assembly-only 不继续下游。

## Handoff Notes

本任务应优先于继续给 TUI 单点补丁。完成后，Phase 6 task 05 中“initial preview”
的语义应被升级为“draft plan approval”，并同步 `docs/handoff.md` 中的 operator
说明。

## 实现要点

- **draft 不引入新模型。** `build_initial_plan_draft()`（`app/entrypoints/ask_planner.py`）
  返回一个未持久化的候选 `AskPlanState`；"草案 vs 状态" 的边界就是"接受前不落盘"。
  渲染复用既有的 `build_run_plan_summary()`。
- **evidence-prefilled。** draft 预填能从证据推导的一切：target stages、排序后的
  execution boundary、manifest 推断的 shared params、安全默认方法（含 raw-long-reads-only 时 assembly→flye）、
  推断的方法参数。accept 直接采用整个 draft；随后复用既有 `next_clarification()` 循环，
  **只**对仍缺失/有歧义的项发问（如缺 genome size、多候选且无默认的 `assembly_review`）。
- **统一入口。** 删除了 `auto_resolve_plan` / `_confirm_auto_resolve` /
  `_render_intent_stage_preview` / `_try_auto_fill_methods` 双路径，TUI/ask 初始交互统一为
  Plan Draft → `Accept this plan?` → （accept 采用 / reject 进入手改 / no-goal 直接手选）。
- **headless 协议不变。** draft 仅作用于交互式 loop；`next_clarification` /
  `clarification_template`（`ask --clarification-response`）仍从 `stage_selection` 开始。
- **value source 仅用于展示。** Plan Draft 渲染时把解析自请求的值标 `(from request)`、
  推断默认值标 `(recommended)`，不写入 `AskPlanState`。
- 路由边界由 `tests/test_graph_routing.py` 与
  `test_build_run_config_for_assembly_goal_stops_at_dependency_expanded_target` 守护。
