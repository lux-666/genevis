# Phase 8 & 8.5: Assembly Resolver, Stage Contract, Universal Resource Resolver

**状态**: ✅ Phase 8 完成，Phase 8.5 核心架构完成  
**日期**: 2026-06-28

---

## 概述

Phase 8 统一了 stage 边界和输入解析逻辑，Phase 8.5 将其泛化为通用 Resource Resolver 并为 TUI 提供执行前验证 API。

### Stage 模型

```text
inventory
read_qc
genome_survey
assembly
assembly_dedup
assembly_review
hic_scaffolding
t2t                    # Phase 8 新增
repeat_annotation
gene_annotation
function_annotation
```

### Phase 8 核心成就

1. ✅ **中心化 Assembly Resolver** - 统一资源解析逻辑
2. ✅ **T2T Manual Review Gate Node** - 显式 gate 架构
3. ✅ **统一 Stage Output Contract** - unified + legacy keys
4. ✅ **重构 4 个 Stages** - hic/t2t/repeat/gene 使用新 resolver
5. ✅ **修复 Scenario 4 Bugs** - 用户自带 assembly 场景
6. ✅ **79/79 测试通过** - 零回归

### Phase 8.5 核心成就

1. ✅ **Universal Resource Resolver** - 泛化为所有资源类型
2. ✅ **Preflight Check API** - TUI 执行前验证
3. ✅ **Resource Specs Registry** - 声明式资源需求
4. ✅ **向后兼容** - 保留 legacy API
5. ✅ **LLM Integration 预留** - 知识库接口

---

## 核心文档

### 设计与实施
- **[DESIGN_SUMMARY.md](DESIGN_SUMMARY.md)** - Phase 8 & 8.5 设计总结
- **[PHASE_8.5_DESIGN.md](PHASE_8.5_DESIGN.md)** - Phase 8.5 详细设计和实施计划
- **[PHASE_8.5_PROGRESS.md](PHASE_8.5_PROGRESS.md)** - 当前进度和剩余工作

### 实施步骤（Phase 8）

### 实施步骤（Phase 8）

1. **[01_schema_and_knowledge_packs.md](01_schema_and_knowledge_packs.md)** - Schema 和知识包设计
2. **[02_inventory.md](02_inventory.md)** - Inventory stage 增强
3. **[03_assembly_stage_methods.md](03_assembly_stage_methods.md)** - Assembly stage 多方法支持
4. **[04_hic_scaffolding_stage.md](04_hic_scaffolding_stage.md)** - HiC scaffolding stage 增强
5. **[05_t2t_stage.md](05_t2t_stage.md)** - T2T stage 新增（✅ 完成）
6. **[06_subgraph_wiring_and_cleanup.md](06_subgraph_wiring_and_cleanup.md)** - Stage wiring 和清理（✅ 完成）

### 归档讨论

设计讨论和重构过程文档已移至 **[archive/](archive/)** 目录：
- `ASSEMBLY_RESOLUTION_REFACTOR.md` - Assembly resolver 重构讨论
- `RETHINK_RESOLVER_SCOPE.md` - Resolver 范围重新思考
- `WHY_UPSTREAM_NO_RESOLVER.md` - 早期错误分析
- `STAGE_OUTPUT_CONTRACT.md` - Stage 输出契约设计
- `REFACTOR_COMPLETE.md` - Phase 8 完成记录
- `DESIGN_ISSUES_AND_TODO.md` - 设计问题和待办事项

---

## 核心架构

### 三层控制架构

**Layer 1: TUI/Plan 层**
```python
# 用户通过 requested_stages 控制执行流程
validation = validate_execution_plan(state, requested_stages)
if not validation.valid:
    # 显示友好错误和建议
```

**Layer 2: RunConfig 层**
```python
# 通过 local_exec 提供 override 参数
local_exec = {
    "hic_scaffolding": {
        "assembly_fasta_ref": "/path/to/custom.fa"
    }
}
```

**Layer 3: Stage 内部层**
```python
# 使用 resolver 自动查找资源
resolution = resolve_stage_resource(
    state,
    resource_spec=ASSEMBLY_SPEC,
    override=config.get("assembly_fasta_ref")
)
```

### Universal Resource Resolver

```python
# 声明式资源需求
spec = ResourceSpec(
    resource_type="assembly",
    prefer_stages=["t2t", "hic_scaffolding", "assembly_review"],
    output_keys={...},
    asset_types=["assembly_fasta_registered"],
    required=True,
)

# 通用解析
resolution = resolve_stage_resource(state, resource_spec=spec)

# 解析顺序
# 1. RunConfig override
# 2. Preferred stages outputs
# 3. All stages (reverse STAGE_ORDER)
# 4. Asset registry
```

### Preflight Check API

```python
# TUI 执行前验证
validation = validate_execution_plan(
    state,
    requested_stages=["hic_scaffolding", "repeat_annotation"]
)

if not validation.valid:
    print(validation.summary)  # "✗ Cannot run 'hic': missing assembly"
    for suggestion in validation.suggestions:
        print(suggestion)  # "Provide assembly_fasta_registered asset"
```

---

## 实施状态

### ✅ Phase 8 完成
- [x] Assembly Resolver 中心化
- [x] T2T Stage 显式 Gate Node
- [x] 统一 Stage Output Contract
- [x] 重构 hic/t2t/repeat/gene stages
- [x] 修复 scenario 4 bugs
- [x] 79/79 测试通过

