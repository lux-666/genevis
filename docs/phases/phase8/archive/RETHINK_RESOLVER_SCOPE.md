# 重新审视：所有 Stage 都需要通用 Resource Resolver

## 你说得对！

每个 stage 确实都是前一个 stage 的消费者，而且都面临"跳过场景"问题。

## 当前问题

### 问题 1: read_qc 和 genome_survey 都在重复查询 asset registry

**read_qc**:
```python
def resolve_raw_long_reads(state):
    registry = json.loads(registry_path.read_text())
    assets = [asset for asset in registry["assets"] if asset["asset_type"] == "raw_long_reads"]
    # 22 行代码重复查询逻辑
```

**genome_survey**:
```python
def resolve_survey_inputs(state):
    registry = json.loads(registry_path.read_text())
    clean_short = _assets_of_type(assets, "clean_short_dna_reads")
    raw_short = _assets_of_type(assets, "raw_short_dna_reads")
    # 30+ 行代码重复查询逻辑
```

**assembly**: （我还没检查，但应该类似）

### 问题 2: 所有 stage 都可能被跳过

```
场景 1: 完整流程
inventory → read_qc → genome_survey → assembly → ... → gene_annotation

场景 2: 跳过 read_qc（用户提供 clean reads）
inventory → genome_survey → assembly → ...

场景 3: 跳过 read_qc + genome_survey
inventory → assembly → ...

场景 4: 跳过所有前置（用户提供 assembly）
inventory → hic_scaffolding → ...

场景 5: 只做注释（用户提供 assembly + repeat masked）
inventory → gene_annotation
```

**当前代码能处理这些场景吗？** → 部分可以，但逻辑分散、重复

## 我之前错在哪里？

我错误地认为：
- ❌ "assembly 是生产者，不是消费者" → **错！它消费 reads**
- ❌ "assembly 不能跳过" → **错！可以跳过（用户自带 assembly）**
- ❌ "只有下游需要 resolver" → **错！所有 stage 都需要**

**正确认识**：
- ✅ **每个 stage 都是消费者**（除了 inventory）
- ✅ **每个 stage 都可能被跳过**（通过 requested_stages）
- ✅ **每个 stage 都需要统一的输入解析逻辑**

## 应该怎么做？

### 方案 A: 扩展当前 Assembly Resolver 为通用 Resource Resolver（推荐）

回到之前讨论的通用方案：

```python
def resolve_stage_input(
    state: TopLevelState,
    *,
    resource_type: str,  # "assembly" | "reads" | "clean_reads" | "masked_assembly"
    override: str | None = None,
    prefer_stages: list[str] | None = None,
    asset_types: list[str] | None = None,
) -> tuple[str | None, str]:
    """通用的 stage 输入资源解析器"""
```

**使用示例**:
```python
# read_qc: 需要 raw reads
reads_ref, source = resolve_stage_input(
    state,
    resource_type="raw_reads",
    asset_types=["raw_long_reads"]
)

# genome_survey: 需要 clean reads（优先）或 raw reads
clean_reads, source = resolve_stage_input(
    state,
    resource_type="clean_reads",
    prefer_stages=["read_qc"],
    asset_types=["clean_long_reads"]
)
if not clean_reads:
    raw_reads, source = resolve_stage_input(
        state,
        resource_type="raw_reads",
        asset_types=["raw_long_reads"]
    )

# assembly: 需要 clean reads（优先）或 raw reads
reads_ref, source = resolve_stage_input(
    state,
    resource_type="clean_reads",
    prefer_stages=["read_qc"],
    asset_types=["clean_long_reads", "raw_long_reads"]
)

# hic_scaffolding: 需要 assembly（已实现）
assembly_ref, source = resolve_current_assembly(...)
```

### 方案 B: 保持当前实现，逐步添加专门的 resolver

- `resolve_current_assembly()` - 已实现
- `resolve_current_reads()` - 待实现
- `resolve_masked_assembly()` - 待实现
- ...

