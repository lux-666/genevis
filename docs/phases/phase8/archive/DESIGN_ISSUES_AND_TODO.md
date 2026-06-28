# Phase 8 T2T Stage 设计问题与待实施项分析

**分析日期**: 2026-06-28  
**基于**: HPC 测试结果 (2026-06-26) + 实际代码检查

---

## 执行摘要

T2T stage 的**核心功能已实现并通过测试**，但文档中描述的一些**增强功能和架构规划仍待实施**。主要问题分为三类：

1. **文档状态标记不一致** - 部分已实现功能标记为待实施
2. **设计与实现的语义漂移** - 文档描述的 schema/contract 与实际代码存在差异
3. **真正的待实施功能** - 文档规划但尚未实现的增强功能

---

## 1. 文档状态标记问题

### 问题 1.1: README.md 任务列表与实际状态不符

**当前状态**:
```markdown
- `[ ] 05_t2t_stage.md`：新增 top-level `t2t` stage。
- `[ ] 06_subgraph_wiring_and_cleanup.md`：更新 stage order、parent graph wiring、t2t stage 接入、旧 phase8 错误命名清理。
```

**实际情况**:
- ✅ `t2t` stage 已实现并在 `STAGE_ORDER` 中
- ✅ Parent graph (`app/graph/builder.py`) 已接入 `build_t2t_stage()`
- ✅ 16 个单元测试通过 + HPC smoke test 通过
- ✅ 05_t2t_stage.md 开头标记"任务状态：已实现"

**建议**: 更新 README.md 任务列表标记为 `[x]` 或 `[~]`（部分完成）。

---

## 2. 设计与实现的语义漂移

### 问题 2.1: T2T Readiness & Review Tasks artifacts 缺失

**文档声称** (01_schema_and_knowledge_packs.md):
```python
T2T_READINESS = "t2t.readiness"
T2T_REVIEW_TASKS = "t2t.review_tasks"
T2T_GAP_SURVEY = "t2t.gap_survey"

class T2TReadiness(BaseModel):
    verdict: Literal["not_t2t", "near_t2t", "strict_t2t", "insufficient"]
    confidence: float
    reasoning: str
    telomere_summary_ref: str | None
    gap_survey_ref: str | None
```

**实际实现**:
- ❌ `T2T_READINESS`、`T2T_REVIEW_TASKS`、`T2T_GAP_SURVEY` 这些 artifact constants **不存在**
- ✅ 实际存在的 artifacts:
  ```python
  T2T_ASSEMBLY_TELOMERE_SCAN = "t2t.assembly_telomere_scan"
  T2T_READS_TELOMERE_SCAN = "t2t.reads_telomere_scan"
  T2T_TELOMERIC_READS = "t2t.telomeric_reads"
  T2T_EXTENSION_CANDIDATES = "t2t.extension_candidates"
  T2T_STATUS_SUMMARY = "t2t.status_summary"
  T2T_FINISHING_REPORT = "t2t.finishing_report"
  T2T_PATCHED_ASSEMBLY = "t2t.patched_assembly"
  T2T_PATCH_RECORDS = "t2t.patch_records"
  ```
- ❌ `T2TReadiness` 和 `T2TReviewTasks` models **不存在**
- ✅ 存在 `T2TFinishingBundle` 和 `T2TReviewDecisionArtifact`
- ⚠️  `AssemblyT2TReadiness` 和 `AssemblyReviewTasks` 仍在 assembly 命名空间（legacy prototypes）

**影响**:
- 当前实现专注于 **保守自动 patch**（conservative auto-patch），而不是 **综合 T2T readiness 评估**
- 没有实现 LLM-based review task generation
- Gap survey 功能完全未实现

**设计意图 vs 实际**:
- **文档设计**: T2T stage 应该做 readiness 判断、生成 review tasks、gap survey
- **实际实现**: T2T stage 只做 telomere scan、ONT evidence alignment、conservative patch、报告生成

**这是设计问题还是待实施?** → **待实施增强功能**。当前实现是 MVP（最小可行产品）。

