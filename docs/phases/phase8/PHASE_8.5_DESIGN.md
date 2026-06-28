# Phase 8.5 设计: Universal Resource Resolver & Preflight API

**日期**: 2026-06-28
**状态**: 完成

---

## 目标

Phase 8.5 将 Phase 8 的 assembly resolver 泛化为可复用的 resource-resolution layer，并将同一套模型暴露给 TUI 做 preflight validation。

设计目标是对齐：

- TUI 应该在执行前提示缺失输入。
- Runtime stage 应该使用同一套规则解析输入。
- Resource requirements 应该集中声明，减少 drift。
- 高级用户仍可以 override 或 proceed anyway。

---

## 架构

```text
Layer 1: TUI / Plan
  validate_execution_plan(state, requested_stages)
  渲染缺失资源和恢复动作

Layer 2: RunConfig
  显式 stage-level overrides，例如 assembly_fasta_ref

Layer 3: Stage Runtime
  resolve_stage_resource(state, resource_spec=..., override=...)
```

三层职责：

- **TUI / Plan** 检查选中的 plan 是否大概率可运行。
- **RunConfig** 将用户显式 override 传给 runtime。
- **Stage Runtime** 解析 stage nodes 实际消费的 file refs。

---

## 核心数据结构

### ResourceSpec

`ResourceSpec` 声明 stage 需要什么资源，不嵌入 stage-specific lookup 逻辑。

```python
@dataclass
class ResourceSpec:
    resource_type: str
    prefer_stages: list[str] = field(default_factory=list)
    output_keys: dict[str, list[str]] = field(default_factory=dict)
    asset_types: list[str] = field(default_factory=list)
    required: bool = True
    allow_multiple: bool = False
```

内置 specs：

- `ASSEMBLY_SPEC`
- `MASKED_ASSEMBLY_SPEC`
- `RAW_READS_SPEC`
- `CLEAN_READS_SPEC`
- `PREDICTED_PROTEINS_SPEC`

### ResourceResolution

`ResourceResolution` 是 `resolve_stage_resource()` 返回的 runtime 解析结果。

```python
@dataclass
class ResourceResolution:
    found: bool
    paths: list[str]
    source: str
    source_type: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> str | None:
        return self.paths[0] if self.paths else None
```

### Preflight Results

Preflight 返回结构化 validation results：

```python
@dataclass
class ResourceCheck:
    resource_name: str
    spec: ResourceSpec
    found: bool
    source: str | None
    message: str

@dataclass
class StageCheck:
    stage: str
    can_run: bool
    missing: list[ResourceCheck]
    warnings: list[str]
    suggestions: list[str]

@dataclass
class PlanValidation:
    valid: bool
    checks: dict[str, StageCheck]
    first_blocker: str | None
    summary: str
    suggestions: list[str]
```

---

## Runtime Resolution

`resolve_stage_resource()` 使用以下优先级：

1. **RunConfig override**
   - 用户显式指定的路径优先于所有自动来源。
   - 例：Hi-C scaffolding 的 `assembly_fasta_ref`。

2. **Preferred stage outputs**
   - 按 `ResourceSpec.prefer_stages` 声明顺序查找。
   - 例：assembly 优先 `t2t`，然后 `hic_scaffolding`，然后 `assembly_review`。

3. **Reverse `STAGE_ORDER` fallback**
   - 如果 preferred stages 没有资源，则优先使用 workflow 后段更精炼的输出。

4. **Asset registry**
   - 用户注册的 assets 是最后 fallback。

这让精炼后的 workflow output 默认优先，同时仍支持显式用户数据。

---

## Preflight Validation

`validate_execution_plan()` 按 canonical stage order 验证 requested plan。

关键行为：

- 只检查 requested stages。
- 模拟已经通过 validation 的上游 stage outputs。
- 允许 downstream stages 依赖同一 plan 中更早产生的资源。
- 要求 planned resources 来自已经处理过的 stages，避免 self-dependency。
- 报告 first blocker、missing resources 和 suggestions。

这避免了两个常见问题：

