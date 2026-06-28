# Phase 8 设计总结

**状态**: ✅ Phase 8 完成，Phase 8.5 完成
**日期**: 2026-06-28

---

## 核心成就

### Phase 8: Assembly Resolver & Stage Contract
1. ✅ 中心化 Assembly Resolver
2. ✅ T2T Manual Review 显式 Gate Node
3. ✅ 统一 Stage Output Contract
4. ✅ 重构 4 个 stage（hic/t2t/repeat/gene）
5. ✅ 修复 3 个 scenario 4 bug
6. ✅ 79/79 测试通过

### Phase 8.5: Universal Resource Resolver（完成）
1. ✅ 通用 Resource Resolver API
2. ✅ Preflight Check API（供 TUI 使用）
3. ✅ Resource Specs Registry
4. ✅ 向后兼容保持
5. ✅ LLM Integration 预留

---

## 关键设计决策

### 1. 三层控制架构

**Layer 1: TUI/Plan 层**
- 通过 `requested_stages` 控制哪些 stage 运行
- 使用 `validate_execution_plan()` 在执行前验证
- 显示友好错误和建议

**Layer 2: RunConfig 层**
- 通过 `local_exec` 控制执行细节
- 各 stage 的 config 提供 override 参数
- 例如：`assembly_fasta_ref` override

**Layer 3: Stage 内部层**
- 使用 `resolve_stage_resource()` 解析输入
- 支持多个 fallback 路径
- 从 stage outputs → asset_registry

### 2. 统一 Stage Output Contract

**设计原则**:
- 每个 stage 输出一个 **unified key**: `final_<resource_type>_for_downstream`
- 保留 **legacy keys** 向后兼容
- Resolver 优先检查 unified key，fallback 到 legacy keys

**实际输出示例**:
```python
# hic_scaffolding
{
    "final_assembly_for_downstream": "path/to/hic.fa",  # Unified (未来实施)
    "final_assembly_for_annotation": "path/to/hic.fa",  # Legacy (当前)
    "chromosome_scaffolded_assembly_candidate_ref": "...",  # Legacy
}

# t2t
{
    "final_assembly_for_downstream": "path/to/t2t.fa",  # Unified (未来实施)
    "final_assembly_for_annotation": "path/to/t2t.fa",  # Legacy (当前)
}
```

**为什么两层 keys？**
- Unified key 支持自动 fallback（resolver 不需要知道具体 stage）
- Legacy keys 保持现有代码兼容（渐进式重构）

### 3. Universal Resource Resolver

**核心 API**:
```python
@dataclass
class ResourceSpec:
    resource_type: str  # "assembly", "reads", "masked_assembly"
    prefer_stages: list[str]  # 优先检查的 stages
    output_keys: dict[str, list[str]]  # 每个 stage 的输出 keys
    asset_types: list[str]  # asset registry fallback
    required: bool  # 是否必需
    allow_multiple: bool  # 是否返回多个路径

def resolve_stage_resource(
    state: TopLevelState,
    *,
    resource_spec: ResourceSpec,
    override: str | None = None,
) -> ResourceResolution:
    """
    解析顺序:
    1. RunConfig override
    2. Preferred stages outputs (unified + legacy keys)
    3. All stages in reverse STAGE_ORDER (auto-fallback)
    4. Asset registry
    """
```

**关键设计点**:
- **声明式**: ResourceSpec 声明需求，resolver 负责查找
- **多层 fallback**: override → prefer_stages → all_stages → assets
- **向后兼容**: `resolve_current_assembly()` 作为 wrapper 保留

### 4. Preflight Check API

**设计目标**:
- **READ-ONLY**: 不修改 state，仅检查可行性
- **TUI 使用**: 在 stage 选择后、执行前验证
- **友好反馈**: 显示缺失资源、提供解决建议