---

### 问题 2.2: Hi-C → T2T 输出 key 的语义不一致

**文档声称** (04_hic_scaffolding_stage.md, 已在本次修复中更新):
```python
{
    "final_assembly_for_t2t_or_annotation": "...",
    "chromosome_scaffolded_assembly_candidate_ref": "...",
}
```

**实际实现**:
- ❌ `hic_scaffolding` **不写** `final_assembly_for_t2t_or_annotation`
- ❌ `hic_scaffolding` **不写** `chromosome_scaffolded_assembly_candidate_ref`
- ✅ `hic_scaffolding` 只写 `final_assembly_for_annotation`
- ✅ `t2t` 的 `_assembly_ref()` 搜索多个 fallback keys（为未来扩展预留）

**状态**: 已在本次漂移修复中添加 Implementation note 说明。

---

### 问题 2.3: Knowledge packs 缺失

**文档规划** (01_schema_and_knowledge_packs.md):
```text
descriptors/knowledge_packs/
  inventory/input_profile/
  assembly/route_selector/
  assembly/method_selection/
  hic_scaffolding/curation_review/
  t2t/criteria/
  t2t/review_task_guide/
```

**实际实现**:
```bash
descriptors/knowledge_packs/
  assembly/
    input_profiler
    qc_interpretation
    review_task_guide  # 这是 assembly 的，不是 t2t 的
    route_selector
    t2t_criteria       # 这是 assembly 的，不是 t2t 的
```

**缺失**:
- ❌ `inventory/input_profile/`
- ❌ `assembly/method_selection/`
- ❌ `hic_scaffolding/curation_review/`
- ❌ `t2t/criteria/`
- ❌ `t2t/review_task_guide/`

**当前 workaround**:
- `assembly/t2t_criteria` 和 `assembly/review_task_guide` 是 legacy prototypes
- 文档已说明"treat as compatibility packs"

**这是问题吗?** → **待实施**。文档明确说"Do not create unused packs until a node consumes them"，所以这是合理的延迟实施。

---

### 问题 2.4: Manual review gate 未完全实现

**文档暗示** (06_subgraph_wiring_and_cleanup.md):
```text
Expected gates after Phase 8:
- t2t.t2t_final_review
```

**实际实现**:
- ✅ `t2t/nodes.py` 有 `apply_t2t_review_decision()` 函数
- ✅ `write_t2t_report()` 可以进入 `blocked_human` 状态
- ⚠️  **但** `t2t/builder.py` graph **没有显式的 review gate 节点**
- ✅ 通过 `route_after_report()` 检查 `supervision_state == "blocked_human"` 实现隐式 gate
- ✅ `_block_for_t2t_review()` 函数存在并在 manual_review 模式下调用

**状态**: 
- 基础的 manual review 功能**已实现**
- Review gate 通过状态检查而非显式节点实现（与 assembly_review 风格不同）
- 功能完整，但架构风格与其他 stage 不统一

**这是问题吗?** → **设计风格差异**，功能上可用，但可能需要统一架构模式。

---

## 3. 真正的待实施功能

### 3.1 Inventory Profile 增强

**文档**: `02_inventory.md` - 任务状态：待校正实施

**规划功能**:
```python
{
    "data_profile": {
        "has_ont": bool,
        "has_cyclone": bool,
        "has_hifi": bool,
        "has_hic": bool,
        ...
    },
    "assembly_hints": {
        "route": "cyclone | ont | hifi | auto",
        "method": "flye | hifiasm_ont | ...",
    },
    "stage_availability": {
        "hic_scaffolding": bool,
        "t2t": bool,
        ...
    },
}
```

**当前状态**:
- ⚠️  `assembly` stage 有 `resolve_extended_inputs` / `infer_input_profile`（局部解决方案）
- ❌ `inventory` stage 未输出结构化 profile hints
- ❌ 后续 stages 仍从 asset registry 直接读取，而非消费 inventory hints

**优先级**: 中 - 当前 workaround 可用，但不是最佳架构