- false negative：完整 plan 被拒绝，因为上游输出还不存在。
- false positive：downstream stage 依赖运行顺序上不会提前产生的资源。

---

## Stage Contracts

Stage requirements 和 produced outputs 集中在 `app/stages/resource_specs.py`。

`STAGE_REQUIREMENTS` 描述每个 stage 的 preflight requirements。

示例：

```python
STAGE_REQUIREMENTS = {
    "hic_scaffolding": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
    ],
    "gene_annotation": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
        {"name": "masked assembly", "spec": MASKED_ASSEMBLY_SPEC},
    ],
    "function_annotation": [
        {"name": "predicted proteins", "spec": PREDICTED_PROTEINS_SPEC},
    ],
}
```

`STAGE_OUTPUTS` 描述 validation 时可以模拟的 stage outputs。

当前 contract 包括：

- `read_qc` 产生 clean reads。
- `assembly` 产生 draft assembly refs。
- `assembly_dedup` 产生 deduplicated/purged assembly refs。
- `assembly_review`、`hic_scaffolding`、`t2t` 产生 downstream assembly refs。
- `repeat_annotation` 产生 softmasked assembly refs。
- `gene_annotation` 产生 predicted proteins。

---

## Contextual Requirements

有些 requirement 不能只用 optional/required 表达。

### Gene annotation softmask

Runtime 要求：

- 有 softmasked genome，通常来自 repeat annotation；或
- 有显式 reason 允许 unmasked fallback。

Preflight 与 runtime 保持一致：

- 只有 unmasked registered assembly 时，`gene_annotation` 会失败。
- `repeat_annotation -> gene_annotation` 会通过，因为 repeat annotation 计划产生 softmask。
- 显式 skip/fallback reason 允许 unmasked fallback。

### Function annotation proteins

Runtime 要求 predicted proteins 来自 gene annotation。

Preflight 与 runtime 保持一致：

- 单独运行 `function_annotation` 且没有 proteins 时失败。
- `gene_annotation -> function_annotation` 会通过，因为 gene annotation 计划产生 proteins。

---

## TUI Behavior

TUI 在最终 confirmation 前验证 selected stages。

Validation 失败时提供三个动作：

1. **Modify stages**
   - 清空 selected stages，回到 stage selection。

2. **Provide data**
   - 提示用户更新 manifest。
   - 按 Enter 后重新验证。
   - 验证通过则继续 confirmation。
   - 只有验证仍失败才回到 stage selection。

3. **Proceed anyway**
   - 保留当前 plan，继续执行。
   - 主要用于高级用户或 debug workflow。

Preflight exception 会记录 debug traceback，但不会 crash TUI。

---

## Validation Philosophy

Preflight 是 advisory，不是 mandatory。

这是有意设计，因为：

- 高级用户可能通过非标准路径提供资源。
- 调试时可能需要观察 runtime failure path。
- 一些实验场景可能动态准备数据。

TUI 应该清楚提示最可能的问题，提供最安全的恢复路径，同时保留 escape hatch。

未来可以单独增加 strict mode，不改变默认 interactive contract。

---

## Compatibility

兼容性规则：

- 现有 assembly consumers 可以继续使用 `resolve_current_assembly()`。
- 新增或重构的 stages 应优先使用 `resolve_stage_resource()`。
- Runtime 仍在使用的 legacy output keys 继续受支持。
- 影响 stage reachability 的 resource spec 变更必须配套 preflight tests。

---

## Extension Path

新增 resource type 的步骤：

1. 添加 `ResourceSpec`。
2. 在 `STAGE_REQUIREMENTS` 中添加 stage requirements。
3. 如果上游 stage 会产生该资源，在 `STAGE_OUTPUTS` 中添加 produced outputs。
4. Runtime stage 改为调用 `resolve_stage_resource()`。
5. 添加 missing、provided、in-plan-produced resource 的 preflight tests。

长期来看，`STAGE_REQUIREMENTS` 和 `STAGE_OUTPUTS` 可以演进为更完整的 stage IO contract，或由知识库生成。