### ✅ Phase 8.5 核心架构完成
- [x] Universal Resource Resolver API
- [x] Preflight Check API
- [x] Resource Specs Registry
- [x] 向后兼容保持
- [x] LLM Integration 预留

### ⏸️ Phase 8.5 剩余工作（7-8h）
- [ ] TUI Integration（2h）- 集成 preflight validation
- [ ] Stage 重构（2h）- read_qc, genome_survey 使用 helpers
- [ ] 测试和文档（2h）- Unit tests, integration tests
- [ ] Assembly stage 重构（暂缓）- 复杂度高，需单独规划

---

## 测试覆盖

**当前测试状态**: ✅ 79/79 通过

- 16 hic_scaffolding tests
- 16 t2t tests  
- 18 repeat_annotation tests
- 29 gene_annotation tests

**Preflight API 验证**:
```bash
python -c "from app.stages.preflight import validate_execution_plan; print('✓ API loaded')"
# ✓ 已验证工作正常
```

---

## 关键设计决策

### 1. 统一 Stage Output Contract

每个 stage 输出：
- **Unified key** (未来): `final_<resource_type>_for_downstream`
- **Legacy keys** (当前): stage-specific keys

Resolver 优先检查 unified key，fallback 到 legacy keys。

### 2. 资源解析 4 层 Fallback

1. RunConfig override（最高优先级）
2. Prefer stages outputs（显式优先）
3. All stages in reverse order（自动 fallback）
4. Asset registry（用户提供数据）

### 3. Preflight Check 时机

TUI 流程：
```
1. 询问 goal
2. 生成初始 plan
3. 澄清 stage 选择
4. ✅ **Preflight validation** (新增)
5. 确认执行
6. 运行 stages
```

### 4. LLM Integration 预留

当前 `STAGE_REQUIREMENTS` 硬编码，未来由 LLM + 知识库生成：
```python
def _get_stage_requirements(stage: str):
    # TODO: Query via LLM + knowledge base
    # Query: "What resources does {stage} need?"
    return STAGE_REQUIREMENTS.get(stage, [])
```

---

## 文件结构

### 核心实现
- `app/stages/base.py` (~320 行) - Universal resolver
- `app/stages/preflight.py` (~200 行) - TUI validation API
- `app/stages/resource_specs.py` (~150 行) - Resource specs registry

### 重构的 Stages
- `app/stages/hic_scaffolding/nodes.py` - 使用 resolver
- `app/stages/t2t/nodes.py` - 使用 resolver
- `app/stages/repeat_annotation/nodes.py` - 使用 resolver
- `app/stages/gene_annotation/nodes.py` - 使用 resolver

---

## 对外接口

### Stage 输出契约

```python
# assembly_review
{
    "final_assembly_for_scaffolding_or_annotation": "path/to/reviewed.fa",
    "final_assembly_ref": "path/to/reviewed.fa",
}

# hic_scaffolding
{
    "final_assembly_for_annotation": "path/to/hic.fa",
    "chromosome_scaffolded_assembly_candidate_ref": "path/to/hic.fa",
}

# t2t
{
    "final_assembly_for_annotation": "path/to/t2t.fa",
}
```

### 路由流程

```text
inventory
  → read_qc
  → genome_survey
  → assembly (方法选择 + method_application subgraph)
  → assembly_dedup
  → assembly_review
  → hic_scaffolding (可选，基于 target/data)
  → t2t (可选，Phase 8 新增)
  → repeat_annotation
  → gene_annotation
  → function_annotation
```

---

## 经验教训

### 做得好的
1. ✅ 渐进式重构 - 先 assembly resolver，再泛化
2. ✅ 向后兼容 - 保留 legacy API，新旧并存
3. ✅ API First - 先定义接口，再填充实现
4. ✅ LLM Ready - 预留扩展点，标记 TODO

### 可以改进的
1. ⚠️ 文档 drift - 实施时未及时更新（后期修复）
2. ⚠️ 早期错误判断 - 认为只有下游需要 resolver（后期纠正）
3. ⚠️ 过度设计倾向 - 最初想完全统一输出 key（简化为渐进式）

---

## 下一步

### Phase 8.5 剩余工作
1. **TUI Integration** (2h) - 修改 `ask_interactive.py`，集成 preflight validation
2. **Stage 重构** (2h) - read_qc, genome_survey 使用新 helpers
3. **测试和文档** (2h) - Unit tests, integration tests

### Phase 9+ 展望
1. Assembly stage 完整重构
2. Unified output keys 全面实施
3. LLM-driven requirements generation
4. Knowledge base integration

---

## 参考文档

### 核心设计
- [DESIGN_SUMMARY.md](DESIGN_SUMMARY.md) - 设计总结和经验教训
- [PHASE_8.5_DESIGN.md](PHASE_8.5_DESIGN.md) - 详细设计和实施计划
- [PHASE_8.5_PROGRESS.md](PHASE_8.5_PROGRESS.md) - 当前进度

### 实施步骤
- [01_schema_and_knowledge_packs.md](01_schema_and_knowledge_packs.md)
- [02_inventory.md](02_inventory.md)
- [03_assembly_stage_methods.md](03_assembly_stage_methods.md)
- [04_hic_scaffolding_stage.md](04_hic_scaffolding_stage.md)
- [05_t2t_stage.md](05_t2t_stage.md)
- [06_subgraph_wiring_and_cleanup.md](06_subgraph_wiring_and_cleanup.md)

### 归档讨论
- [archive/](archive/) - 设计讨论和重构过程文档