---

### 3.2 T2T Gap Survey

**文档**: 01_schema_and_knowledge_packs.md 提到 `T2T_GAP_SURVEY`

**规划功能**:
- Assembly 中的 N-gap 统计
- Gap size distribution
- Gap-near-telomere analysis

**当前状态**:
- ❌ 完全未实现
- ❌ 代码中无 "gap" 相关逻辑
- ❌ 无 gap survey artifact

**优先级**: 低 - 对于 near_t2t/strict_t2t 判断可能有用，但非必需

---

### 3.3 LLM-based T2T Review Task Generation

**文档**: 01_schema_and_knowledge_packs.md 描述 `T2TReviewTasks`

**规划功能**:
- 基于 telomere/gap 证据生成人工 review checklist
- 与 assembly_review 的 task generation 风格一致

**当前状态**:
- ❌ 未实现
- ⚠️  `assembly/review_task_guide` knowledge pack 存在但未被 t2t 消费
- ✅ 当前报告是静态 Markdown，非动态 task list

**优先级**: 低-中 - 有用但非阻塞功能

---

### 3.4 Hi-C Curation Workspace

**文档**: 04_hic_scaffolding_stage.md "Curation Workspace Extension"

**规划功能**:
```python
HIC_SCAFFOLDING_WORKSPACE = "hic_scaffolding.workspace"
HIC_SCAFFOLDING_CANDIDATE = "hic_scaffolding.candidate"
HIC_SCAFFOLDING_DECISION = "hic_scaffolding.decision"
```

**当前状态**:
- ✅ `HiCScaffoldPlan` artifact 存在
- ✅ `HiCScaffoldDecisionArtifact` 存在
- ❌ 没有专门的 "workspace" artifact
- ⚠️  Juicebox manual curation 路径规划了但未验证

**优先级**: 低 - 当前 YaHS 和 3D-DNA 自动化路径可用

---

### 3.5 Assembly Method Provenance 完善

**文档**: 01_schema_and_knowledge_packs.md, 03_assembly_stage_methods.md

**规划功能**:
- 所有 candidate bundles 记录 method provenance
- `assembly_method` 字段规范化
- hifiasm-ONT / hifiasm-HiFi 路径完整实现

**当前状态**:
- ✅ Flye method 已下沉到 `method_application/flye.py`
- ✅ NextDenovo method 已下沉到 `method_application/nextdenovo.py`
- ❌ hifiasm-ONT / hifiasm-HiFi 尚未实现
- ⚠️  Method provenance 字段可能不完整

**优先级**: 中 - 03_assembly_stage_methods.md 说"部分实施"是准确的

---

## 4. 非问题项（设计合理）

### 4.1 Conservative Auto-patch Only

**设计选择**: 当前 T2T stage 只做 **conservative auto-patch**，不做激进的 telomere completion。

**合理性**:
- ✅ 文档明确说"conservative auto-patch"
- ✅ 阈值保守：≥2 reads、MAPQ≥20、overlap≥500bp
- ✅ 冲突时不 patch，只输出 candidates
- ✅ 05_t2t_stage.md 明确说不做"Verkko/HERRO/Dorado/Medaka/Teloclip/TeloComp 的真实执行"

**非问题**。

---

### 4.2 T2T Stage 使用状态检查而非显式 Review Gate 节点

**设计选择**: `t2t/builder.py` 通过 `route_after_report()` 检查 `supervision_state` 而非显式 gate 节点。

**对比**:
- `assembly_review`: 有显式 `gate_review` 节点
- `hic_scaffolding`: 有显式 gate 逻辑
- `t2t`: 隐式，通过状态检查

**合理性**:
- ⚠️  架构风格不一致
- ✅ 功能上等价
- ⚠️  未来维护可能困惑

**可能需要统一**，但不是功能缺陷。

---

### 4.3 HPC 测试中 Juicer 环境依赖缺失

**现象**: HPC test 06 显示 Juicer 需要 Java 和 helper scripts。

