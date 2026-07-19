# Stage Input Resolution 重构评估

## 当前问题

### 场景 1: 完整流程（assembly → hic → t2t）
```
assembly → assembly_review → hic_scaffolding → t2t → repeat_annotation
```
- hic 从 assembly_review 拿 assembly
- t2t 从 hic_scaffolding 拿 assembly

### 场景 2: 跳过 assembly（用户自带 assembly）
```
inventory (existing assembly) → hic_scaffolding → t2t → repeat_annotation
```
- hic 应该从 **asset registry** 拿 assembly（`assembly_fasta_registered`）
- t2t 从 hic_scaffolding 拿 assembly

### 场景 3: 跳过 hic
```
assembly → assembly_review → t2t → repeat_annotation
```
- t2t 从 assembly_review 拿 assembly

### 场景 4: 跳过 assembly 和 hic
```
inventory (existing assembly) → t2t → repeat_annotation
```
- t2t 应该从 **asset registry** 拿 assembly

## 当前实现的问题

### hic_scaffolding 输入逻辑
```python
def _final_assembly_ref(state):
    # 1. 先找 assembly_review 输出
    review_out = stage_outputs.get("assembly_review")
    if review_out.get("final_assembly_for_scaffolding_or_annotation"):
        return ...
    
    # 2. fallback 到 asset registry（支持场景 2）
    for asset in registry.get("assets"):
        if asset.get("asset_type") == "assembly_fasta_registered":
            return asset["path"]
    
    return None
```
✅ **逻辑合理**：先找上游 stage，找不到就从 registry 拿用户自带的 assembly。

### t2t 输入逻辑（当前实现）
```python
def _assembly_ref(state, override):
    # 1. 先找 hic_scaffolding 输出
    hic = stage_outputs.get("hic_scaffolding")
    if hic.get("final_assembly_for_downstream"):
        return hic["final_assembly_for_downstream"], "hic_scaffolding"
    if hic.get("final_assembly_for_annotation"):
        return hic["final_assembly_for_annotation"], "hic_scaffolding"
    
    # 2. 再找 assembly_review 输出
    review = stage_outputs.get("assembly_review")
    if review.get("final_assembly_for_downstream"):
        return review["final_assembly_for_downstream"], "assembly_review"
    if review.get("final_assembly_for_scaffolding_or_annotation"):
        return ...
    
    # ❌ 问题：没有 fallback 到 asset registry
    return None, "missing"
```

**问题**: 场景 4（跳过 assembly 和 hic）时，t2t 拿不到 assembly！

---

## 根本问题分析

### 问题 1: 混淆了两种职责

**职责 A**: 获取"上游 stage 处理后的 assembly"
- hic 需要 assembly_review 的输出
- t2t 需要 hic_scaffolding 或 assembly_review 的输出

**职责 B**: 获取"当前可用的最佳 assembly"（不管来源）
- 可能来自上游 stage
- 也可能来自 asset registry（用户自带）
- 还可能来自 run_config override

### 问题 2: 疯狂 fallback 的根源

因为混淆了职责 A 和 B，导致：
1. 每个 stage 都要自己实现复杂的 fallback 逻辑
2. 不同 stage 的 fallback 逻辑不一致
3. 新增 stage 时不知道该搜索哪些 key

### 问题 3: `final_assembly_for_downstream` 治标不治本

- 统一了 stage 间的 contract（好）
- 但没有解决"如何处理跳过 stage"的问题（坏）
- 仍然需要每个 stage 自己 fallback 到 registry（坏）

---

## 重构方案

### 方案 A: 中心化 Assembly Resolver ⭐⭐⭐

创建一个统一的 assembly resolution 函数，供所有下游 stage 使用。

