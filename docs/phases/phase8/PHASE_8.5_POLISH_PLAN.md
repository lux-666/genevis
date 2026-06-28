# Phase 8.5 Polish Plan

**日期**: 2026-06-28  
**状态**: 📋 Planning  
**预计工作量**: 3-4 小时

---

## 背景

Phase 8.5 核心功能已完成并通过所有测试，但有一些小的改进点可以提升文档完整性、测试覆盖率和用户理解度。

这些都是 **非阻塞性** 的改进，不影响当前功能正常使用。

---

## 问题清单

### 1. Resource Specs Registry Hardcoded ⚠️ 已知问题
**状态**: 已在代码中标记 TODO，未来由 LLM 驱动  
**优先级**: 低（Phase 9+ 处理）  
**当前方案**: Hardcoded `STAGE_REQUIREMENTS` 足够使用

**不需要立即行动**，已有清晰的迁移路径。

---

### 2. Preflight Validation is Permissive 📖 需要文档说明
**状态**: 行为正确，但需要明确说明权衡  
**优先级**: 中  
**预计时间**: 30 分钟

**问题**:
- Validation 失败不阻塞执行（用户可选择 "proceed anyway"）
- 这是有意的设计（TUI 灵活性），但需要文档说明为什么

**行动项**:
```markdown
# Task 2.1: 在 PHASE_8.5_DESIGN.md 添加 "Validation Philosophy" 章节

## Validation Philosophy

Phase 8.5 的 Preflight Validation 采用 **建议式验证**，而非强制验证：

### 为什么允许 "Proceed Anyway"？

1. **高级用户场景**: 用户可能有额外的数据来源（未在 manifest 中注册）
2. **测试和调试**: 开发者可能想看到失败路径的具体错误信息
3. **渐进式数据准备**: 某些资源可能在运行时动态生成

### 权衡

✅ **优点**:
- 不阻塞高级用户的灵活使用
- 支持边缘场景和实验性工作流
- TUI 保持友好和非强制

⚠️ **缺点**:
- 用户可能忽略警告继续执行（然后在运行时失败）
- 需要用户理解 "proceed anyway" 的风险

### 未来改进方向

- 添加 `--strict` 模式：强制要求所有资源验证通过
- 记录用户选择 "proceed anyway" 的次数，提示风险
- 提供 "试运行" 模式：模拟执行但不实际运行
```

---

### 3. Edge Case: Multiple Stages Producing Same Resource 📖 需要文档说明
**状态**: 行为正确但隐式，需要明确文档  
**优先级**: 中  
**预计时间**: 45 分钟

**问题**:
- Layer 3 auto-fallback 使用 reverse STAGE_ORDER
- 行为是确定性的，但用户可能不理解为什么选择了某个 stage 的输出

**行动项**:

```markdown
# Task 3.1: 在 PHASE_8.5_DESIGN.md 添加 "Resource Resolution Priority" 章节

## Resource Resolution Priority

当多个 stages 都能提供同一个资源时，`resolve_stage_resource()` 按以下优先级选择：

### Priority Order (高 → 低)

1. **RunConfig Override** (Layer 2)
   - 用户显式指定的路径
   - 例: `local_exec = {"hic_scaffolding": {"assembly_fasta_ref": "/custom.fa"}}`
   - 🎯 最高优先级，跳过所有自动解析

2. **Preferred Stages** (Layer 3a)
   - ResourceSpec 中声明的 `prefer_stages`
   - 按列表顺序检查
   - 例: `prefer_stages=["t2t", "hic_scaffolding", "assembly_review"]`

3. **Auto-Fallback** (Layer 3b)
   - 如果 preferred stages 都没找到，自动检查所有 stages
   - 按 **reverse STAGE_ORDER** 顺序（后执行的 stage 优先）
   - 🔑 关键: 最近执行的 stage 通常产生最"新鲜"的输出

4. **Asset Registry** (Layer 3c)
   - 用户在 manifest 中注册的 assets
   - 最后的 fallback
   - 例: `assembly_fasta_registered`

### Example: Assembly Resolution

假设执行了这些 stages（按顺序）:
1. `assembly` → 产生 draft assembly
2. `assembly_review` → 产生 reviewed assembly
3. `hic_scaffolding` → 产生 scaffolded assembly
4. `t2t` → 产生 chromosome-level assembly

现在 `repeat_annotation` 需要 assembly:

**ASSEMBLY_SPEC 配置**:
```python
ASSEMBLY_SPEC = ResourceSpec(
    prefer_stages=["t2t", "hic_scaffolding", "assembly_review"],
    ...
)
```

**解析过程**:
1. 检查 `prefer_stages[0] = "t2t"` → ✅ 找到，使用 t2t 输出
2. (不再继续检查后续)

**如果 t2t 没运行**:
1. 检查 `prefer_stages[0] = "t2t"` → ❌ 未运行
2. 检查 `prefer_stages[1] = "hic_scaffolding"` → ✅ 找到，使用 hic 输出

**如果 prefer_stages 都没运行**:
1. Auto-fallback: 按 reverse STAGE_ORDER 检查
2. 顺序: `t2t` (skip) → `repeat_annotation` (skip) → `hic_scaffolding` (skip) → `assembly_review` → ✅ 找到

### 为什么用 Reverse STAGE_ORDER？

**原因**: 后执行的 stage 通常是数据的"精炼版本"

- `assembly` → draft (粗糙)
- `assembly_review` → reviewed (审核后)
- `hic_scaffolding` → scaffolded (染色体级)
- `t2t` → chromosome-level (端粒到端粒)

如果用正序，会优先选择 draft assembly（不理想）。
```

---

### 4. Test Coverage for Preflight Could Expand 🧪 增强测试
**状态**: 当前 7 个测试覆盖核心场景，可扩展边界情况  
**优先级**: 低-中  
**预计时间**: 1-1.5 小时

**当前覆盖**:
- ✅ 单个资源缺失
- ✅ 单个资源存在（registry）
- ✅ 单个资源存在（upstream stage）
- ✅ 执行计划验证（全通过）
- ✅ 执行计划验证（有 blocker）
- ✅ 第一个 blocker 识别
- ✅ 混合结果（部分通过 + 部分失败）

**缺失的边界情况**:
- ❌ Stage 需要多个资源，部分缺失
- ❌ Optional 资源缺失（应该 warn 但不阻塞）
- ❌ Suggestion 生成的边界情况
- ❌ Multiple asset_types 的 fallback

**行动项**:

```python
# Task 4.1: 添加测试到 tests/test_preflight.py

class PreflightEdgeCaseTests(unittest.TestCase):
    """Edge cases for preflight validation"""
    
    def test_stage_with_multiple_required_resources_partial_missing(self):
        """Test stage needing multiple resources with some missing"""
        # gene_annotation 需要 assembly + masked_assembly
        # 提供 assembly，缺失 masked_assembly
        state = initial_state()
        state.stage_outputs = {
            "assembly_review": {
                "final_assembly_ref": "data/assembly.fa"
            }
            # masked_assembly 缺失
        }
        
        check = check_stage_prerequisites(state, "gene_annotation")
        
        # 应该识别 masked_assembly 缺失
        assert not check.can_run
        assert len(check.missing) == 1
        assert check.missing[0].resource_name == "masked assembly"
        # 但 assembly 应该找到
        # (目前 API 不返回 found 资源列表，可考虑扩展)
    
    def test_optional_resource_missing_warns_but_not_blocks(self):
        """Test optional resource missing generates warning, not blocker"""
        # MASKED_ASSEMBLY_SPEC.required = False
        state = initial_state()
        state.stage_outputs = {
            "assembly_review": {"final_assembly_ref": "data/assembly.fa"}
        }
        
        check = check_stage_prerequisites(state, "gene_annotation")
        
        # 如果只缺 optional resource，应该可以运行
        # 但生成 warning
        if check.can_run:
            assert len(check.warnings) > 0
            assert "masked assembly" in check.warnings[0].lower()
    
    def test_suggestion_generation_for_different_resource_types(self):
        """Test suggestion logic covers all resource types"""
        state = initial_state()
        _write_registry(state, [])  # Empty registry
        
        # Test each major resource type
        test_cases = [
            ("hic_scaffolding", "assembly"),
            ("read_qc", "raw reads"),
            ("gene_annotation", "assembly"),
        ]
        
        for stage, expected_resource in test_cases:
            check = check_stage_prerequisites(state, stage)
            assert not check.can_run
            assert any(expected_resource.lower() in s.lower() 
                      for s in check.suggestions)
    
    def test_multiple_asset_types_fallback(self):
        """Test ResourceSpec with multiple asset_types tries all"""
        # CLEAN_READS_SPEC 有多个 asset_types fallback
        state = initial_state()
        
        # 只提供第二个 asset_type
        _write_registry(state, [
            {"asset_type": "clean_long_reads", "path": "data/clean.fa"}
        ])
        
        resolution = resolve_stage_resource(
            state,
            resource_spec=CLEAN_READS_SPEC,
        )
        
        # 应该找到 clean_long_reads（即使它不是第一个 asset_type）
        assert resolution.found
        assert "clean.fa" in resolution.path
```

**预计新增测试**: 4-6 个，覆盖率提升约 15-20%

---

### 5. Add Docstring Examples 📝 增强文档
**状态**: 当前 docstring 不错，可添加 auto-fallback 示例  
**优先级**: 低  
**预计时间**: 30 分钟

**行动项**:

```python
# Task 5.1: 增强 app/stages/base.py 的 resolve_stage_resource() docstring

def resolve_stage_resource(
    state: TopLevelState,
    *,
    resource_spec: ResourceSpec,
    override: str | None = None,
) -> ResourceResolution:
    """
    通用资源解析器
    
    解析顺序:
    1. RunConfig override (如果提供)
    2. Preferred stages outputs (检查 unified + legacy keys)
    3. All stages in reverse STAGE_ORDER (自动 fallback)
    4. Asset registry (指定的 asset_types)
    
    Args:
        state: 当前状态（包含 stage_outputs 和 asset_registry_ref）
        resource_spec: 资源需求声明
        override: 来自 RunConfig 的手动覆盖路径
    
    Returns:
        ResourceResolution 包含 paths, source, metadata
    
    Examples:
        # Example 1: 使用 preferred stages
        >>> spec = ResourceSpec(
        ...     resource_type="assembly",
        ...     prefer_stages=["hic_scaffolding", "assembly_review"],
        ...     output_keys={
        ...         "hic_scaffolding": ["final_assembly_for_annotation"],
        ...         "assembly_review": ["final_assembly_ref"],
        ...     },
        ... )
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "hic_scaffolding" (如果运行过)
        
        # Example 2: Auto-fallback 到非 preferred stage
        >>> # 假设只运行过 assembly_dedup (不在 prefer_stages 中)
        >>> state.stage_outputs = {
        ...     "assembly_dedup": {
        ...         "deduplicated_assembly_ref": "data/dedup.fa"
        ...     }
        ... }
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "assembly_dedup" (auto-fallback)
        >>> print(resolution.source_type)  # "stage_output"
        
        # Example 3: Fallback 到 asset registry
        >>> # 没有任何 stage 输出
        >>> state.stage_outputs = {}
        >>> # 但 manifest 中有注册的 assembly
        >>> resolution = resolve_stage_resource(state, resource_spec=spec)
        >>> print(resolution.source)  # "asset_registry"
        >>> print(resolution.source_type)  # "asset"
        
        # Example 4: RunConfig override 跳过所有自动解析
        >>> resolution = resolve_stage_resource(
        ...     state,
        ...     resource_spec=spec,
        ...     override="/custom/path/assembly.fa"
        ... )
        >>> print(resolution.source)  # "run_config"
        >>> print(resolution.path)  # "/custom/path/assembly.fa"
    """
```