**根因**: 
- 这是**环境配置问题**，不是代码缺陷
- YaHS 路径在 HPC 测试中通过了
- Schema 和 planning 路径都验证通过

**非问题**。文档已添加 Implementation note。

---

## 5. 优先级建议与 Backlog

### ✅ 已完成（2026-06-28）
- 文档漂移修复（hic output keys、t2t input fallback、stage order、test status）
- 添加 Implementation notes 说明设计与实现差异
- README.md 任务列表状态更新
- 创建此 backlog 文档
- **T2T Manual Review 显式 Gate Node（Backlog #1）** ⭐
- **统一 Stage Output Contract（新增 `final_assembly_for_downstream`）** ⭐⭐
- **中心化 Assembly Resolver（Backlog #1.6）** ⭐⭐⭐

### 🔧 轻量架构统一

#### ~~Backlog Item #1: T2T Manual Review 显式 Gate Node~~ ✅ 已完成
**收益**: 高 - 架构风格统一，维护清晰  
**风险**: 低 - 只改 graph 结构，逻辑不变  
**工作量**: 小 - 1-2 小时

**实施摘要**（2026-06-28）:
- ✅ 新增 `gate_t2t_review` node（提取 blocking 逻辑）
- ✅ 新增 `route_after_gate` routing function
- ✅ 重构 `write_t2t_report` 为纯 report 生成（不含 blocking）
- ✅ Graph 路径: `report → gate_review → [block|commit]`
- ✅ 16 个现有测试全部通过（包括 5 个 manual review 测试）
- ✅ 架构风格现在与 `assembly_review` 和 `hic_scaffolding` 统一

**改动文件**:
- `app/stages/t2t/builder.py`: 添加 gate node 和 routing
- `app/stages/t2t/nodes.py`: 拆分 `write_t2t_report` 和新增 `gate_t2t_review`
- `app/stages/t2t/routing.py`: 添加 `route_after_gate`，重构 `route_after_report`

---

#### ~~Backlog Item #1.5: 统一 Stage Output Contract~~ ✅ 已完成
**收益**: 高 - 清晰的 stage 间 contract，简化输入逻辑  
**风险**: 低 - 向后兼容，保留所有 legacy keys  
**工作量**: 小 - 2 小时

**实施摘要**（2026-06-28）:
- ✅ 新增统一 output key: `final_assembly_for_downstream`
- ✅ 所有 assembly-producing stages 同时写新 key 和 legacy keys
- ✅ hic_scaffolding 输出: `final_assembly_for_downstream` + `final_assembly_for_annotation` (legacy)
- ✅ t2t 输出: `final_assembly_for_downstream` + `final_assembly_for_annotation` (legacy)
- ✅ t2t 输入: 优先搜索 `final_assembly_for_downstream`，fallback legacy keys
- ✅ 32 个测试全部通过（16 t2t + 16 hic_scaffolding）
- ✅ 完全向后兼容，不破坏现有代码

**改动文件**:
- `app/stages/hic_scaffolding/nodes.py`: 4 处添加 `final_assembly_for_downstream`
- `app/stages/t2t/nodes.py`: 4 处添加 `final_assembly_for_downstream` + 重构 `_assembly_ref` 搜索逻辑
- `docs/phases/phase8/STAGE_OUTPUT_CONTRACT.md`: 新增统一 contract 设计文档

**详细设计**: 见 `STAGE_OUTPUT_CONTRACT.md`

---

### 中优先级（架构完善）

#### Backlog Item #2: Inventory Profile Hints
**收益**: 中 - 解耦 assembly 的 route selection prelude  
**风险**: 中 - 影响多个 stage 的输入逻辑  
**工作量**: 中 - 需要 inventory 和 assembly 协同改动

见 02_inventory.md 设计。当前 workaround (`assembly/resolve_extended_inputs`) 可用。

#### Backlog Item #3: Assembly Method Provenance 完善
**收益**: 中 - 支持 hifiasm-ONT/HiFi 路径  
**风险**: 低 - 增量添加，不破坏现有 Flye/NextDenovo 路径  
**工作量**: 中 - 每个方法需要 descriptor + method_application graph + tests