```python
# app/stages/base.py or app/assembly/resolver.py

def resolve_current_assembly(
    state: TopLevelState,
    *,
    override: str | None = None,
    prefer_stages: list[str] | None = None,
) -> tuple[str | None, str]:
    """
    Resolve the current best assembly from state.
    
    Resolution order:
    1. RunConfig override (if provided)
    2. Preferred stages outputs (if specified, in order)
    3. All stage outputs in reverse STAGE_ORDER
    4. Asset registry (assembly_fasta_registered)
    
    Returns:
        (assembly_path, source)
        source is one of: "run_config" | "<stage_name>" | "asset_registry" | "missing"
    """
    if override:
        return str(override), "run_config"
    
    outputs = state.get("stage_outputs") or {}
    
    # Check preferred stages first
    if prefer_stages:
        for stage in prefer_stages:
            stage_out = outputs.get(stage) or {}
            if stage_out.get("final_assembly_for_downstream"):
                return str(stage_out["final_assembly_for_downstream"]), stage
    
    # Check all stages in reverse STAGE_ORDER
    from app.workflow import STAGE_ORDER
    for stage in reversed(STAGE_ORDER):
        stage_out = outputs.get(stage) or {}
        if stage_out.get("final_assembly_for_downstream"):
            return str(stage_out["final_assembly_for_downstream"]), stage
    
    # Fallback to asset registry
    registry_ref = state.get("asset_registry_ref")
    if registry_ref and Path(registry_ref).exists():
        try:
            registry = json.loads(Path(registry_ref).read_text(encoding="utf-8"))
            for asset in registry.get("assets", []):
                if asset.get("asset_type") == "assembly_fasta_registered" and asset.get("path"):
                    return str(asset["path"]), "asset_registry"
        except (OSError, json.JSONDecodeError):
            pass
    
    return None, "missing"
```

**使用示例**:

```python
# hic_scaffolding/nodes.py
def resolve_hic_inputs(state):
    # 优先从 assembly_review 拿，fallback 到 registry
    assembly_ref, source = resolve_current_assembly(
        state,
        prefer_stages=["assembly_review"]
    )
    if not assembly_ref:
        return _fail(state, "missing assembly for hic_scaffolding")
    ...

# t2t/nodes.py
def resolve_t2t_inputs(state):
    config = _config(_output(state))
    # 优先从 hic_scaffolding 拿，fallback 到 assembly_review，最后 registry
    assembly_ref, source = resolve_current_assembly(
        state,
        override=config.get("assembly_fasta_ref"),
        prefer_stages=["hic_scaffolding", "assembly_review"]
    )
    if not assembly_ref:
        return _fail(state, "missing assembly for t2t")
    ...

# repeat_annotation/nodes.py
def resolve_repeat_inputs(state):
    # 优先从 t2t 拿，fallback 到 hic，assembly_review，registry
    assembly_ref, source = resolve_current_assembly(
        state,
        prefer_stages=["t2t", "hic_scaffolding", "assembly_review"]
    )
    ...
```

**优点**:
- ✅ 统一逻辑，所有 stage 使用同一个函数
- ✅ 自动处理所有跳过场景（包括场景 4）
- ✅ 清晰的优先级机制
- ✅ 新增 stage 只需调用函数，指定 prefer_stages
- ✅ 保留 `final_assembly_for_downstream` 统一 contract
- ✅ 减少重复代码

**缺点**:
- ⚠️ 需要重构现有 3 个 stage 的输入逻辑
- ⚠️ 需要测试所有跳过场景组合

**工作量**: 中（3-4 小时，但收益持久）

---

### 方案 B: Inventory 输出 Best Assembly Reference

让 inventory 负责判断并输出"当前最佳 assembly"。

```python
# inventory/nodes.py
def register_assets(state):
    ...
    # 如果用户提供了 existing assembly，记录到 inventory output
    outputs["inventory"] = {
        "status": "completed",
        "entry_mode": "existing_assembly" if has_existing_assembly else "raw_reads",
        "assembly_ref": existing_assembly_path if has_existing_assembly else None,
        ...
    }

# hic_scaffolding/nodes.py
def resolve_hic_inputs(state):
    inventory = stage_outputs.get("inventory") or {}
    review = stage_outputs.get("assembly_review") or {}
    
    # 优先 assembly_review，fallback inventory
    assembly_ref = (
        review.get("final_assembly_for_downstream") or
        inventory.get("assembly_ref")
    )
```

