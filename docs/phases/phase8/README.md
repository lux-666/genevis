# Phase 8: Inventory Alignment, Assembly Methods, And T2T Stage

状态：重新校正设计中。之前的 Phase 8 文档把输入画像、assembly candidate、Hi-C curation、T2T finishing 都写成了 assembly 内部子代理，这是错误边界。现在统一回到既有 stage 模型：

```text
inventory
read_qc
genome_survey
assembly
assembly_review
hic_scaffolding
t2t
repeat_annotation
gene_annotation
function_annotation
```

`inventory` 负责识别输入、登记资产、汇总 run-config / manifest / stage hint，并给后续 stage 提供结构化 profile。不要新增另一个输入画像 stage，也不要把这部分放进 assembly。

`assembly` 根据数据类型和 route 选择 long-read assembly 方法，产出 draft assembly candidate。这里可以在 assembly 内部再拆 method-application subgraph，因为 Flye 只是一个方法，后续还会有 hifiasm、NextDenovo、Canu 或其他数据类型方法；但这个拆分是 assembly stage 的内部实现，不是新的 top-level stage。

`hic_scaffolding` 已经是独立 stage。Phase 8 不再新增 assembly 内部的 Hi-C 主线。需要增强 Hi-C workspace、manual curation、candidate selection 时，应该落在现有 `hic_scaffolding` stage。

`t2t` 是 Phase 8 真正新增的 top-level stage，放在 `hic_scaffolding` 之后、`repeat_annotation` 之前。没有 Hi-C 时，路由可以跳过 `hic_scaffolding` 后进入 `t2t`；有 chromosome 目标时，T2T 消费 Hi-C 后的 final assembly candidate。

## 当前实现状态

已存在：

- `inventory` stage：manifest / asset registry / existing assembly routing。
- `assembly` stage：已有 route-selection 相关 Slice 1 代码；方法调用已下沉到 assembly 内部 `method_application/` package。当前包含 Flye 和 NextDenovo execution graph，外层 assembly graph 不再直接挂具体方法的 freeze/run/collect 节点。
- `assembly_review` stage：评估、人工 gate、rerun/review 逻辑。
- `hic_scaffolding` stage：已有独立 builder/routing/nodes 和 gate/resume 基础。

## 最近提交兼容性检查

`ca685a9 Implement phase 8 assembly route slice` 和当前校正后的设计不冲突，但有命名和归属上的历史包袱需要显式保留：

- 已落地的 `resolve_extended_inputs` / `infer_input_profile` / `select_route` 当前位于 `app/stages/assembly/nodes.py`。短期作为 assembly 的 route-selection prelude 保留；长期若需要更完整输入画像，再把职责前移到 `inventory`。
- 已注册的 `assembly/route_selection` review gate 可以保留。它 gate 的是 assembly 方法路线确认，不是一个独立 input-profile stage。
- 已新增的 `AssemblyInputProfile`、`AssemblyRouteDecision`、`AssemblyQCSummary`、`AssemblyT2TReadiness`、`AssemblyReviewTasks` 是 Slice 1 schema。后续新增 top-level `t2t` 时，应把 T2T runtime artifacts 迁到 `t2t.*` 命名下，同时保持旧 assembly-scoped schema 在过渡期可读。
- 已新增的 `descriptors/knowledge_packs/assembly/input_profiler` 是兼容目录名。文档不再把它当作 stage 名；未来可以迁到 `inventory/input_profile` 或作为 assembly route-selection 的 legacy pack alias。
- `AssemblyRunConfig.target_level` 目前只支持 `draft | near_t2t | strict_t2t`，尚未支持 `chromosome`；这正是 Phase 8 后续 schema delta，而不是冲突。

`eb1c838 Align phase7 annotation docs and drop redundant bundle metadata` 只影响 function annotation bundle metadata 和 Phase 7 文档字段名，不触碰 `inventory` / `assembly` / `hic_scaffolding` / `t2t` 边界；和当前 Phase 8 设计无冲突。

需要校正：

- 文档全部使用 `inventory`，不再使用其他输入画像 stage 名。
- `assembly` stage 改写为多方法 long-read candidate stage。
- Hi-C 相关任务改写为增强既有 `hic_scaffolding` stage，而不是 assembly 内部 agent。
- 新增 `t2t` stage 的 schema、builder、routing、stage order、review gate 和测试。

## Phase 8 MVP

必须做：

1. 修正文档和 stage 边界，所有设计围绕既有 stage 名。
2. `inventory` 输出更完整的数据 profile：Cyclone/ONT/HiFi/Hi-C、target level、route hints、method hints。
3. `assembly` 支持多方法 candidate planning/execution，Flye 和 NextDenovo 方法应用已下沉到 method-specific subgraph，后续在同一 package 追加 hifiasm/Canu 等方法。
4. 保持 `assembly -> assembly_review` 对外 contract 稳定。
5. 增强现有 `hic_scaffolding` stage 的 chromosome-level candidate/workspace 语义，不新增平行 Hi-C 主线。
6. 新增 `t2t` stage：telomere/gap/readiness review、review tasks、strict/near T2T final gate。

暂不做：

- 重命名 `inventory` 代码。
- 在 assembly 内部实现独立 Hi-C curation agent。
- Verkko/HERRO/Dorado/Medaka/Teloclip/TeloComp 的真实执行。
- 自动修改 genome 以补端粒或 gap。

## 任务列表

- `[~] 01_schema_and_knowledge_packs.md`：校正 schema/pack 命名，补 inventory profile、assembly method provenance、T2T artifacts。
- `[ ] 02_inventory.md`：既有 `inventory` stage 的增强。
- `[~] 03_assembly_stage_methods.md`：`assembly` stage 多方法候选与 method-application package；Flye graph 和 NextDenovo graph 已接入。
- `[ ] 04_hic_scaffolding_stage.md`：既有 `hic_scaffolding` stage 的增强边界。
- `[ ] 05_t2t_stage.md`：新增 top-level `t2t` stage。
- `[ ] 06_subgraph_wiring_and_cleanup.md`：更新 stage order、parent graph wiring、t2t stage 接入、旧 phase8 错误命名清理。

## 对外接口

`assembly` 仍然向 `assembly_review` 提供稳定 candidate/draft refs。`hic_scaffolding` 消费 `assembly_review` 的 final assembly ref，并在 chromosome 目标下产出 scaffolded/final assembly。`t2t` 消费 `hic_scaffolding` 或 `assembly_review` 的 final assembly ref，产出 T2T readiness、review tasks 和 final assembly decision。

## 路由意图

```text
inventory
  -> read_qc
  -> genome_survey
  -> assembly
       route/method planning
       method_application subgraph
       candidate refs
  -> assembly_review
       final_assembly_for_scaffolding_or_annotation
  -> hic_scaffolding     # optional by target/data
       final_assembly_for_t2t_or_annotation
  -> t2t                 # optional by target/policy, new in Phase 8
       final_assembly_for_annotation
  -> repeat_annotation
  -> gene_annotation
  -> function_annotation
```