**优点**: 渐进式，每个 resolver 针对特定资源类型优化
**缺点**: 仍然有重复代码，不够通用

### 方案 C: 先观察，按需重构

- 先保持当前实现（assembly resolver 已完成）
- 观察其他 stage 是否真的有"跳过场景"的需求
- 如果有明确需求，再泛化

## 当前状态评估

### 已知的跳过场景需求

| 场景 | 是否支持 | 代码复杂度 |
|------|---------|-----------|
| 用户自带 assembly，跳过 assembly stages | ✅ 已支持（通过 inventory.entry_mode） | 中 |
| 用户自带 clean reads，跳过 read_qc | ❓ 不确定 | ？ |
| 用户自带 masked assembly，跳过 repeat | ❓ 不确定 | ？ |

### read_qc 和 genome_survey 的特殊性

**观察**：
- read_qc: 只从 **asset registry** 拿 raw reads（没有上游 stage 依赖）
- genome_survey: 只从 **asset registry** 拿 reads（可能优先 clean，fallback raw）
- **它们目前没有"从上游 stage 拿输出"的逻辑**

**为什么？**
- 因为 read_qc 和 genome_survey 是 **数据入口 stage**
- 它们的输入只能来自用户提供（asset registry）
- 不存在"上游 stage 产出 reads"的场景

**所以它们不需要 resolver？**
- 部分对：不需要"从上游 stage fallback"的逻辑
- 但仍有重复：都在重复实现 asset registry 查询

## 建议的行动

### 立即行动（低成本）

创建一个 **asset registry helper**：

```python
# app/stages/base.py

def load_assets_from_registry(
    state: TopLevelState,
    asset_types: list[str],
) -> list[dict[str, Any]]:
    """
    Load assets from registry by type.
    
    Returns list of matching assets, or empty list if not found.
    Handles JSON parsing errors gracefully.
    """
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []
    
    registry_path = Path(str(registry_ref))
    if not registry_path.exists():
        return []
    
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        assets = registry.get("assets", [])
        return [
            asset for asset in assets 
            if asset.get("asset_type") in asset_types
        ]
    except (OSError, json.JSONDecodeError):
        return []
```

**使用**:
```python
# read_qc
def resolve_raw_long_reads(state):
    from app.stages.base import load_assets_from_registry
    assets = load_assets_from_registry(state, ["raw_long_reads"])
    if not assets:
        return _fail_update(state, "raw_long_reads not found")
    # ... rest of logic

# genome_survey
def resolve_survey_inputs(state):
    from app.stages.base import load_assets_from_registry
    clean_short = load_assets_from_registry(state, ["clean_short_dna_reads"])
    raw_short = load_assets_from_registry(state, ["raw_short_dna_reads"])
    # ... rest of logic
```

**收益**：
- ✅ 减少 registry 查询的重复代码
- ✅ 统一错误处理
- ✅ 低成本（1 小时实施）

### 中期规划（如果有需求）

当发现明确的"跳过场景"需求时，实施通用 `resolve_stage_input()`。

**触发条件**：
- 用户要求支持"跳过 read_qc，直接用 clean reads"
- 用户要求支持"跳过 repeat，直接用 masked assembly"
- 出现 3+ 个类似的 fallback 逻辑重复

## 结论

1. ✅ **你是对的**：所有 stage 都是消费者，理论上都需要 resolver
2. ✅ **当前实施合理**：assembly resolver 解决了最紧迫的问题
3. ✅ **下一步建议**：添加 `load_assets_from_registry()` helper
4. ⏸️ **通用 resolver 暂缓**：等待明确的跨 stage fallback 需求

**立即行动项**：
- 创建 `load_assets_from_registry()` helper
- 重构 read_qc 和 genome_survey 使用它
- 观察是否需要更通用的 resolver

要我立即实施 `load_assets_from_registry()` helper 吗？