**优点**:
- ✅ Inventory 是唯一知道"用户自带 assembly"的地方
- ✅ 减少重复的 asset registry 查询

**缺点**:
- ❌ Inventory 不应该负责"选择最佳 assembly"（职责过重）
- ❌ 如果 assembly_review 后又有新 assembly（如 hic patched），inventory 的 ref 就过期了
- ❌ 仍然需要每个 stage 自己实现 fallback 逻辑

**结论**: ❌ 不推荐，职责不清晰

---

### 方案 C: 保持现状 + 修复 T2T Bug

只修复 t2t 没有 fallback 到 asset registry 的 bug，不做大重构。

```python
# t2t/nodes.py
def _assembly_ref(state, override):
    if override:
        return str(override), "run_config"
    
    outputs = state.get("stage_outputs") or {}
    hic = outputs.get("hic_scaffolding") or {}
    if hic.get("final_assembly_for_downstream"):
        return str(hic["final_assembly_for_downstream"]), "hic_scaffolding"
    
    review = outputs.get("assembly_review") or {}
    if review.get("final_assembly_for_downstream"):
        return str(review["final_assembly_for_downstream"]), "assembly_review"
    
    # ✅ 新增：fallback 到 asset registry
    registry_ref = state.get("asset_registry_ref")
    if registry_ref and Path(registry_ref).exists():
        try:
            registry = json.loads(Path(registry_ref).read_text())
            for asset in registry.get("assets", []):
                if asset.get("asset_type") == "assembly_fasta_registered":
                    return str(asset["path"]), "asset_registry"
        except (OSError, json.JSONDecodeError):
            pass
    
    return None, "missing"
```

**优点**:
- ✅ 最小改动
- ✅ 修复场景 4 的 bug
- ✅ 低风险

**缺点**:
- ❌ 仍然需要每个 stage 重复 registry fallback 代码
- ❌ 不解决根本架构问题

**结论**: ⚠️ 作为临时修复可以，但不是长期方案

---

## 推荐方案

### 优先级 1: 方案 A（中心化 Resolver）⭐⭐⭐

**理由**:
1. 解决根本问题（统一 assembly resolution 逻辑）
2. 支持所有跳过场景
3. 未来添加新 stage 容易
4. 减少重复代码

**实施步骤**:
1. 创建 `resolve_current_assembly()` 函数
2. 重构 hic_scaffolding 使用新函数
3. 重构 t2t 使用新函数
4. 重构 repeat_annotation 使用新函数
5. 补充跳过场景测试

**风险**:
- 需要仔细测试所有 stage 组合
- 需要确保向后兼容

### 优先级 2: 方案 C（临时修复）

如果时间紧急，先用方案 C 修复 t2t 的 bug，再规划方案 A。

---

## 关于 `final_assembly_for_downstream`

### 保留还是回滚？

**建议**: ✅ **保留**

**理由**:
1. 它解决了"stage 间 contract 统一"的问题（这是对的）
2. 即使实施方案 A，仍然需要统一的 output key
3. 方案 A 的 `resolve_current_assembly()` 就是依赖这个统一 key 工作的
4. 已实施完成且测试通过，回滚是浪费

**调整**:
- 保持 `final_assembly_for_downstream` 作为 stage 输出 contract
- 删除各 stage 内部的复杂 fallback 逻辑
- 统一使用 `resolve_current_assembly()` 获取输入

---

## 总结

| 方案 | 收益 | 风险 | 工作量 | 推荐 |
|------|------|------|--------|------|
| A. 中心化 Resolver | 高 | 中 | 中 (3-4h) | ⭐⭐⭐ |
| B. Inventory 输出 | 低 | 高 | 中 | ❌ |
| C. 临时修复 | 低 | 低 | 小 (30min) | ⚠️ 短期可行 |

**建议行动**:
1. 短期：实施方案 C，修复 t2t 的 asset registry fallback bug
2. 中期：实施方案 A，创建中心化 resolver，彻底解决架构问题
3. `final_assembly_for_downstream` 保留，它是正确的设计
