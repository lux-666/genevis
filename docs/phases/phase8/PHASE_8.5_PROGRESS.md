# Phase 8.5 进度跟踪

**日期**: 2026-06-28  
**状态**: ✅ 核心架构完成，TUI Integration 和全面重构留待下次  
**文档整理**: ✅ 已完成（归档讨论文档，创建 DESIGN_SUMMARY.md 和 PHASE_8.5_DESIGN.md）

---

## 文档结构整理（✅ 完成）

### 归档的讨论文档
移至 `archive/` 目录：
- `ASSEMBLY_RESOLUTION_REFACTOR.md` - 重构讨论过程
- `RETHINK_RESOLVER_SCOPE.md` - Resolver 范围重新思考
- `WHY_UPSTREAM_NO_RESOLVER.md` - 早期错误分析
- `STAGE_OUTPUT_CONTRACT.md` - 输出契约设计
- `REFACTOR_COMPLETE.md` - Phase 8 完成记录
- `DESIGN_ISSUES_AND_TODO.md` - 设计问题追踪

### 新增的核心文档
- `DESIGN_SUMMARY.md` - Phase 8 & 8.5 设计总结和经验教训
- `PHASE_8.5_DESIGN.md` - Phase 8.5 详细设计和实施计划（包含 API 定义、实施细节、测试策略）

### 更新的文档
- `README.md` - 更新为 Phase 8 & 8.5 总览，清晰的结构和导航

---

## 今天完成的工作

### Phase 8: Assembly Resolver（已完成）
1. ✅ 中心化 Assembly Resolver
2. ✅ T2T Manual Review 显式 Gate Node  
3. ✅ 统一 Stage Output Contract
4. ✅ 重构 4 个 stage（hic/t2t/repeat/gene）
5. ✅ 修复 3 个 bug（场景 4 支持）
6. ✅ 79/79 测试通过

### Phase 8.5: Universal Resource Resolver（架构完成）
1. ✅ **核心 API 设计**
   - `ResourceSpec` dataclass - 声明式资源需求
   - `ResourceResolution` dataclass - 统一返回类型
   - `resolve_stage_resource()` - 通用 resolver
   - `load_assets_from_registry()` - Helper 函数

2. ✅ **Preflight Check API**
   - `check_stage_prerequisites()` - 单 stage 检查
   - `validate_execution_plan()` - 全流程验证
   - `ResourceCheck`, `StageCheck`, `PlanValidation` - 返回类型

3. ✅ **Resource Specs Registry**
   - `ASSEMBLY_SPEC`, `CLEAN_READS_SPEC`, `RAW_READS_SPEC` - 预定义 specs
   - `STAGE_REQUIREMENTS` - Stage 需求 registry
   - 为 LLM integration 预留接口

4. ✅ **向后兼容**
   - `resolve_current_assembly()` 保留作为 wrapper
   - 所有现有测试通过

---

## 文件清单

### 新增
- `app/stages/preflight.py` (~200 行) - TUI Preflight API
- `app/stages/resource_specs.py` (~150 行) - Resource Specs Registry

### 修改
- `app/stages/base.py` (138 → ~320 行) - 通用 Resource Resolver

---

## 剩余工作（下次完成）

### 1. TUI Integration（2h）
- 修改 `app/entrypoints/ask_interactive.py`
- 集成 `validate_execution_plan()` 调用
- 显示友好错误和建议

### 2. 重构所有 Stages（3-4h）
- read_qc - 使用 `resolve_stage_resource()`
- genome_survey - 使用 `resolve_stage_resource()`
- assembly - 使用 `resolve_stage_resource()`
- 其他 stages 按需重构

### 3. 测试和文档（2h）
- Preflight API 单元测试
- TUI validation 集成测试
- 更新文档

---

## 关键设计

### 三层架构支持

**Layer 1: TUI/Plan**
```python
validation = validate_execution_plan(state, requested_stages)
if not validation.valid:
    # 显示错误，让用户修改 plan
```

**Layer 2: RunConfig**
```python
resolution = resolve_stage_resource(
    state,
    resource_spec=ASSEMBLY_SPEC,
    override=config.get("assembly_fasta_ref")  # RunConfig override
)
```

**Layer 3: Stage 内部**
```python
spec = ResourceSpec(
    prefer_stages=["hic_scaffolding", "assembly_review"],  # Stage preference
    asset_types=["assembly_fasta_registered"],  # Layer 1 fallback
)
```

### LLM Integration 预留

```python
# app/stages/resource_specs.py
STAGE_REQUIREMENTS = {
    "hic_scaffolding": [...],
    # TODO: 将来由 LLM + 知识库生成
}

# app/stages/preflight.py
def _get_stage_requirements(stage: str):
    """TODO: Query via LLM + knowledge base"""
    return STAGE_REQUIREMENTS.get(stage, [])
```

---

## Token 使用情况

- 已使用: ~146K / 200K
- 剩余: ~54K

由于 token 接近上限，今天先到这里。核心架构已完成，下次可以：
1. 继续 TUI Integration
2. 逐步重构所有 stages
3. 完成测试和文档

---

## 下次启动时

运行以下命令验证当前状态：
```bash
# 验证所有测试通过
conda run -n gene_agent pytest tests/test_hic_scaffolding.py tests/test_t2t.py tests/test_repeat_annotation.py tests/test_gene_annotation.py

# 测试 Preflight API
python -c "from app.stages.preflight import validate_execution_plan; print('✓ Preflight API loaded')"
```

然后继续剩余工作。