---

## 实施计划

### Phase 1: 文档完善（推荐立即完成）

**预计时间**: 1.5 小时  
**价值**: 高（帮助用户理解设计决策）

- [ ] Task 2.1: 添加 "Validation Philosophy" 章节（30 分钟）
- [ ] Task 3.1: 添加 "Resource Resolution Priority" 章节（45 分钟）
- [ ] Task 5.1: 增强 `resolve_stage_resource()` docstring examples（30 分钟）

**交付物**:
- 更新 `PHASE_8.5_DESIGN.md`
- 更新 `app/stages/base.py`

---

### Phase 2: 测试增强（可选，建议下次迭代）

**预计时间**: 1.5 小时  
**价值**: 中（提升鲁棒性，但当前覆盖已足够）

- [ ] Task 4.1: 添加 4-6 个 edge case 测试（1.5 小时）

**交付物**:
- 更新 `tests/test_preflight.py`
- 测试覆盖率从 7 tests → 11-13 tests

---

### Phase 3: 未来增强（Phase 9+）

**预计时间**: 未定  
**价值**: 高（长期改进）

- [ ] LLM-driven requirements 替换 hardcoded registry
- [ ] `--strict` 模式强制验证
- [ ] "试运行" 模式模拟执行
- [ ] Unified output keys 迁移完成
- [ ] Assembly stage 重构

---

## 优先级建议

### 立即执行（本轮）
✅ **Phase 1: 文档完善** (1.5 小时)
- 用户现在就能从清晰的文档中受益
- 不需要代码改动，只需要更新文档
- 帮助高级用户理解 auto-fallback 行为

### 下次迭代
⏸️ **Phase 2: 测试增强** (1.5 小时)
- 当前测试覆盖已足够核心功能
- 边界情况测试更多是"安全网"
- 可以在发现实际 bug 时再添加相应测试

### 长期路线图
🔮 **Phase 3: 未来增强**
- 依赖更大的架构变化
- 需要与 LLM integration、knowledge base 协调
- 当前设计已预留接口

---

## 验收标准

### Phase 1 完成标准
- [ ] `PHASE_8.5_DESIGN.md` 包含 "Validation Philosophy" 章节
- [ ] `PHASE_8.5_DESIGN.md` 包含 "Resource Resolution Priority" 章节
- [ ] `app/stages/base.py` 的 `resolve_stage_resource()` 有 4 个 docstring examples
- [ ] 文档 review 通过

### Phase 2 完成标准
- [ ] `tests/test_preflight.py` 增加 4-6 个 edge case 测试
- [ ] 所有新测试通过
- [ ] 测试覆盖率报告显示提升

---

## 风险与缓解

### 风险 1: 文档过于详细，用户反而困惑
**缓解**: 
- 使用清晰的层级结构（priority order 列表）
- 提供具体示例（而非抽象描述）
- 添加 TL;DR 小节

### 风险 2: 测试用例过于边缘，增加维护负担
**缓解**:
- 只添加有实际价值的测试（对应真实用户场景）
- 每个测试写清楚 "为什么需要这个测试"
- 如果难以构造测试场景，可能说明该边界情况不重要

---

## 总结

Phase 8.5 核心功能已完成，这些 polish 工作都是**锦上添花**：

- **Phase 1** (文档) - 建议立即完成，性价比最高
- **Phase 2** (测试) - 可以延后到发现实际问题时再补充
- **Phase 3** (未来) - 长期路线图，当前已预留接口

建议本轮完成 **Phase 1**，用 1.5 小时完成文档完善，为用户提供清晰的行为说明。