**核心 API**:
```python
@dataclass
class StageCheck:
    stage: str
    can_run: bool
    missing: list[ResourceCheck]  # 缺失的资源
    warnings: list[str]  # 非阻塞警告
    suggestions: list[str]  # 解决建议

@dataclass
class PlanValidation:
    valid: bool
    checks: dict[str, StageCheck]
    first_blocker: str | None  # 第一个阻塞的 stage
    summary: str
    suggestions: list[str]

def validate_execution_plan(
    state: TopLevelState,
    requested_stages: list[str],
) -> PlanValidation:
    """
    按 STAGE_ORDER 检查每个 requested stage
    遇到第一个 blocker 时停止
    """
```

### 5. LLM Integration 预留

**当前实现**: 硬编码 `STAGE_REQUIREMENTS` registry
```python
# app/stages/resource_specs.py
STAGE_REQUIREMENTS = {
    "hic_scaffolding": [{"name": "assembly", "spec": ASSEMBLY_SPEC}],
    "read_qc": [{"name": "raw reads", "spec": RAW_READS_SPEC}],
    ...
}
```

**未来扩展**: LLM + 知识库生成
```python
# app/stages/preflight.py
def _get_stage_requirements(stage: str) -> list[dict]:
    """
    TODO: Future enhancement - generate via LLM + knowledge base.
    
    Query: "What resources does {stage} need to run?"
    Returns: Structured requirements with rationale
    """
    # 当前: 从 STAGE_REQUIREMENTS 读取
    # 未来: query_stage_requirements_via_llm(stage, knowledge_base)
```

---

## 实施历程

### 错误与纠正

**错误 1**: 认为 assembly 不是消费者，不需要 resolver
- **纠正**: 所有 stage 都是消费者，都可能被跳过
- **结果**: 设计通用 resolver 支持所有 stages

**错误 2**: 认为只有下游 stages 需要 resolver
- **纠正**: read_qc、genome_survey 也需要统一 asset registry 查询
- **结果**: 创建 `load_assets_from_registry()` helper

**错误 3**: 过度设计统一输出 key
- **纠正**: 保留 legacy keys，unified key 作为可选优化
- **结果**: 渐进式重构路径

### Bug 修复

**Bug 1**: Scenario 4（用户自带 assembly）失败
- **原因**: resolver 没有 asset_registry fallback
- **修复**: 添加 `_load_assembly_from_registry()`

**Bug 2**: gene_annotation 没有检查 t2t 输出
- **原因**: prefer_stages 缺少 "t2t"
- **修复**: 添加 t2t 到 prefer_stages

**Bug 3**: STAGE_ORDER 缺少 assembly_dedup
- **原因**: 文档 drift
- **修复**: 同步所有文档

---

## 文件结构

### 核心实现
- `app/stages/base.py` (~320 行)
  - `resolve_stage_resource()` - 通用 resolver
  - `load_assets_from_registry()` - Helper
  - `resolve_current_assembly()` - 向后兼容 wrapper

- `app/stages/preflight.py` (~200 行)
  - `check_stage_prerequisites()` - 单 stage 检查
  - `validate_execution_plan()` - 全流程验证
  - `_get_stage_requirements()` - 需求 registry 查询

- `app/stages/resource_specs.py` (~150 行)
  - `ASSEMBLY_SPEC`, `CLEAN_READS_SPEC`, etc. - 预定义 specs
  - `STAGE_REQUIREMENTS` - Stage 需求 registry

### 重构的 Stages
- `app/stages/hic_scaffolding/nodes.py` - 使用 resolver
- `app/stages/t2t/nodes.py` - 使用 resolver
- `app/stages/repeat_annotation/nodes.py` - 使用 resolver
- `app/stages/gene_annotation/nodes.py` - 使用 resolver

### 测试覆盖
- 79/79 测试通过 ✅
- 16 hic_scaffolding tests
- 16 t2t tests
- 18 repeat_annotation tests
- 29 gene_annotation tests

---

## Phase 8.5 完成范围

### 1. TUI Integration
- `app/entrypoints/ask_interactive.py` 已集成 `validate_execution_plan()`
- 验证失败时显示缺失资源和建议
- 支持修改 stage、补充数据后重试、以及 proceed anyway
- `provide_data` revalidation 成功后会继续进入确认流程

