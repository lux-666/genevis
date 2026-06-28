# Phase 8 Stage Output Contract 统一

## 问题

当前 Hi-C → T2T 的输出输入 contract 不统一：
- hic_scaffolding 只写 `final_assembly_for_annotation`
- t2t 搜索 `final_assembly_for_t2t_or_annotation`（不存在）+ `final_assembly_for_annotation`（存在）+ fallback keys

这导致：
1. T2T 需要复杂的 fallback 搜索逻辑
2. 文档与实现不一致
3. 未来添加新 stage 时 contract 不清晰

## 统一方案

### 原则
每个 assembly-producing stage 输出统一的 downstream contract key：
```python
"final_assembly_for_downstream": "<path>"
```

同时保留现有 legacy keys 向后兼容。

### 各 Stage 输出

#### assembly_review
```python
{
    "final_assembly_ref": "...",                          # legacy
    "final_assembly_for_scaffolding_or_annotation": "...", # legacy
    "final_assembly_for_downstream": "...",                # ✅ 新增统一 key
}
```

#### hic_scaffolding
```python
{
    "pre_hic_assembly_ref": "...",
    "final_assembly_for_annotation": "...",  # legacy
    "final_assembly_for_downstream": "...",  # ✅ 新增统一 key
}
```

#### t2t
```python
{
    "input_assembly_ref": "...",
    "final_assembly_for_annotation": "...",  # legacy
    "final_assembly_for_downstream": "...",  # ✅ 新增统一 key
}
```

### 各 Stage 输入

#### hic_scaffolding 输入
```python
def _final_assembly_ref(state):
    review = stage_outputs.get("assembly_review")
    # 优先搜索统一 key
    if review.get("final_assembly_for_downstream"):
        return review["final_assembly_for_downstream"]
    # fallback legacy keys
    if review.get("final_assembly_for_scaffolding_or_annotation"):
        return review["final_assembly_for_scaffolding_or_annotation"]
    if review.get("final_assembly_ref"):
        return review["final_assembly_ref"]
    # fallback asset registry
    ...
```

#### t2t 输入
```python
def _assembly_ref(state, override):
    if override:
        return override, "run_config"
    
    hic = stage_outputs.get("hic_scaffolding")
    # 优先搜索统一 key
    if hic.get("final_assembly_for_downstream"):
        return hic["final_assembly_for_downstream"], "hic_scaffolding"
    # fallback legacy keys
    if hic.get("final_assembly_for_annotation"):
        return hic["final_assembly_for_annotation"], "hic_scaffolding"
    
    review = stage_outputs.get("assembly_review")
    # 优先搜索统一 key
    if review.get("final_assembly_for_downstream"):
        return review["final_assembly_for_downstream"], "assembly_review"
    # fallback legacy keys
    ...
```

#### repeat_annotation 输入
```python
def _assembly_ref(state):
    t2t = stage_outputs.get("t2t")
    # 优先搜索统一 key
    if t2t.get("final_assembly_for_downstream"):
        return t2t["final_assembly_for_downstream"]
    # fallback legacy keys
    if t2t.get("final_assembly_for_annotation"):
        return t2t["final_assembly_for_annotation"]
    
    hic = stage_outputs.get("hic_scaffolding")
    if hic.get("final_assembly_for_downstream"):
        return hic["final_assembly_for_downstream"]
    ...
```

## 实施计划

### Phase 1: 添加统一 key（向后兼容）
1. ✅ 每个 stage commit 时同时写 `final_assembly_for_downstream` 和 legacy keys
2. ✅ 每个 stage 输入时优先搜索 `final_assembly_for_downstream`，fallback legacy keys
3. ✅ 测试验证：所有现有测试应通过

### Phase 2: 文档更新
1. 更新 04_hic_scaffolding_stage.md
2. 更新 05_t2t_stage.md
3. 更新 stage_contracts.md（如果存在）

### Phase 3: 逐步迁移（可选）
1. 新测试只验证 `final_assembly_for_downstream`
2. 文档只推荐使用 `final_assembly_for_downstream`
3. 保留 legacy keys 至少 2-3 个 minor version

## 好处

1. **清晰的 contract**：每个 stage 都知道下游期望什么
2. **简化输入逻辑**：优先搜索一个统一 key，减少 fallback 复杂度
3. **向后兼容**：不破坏现有代码和测试
4. **未来扩展**：新增 stage 有明确的 contract 可遵循

## 拒绝的备选方案

### ❌ 方案 A：只用 `final_assembly_for_annotation`
- 问题：名字暗示只给 annotation 用，但 t2t 也消费
- 问题：hic_scaffolding 和 t2t 都写同名 key，语义混乱

### ❌ 方案 B：每个 stage 用 stage-specific key
- 如 `hic_final_assembly`, `t2t_final_assembly`
- 问题：下游需要知道是从哪个 stage 来的（破坏解耦）
- 问题：跳过 stage 时需要复杂的 fallback 链

### ✅ 方案 C：统一 `final_assembly_for_downstream` + legacy fallback
- 清晰、可扩展、向后兼容
