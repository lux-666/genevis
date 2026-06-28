# 为什么前面的 Stage 不需要 Assembly Resolver？

## 问题

用户问：assembly_review、assembly_dedup 等前面的环节为什么没有用 `resolve_current_assembly()` resolver？

## 答案：它们不需要！

### 原因分析

#### 1. Assembly、Assembly_dedup、Assembly_review 的特殊性

这些 stage 是 **assembly 的生产者**，不是消费者：

```
assembly → assembly_dedup → assembly_review
    ↓           ↓                ↓
  产出草稿    可选去冗余      人工审查选择最终版
```

**关键差异**：
- **assembly**: 从 reads 产出 draft assembly（生产者）
- **assembly_dedup**: 从 assembly 的输出去冗余（内部处理）
- **assembly_review**: 从 assembly 的多个 candidates 中选择（内部处理）
- **hic/t2t/repeat/gene**: 消费 "最终 assembly"（消费者）

#### 2. 实际代码验证

**assembly_review** (`resolve_candidates`):
```python
def resolve_candidates(state: TopLevelState) -> dict:
    """Resolve available assembly candidates from prior stage outputs."""
    stage_outputs = state.get("stage_outputs") or {}
    
    # 1. Check for existing assembly route from inventory
    inventory_out = stage_outputs.get("inventory", {})
    entry_mode = inventory_out.get("entry_mode")
    
    # 2. Get candidates from assembly stage
    assembly_out = stage_outputs.get("assembly", {})
    candidate_refs = assembly_out.get("selected_candidate_refs", [])
    ...
```

**特点**：
- ✅ 直接从 `assembly` stage 拿 candidates（唯一来源）
- ✅ 支持 `inventory` 的 existing assembly route
- ❌ **不需要** fallback 到 asset registry（因为 assembly 阶段不能跳过）

**assembly_dedup** (`plan_purge_dups`):
```python
def plan_purge_dups(state: TopLevelState) -> dict:
    assembly_out = dict((state.get("stage_outputs") or {}).get("assembly") or {})
    candidate = _selected_assembly_candidate(state)
    ...
```

**特点**：
- ✅ 直接从 `assembly` stage 拿 selected candidate（唯一来源）
- ❌ **不需要** fallback（因为 assembly 阶段不能跳过）

### 3. 为什么下游需要 Resolver？

**下游 stages（hic/t2t/repeat/gene）的特殊性**：

```
场景 1: 完整流程
assembly → assembly_review → hic → t2t → repeat → gene
  ✓          ✓               ✓     ✓      ✓       ✓

场景 2: 跳过 assembly（用户自带）
inventory (existing assembly) → hic → t2t → repeat → gene
                                 ✓     ✓      ✓       ✓
                                 
场景 3: 跳过 hic
assembly → assembly_review → t2t → repeat → gene
  ✓          ✓                ✓      ✓       ✓

场景 4: 跳过 assembly + hic（用户自带）
inventory (existing assembly) → t2t → repeat → gene
                                 ✓      ✓       ✓
```

**关键问题**：
- ❓ 场景 2、4 中，hic/t2t/repeat/gene 如何拿到 assembly？
- ✅ **答案**：通过 resolver 的 `asset_registry` fallback！

### 4. 是否需要扩展 Resolver？

#### assembly_review 需要吗？

**不需要！** 理由：

1. **Assembly 阶段不能跳过**（只要做 genome assembly，必须运行）
2. **Existing assembly 有专门路径**：通过 `inventory.entry_mode == "existing_assembly"` 处理
3. **单一来源**：assembly_review 只从 `assembly` stage 或 `inventory` existing route 拿数据

#### assembly_dedup 需要吗？

**不需要！** 理由：

1. **可选 stage**：如果没有 dedup 需要，直接跳过
2. **紧耦合**：dedup 是 assembly 的后处理，只能从 assembly 拿输入
3. **不支持跳过场景**：dedup 不应该从 asset registry 拿 assembly（那就不是 dedup 了）

### 5. 设计原则总结

| Stage | 角色 | 输入来源 | 是否需要 Resolver |
|-------|------|----------|-------------------|
| assembly | 生产者 | reads | ❌ 不需要 |
| assembly_dedup | 后处理 | assembly 输出（唯一） | ❌ 不需要 |
| assembly_review | 审查者 | assembly candidates | ❌ 不需要 |
| hic_scaffolding | 消费者 | assembly_review **或** registry | ✅ 需要 |
| t2t | 消费者 | hic **或** assembly_review **或** registry | ✅ 需要 |
| repeat_annotation | 消费者 | t2t **或** hic **或** assembly_review **或** registry | ✅ 需要 |
| gene_annotation | 消费者 | 同上 | ✅ 需要 |

**规律**：
- **生产者和后处理 stage**：输入来源单一，不需要 resolver
- **消费者 stage**：输入来源多样（支持跳过场景），需要 resolver

### 6. 未来可能的扩展

如果将来有这些场景，可能需要扩展：

**场景 A**：用户跳过 assembly，直接提供 "已去冗余的 assembly"
- 那么 assembly_dedup 可能需要 resolver
- 但目前不支持这个场景

**场景 B**：用户跳过 assembly + assembly_review，直接提供 "已审查的 final assembly"
- 那么 assembly_review 可能需要 resolver
- 但当前 existing assembly 通过 inventory 专门路径处理，不需要

## 结论

**前面的 stage 不需要 resolver，因为**：
1. ✅ 它们是 assembly 的生产者/后处理，不是消费者
2. ✅ 输入来源单一且确定（assembly stage 输出）
3. ✅ 不支持跳过场景（assembly 阶段是必需的）
4. ✅ Existing assembly 有专门的 inventory route

**只有消费者 stages（hic/t2t/repeat/gene）需要 resolver**，因为它们：
1. ✅ 支持多种跳过场景组合
2. ✅ 可能从多个上游 stage 或 asset registry 获取输入
3. ✅ 需要统一的 fallback 逻辑

**当前设计是合理的！** 🎯