### 2. Stage Runtime Alignment
- `repeat_annotation` runtime 改用 `resolve_stage_resource(ASSEMBLY_SPEC)`
- `gene_annotation` softmask preflight 规则与 runtime 对齐
- `function_annotation` 增加 predicted proteins requirement

### 3. 测试和文档
- Preflight API 增加回归测试
- TUI provide-data recovery 增加回归测试
- Phase 8.5 过程文档已合并为设计文档和完成报告

### 后续可选演进
- strict validation 模式
- LLM/知识库生成 stage requirements
- 更完整的 stage IO contract，供 runtime、preflight、docs 共享

---

## 核心价值

### 对开发者
1. **减少重复**: 统一 resolver 替代 60+ 行重复代码
2. **清晰契约**: ResourceSpec 声明式需求
3. **易扩展**: 添加新资源类型只需定义新 spec

### 对用户（TUI）
1. **提前验证**: 执行前知道是否可运行
2. **友好错误**: 显示缺失资源和解决建议
3. **灵活控制**: requested_stages 控制执行流程

### 对 LLM Agent
1. **结构化接口**: Preflight API 提供清晰反馈
2. **知识库集成**: 未来可由 LLM 生成 requirements
3. **自动推理**: 从错误信息推断需要补充的数据

---

## 架构图

```
┌─────────────────────────────────────────────────────────┐
│                    TUI / Plan Layer                      │
│  - requested_stages: ["hic", "repeat", "gene"]          │
│  - validate_execution_plan() → PlanValidation           │
│  - 显示错误和建议                                        │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                   RunConfig Layer                        │
│  - local_exec: {stage: {...config...}}                  │
│  - assembly_fasta_ref: "/path/to/override.fa"           │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                   Stage Internal Layer                   │
│  resolution = resolve_stage_resource(                    │
│      state,                                              │
│      resource_spec=ASSEMBLY_SPEC,                        │
│      override=config.get("assembly_fasta_ref")           │
│  )                                                       │
│                                                          │
│  Resolution order:                                       │
│  1. override (RunConfig)                                 │
│  2. prefer_stages outputs (t2t → hic → assembly_review) │
│  3. all stages (reverse STAGE_ORDER)                     │
│  4. asset_registry (assembly_fasta_registered)           │
└─────────────────────────────────────────────────────────┘
```

---

## 经验教训

### 做得好的
1. ✅ **渐进式重构**: 先 assembly resolver，再泛化
2. ✅ **向后兼容**: 保留 legacy API，新旧并存
3. ✅ **API First**: 先定义接口，再填充实现
4. ✅ **LLM Ready**: 预留扩展点，标记 TODO

### 可以改进的
1. ⚠️ **文档 drift**: 实施时未及时更新文档（Phase 8 后期修复）
2. ⚠️ **早期错误判断**: 认为只有下游需要 resolver（后期纠正）
3. ⚠️ **过度设计倾向**: 最初想完全统一输出 key（简化为 legacy + unified）

### 下次建议
1. 📝 实施时同步更新文档
2. 🔍 先全面调研再下结论（避免"assembly 不是消费者"错误）
3. 🎯 优先最小可行方案（MVP），再逐步扩展

---

## 参考文档

### 实施文档
- [README.md](README.md) - Phase 8 主索引
- [PHASE_8.5_DESIGN.md](PHASE_8.5_DESIGN.md) - Phase 8.5 当前设计
- [PHASE_8.5_COMPLETE.md](PHASE_8.5_COMPLETE.md) - Phase 8.5 完成和修复记录

### 归档讨论
- [archive/ASSEMBLY_RESOLUTION_REFACTOR.md](archive/ASSEMBLY_RESOLUTION_REFACTOR.md) - 重构讨论过程
- [archive/RETHINK_RESOLVER_SCOPE.md](archive/RETHINK_RESOLVER_SCOPE.md) - Resolver 范围重新思考
- [archive/STAGE_OUTPUT_CONTRACT.md](archive/STAGE_OUTPUT_CONTRACT.md) - 输出契约设计
- [archive/REFACTOR_COMPLETE.md](archive/REFACTOR_COMPLETE.md) - Phase 8 完成记录