见 03_assembly_stage_methods.md。

#### Backlog Item #4: Knowledge Packs 迁移
**收益**: 低 - 命名规范化  
**风险**: 低 - 只是文件移动和 import 路径更新  
**工作量**: 小 - 但需要确保 descriptor registry 更新

```bash
# 目标
descriptors/knowledge_packs/assembly/t2t_criteria → descriptors/knowledge_packs/t2t/criteria
descriptors/knowledge_packs/assembly/review_task_guide (保留) + t2t/review_task_guide (新增)
```

---

### 低优先级（增强功能）

#### Backlog Item #5: T2T Gap Survey
**收益**: 低 - 对 strict_t2t 判断可能有用  
**工作量**: 中 - 需要 N-gap parsing + artifact registration + tests

#### Backlog Item #6: LLM-based T2T Review Task Generation
**收益**: 低 - 提升 UX，但非必需  
**工作量**: 中 - 需要 LLM integration + task schema + tests

#### Backlog Item #7: T2T Readiness Confidence Scoring
**收益**: 低 - 从 boolean 升级为 scored verdict  
**工作量**: 中 - 需要 scoring logic + T2TReadiness model + tests

#### Backlog Item #8: Hi-C Curation Workspace Artifacts
**收益**: 低 - 当前 YaHS/3D-DNA 自动化路径够用  
**工作量**: 中 - Juicebox manual curation 路径验证

---

## 6. 建议的文档更新

### 6.1 更新 01_schema_and_knowledge_packs.md

在 T2T Artifacts 段落添加：

```markdown
**Implementation Status (2026-06-28)**:

已实现 artifacts:
- `T2T_ASSEMBLY_TELOMERE_SCAN`, `T2T_READS_TELOMERE_SCAN`
- `T2T_TELOMERIC_READS`, `T2T_EXTENSION_CANDIDATES`
- `T2T_STATUS_SUMMARY`, `T2T_FINISHING_REPORT`
- `T2T_PATCHED_ASSEMBLY`, `T2T_PATCH_RECORDS`
- `T2TFinishingBundle`, `T2TReviewDecisionArtifact`

待实施增强 artifacts:
- `T2T_READINESS` (综合 readiness 评估)
- `T2T_REVIEW_TASKS` (LLM-generated tasks)
- `T2T_GAP_SURVEY` (N-gap 分析)
- `T2TReadiness`, `T2TReviewTasks` models

Legacy prototypes:
- `AssemblyT2TReadiness`, `AssemblyReviewTasks` (保留向后兼容)
```

### 6.2 更新 README.md 任务列表

```markdown
- `[x] 05_t2t_stage.md`：新增 top-level `t2t` stage（MVP 已实现，增强功能见 DESIGN_ISSUES_AND_TODO.md）。
- `[x] 06_subgraph_wiring_and_cleanup.md`：更新 stage order、parent graph wiring、t2t stage 接入（核心完成，knowledge packs 待迁移）。
```

### 6.3 更新 06_subgraph_wiring_and_cleanup.md 任务状态

```markdown
任务状态：核心已实施，知识包迁移和架构统一待后续。
```

---

## 7. 结论

**T2T stage 的核心功能（telomere scan + conservative patch）已完整实现并通过测试。** 当前状态是：

- ✅ **MVP 可用**: scan → align → patch → report 流程完整
- ✅ **测试覆盖**: 16 unit tests + HPC smoke test
- ✅ **Stage 集成**: STAGE_ORDER 和 parent graph 已接入
- ⚠️  **文档漂移**: 部分文档描述的增强功能未实现（已在本次修复中添加说明）
- 📋 **待实施**: Gap survey、LLM review tasks、inventory hints、knowledge packs 迁移

**不存在阻塞性设计问题**。文档中标记为"待实施"的大部分是合理的增强功能规划，不是当前实现的缺陷。建议将 README.md 任务状态更新为反映实际进展。
