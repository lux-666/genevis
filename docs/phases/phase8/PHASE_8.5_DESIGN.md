# Phase 8.5: Universal Resource Resolver & Preflight API

**状态**: 🚧 核心架构完成，TUI Integration 待实施  
**日期**: 2026-06-28  
**预计工作量**: 剩余 7-8 小时

---

## 目标

将 Phase 8 的 Assembly Resolver 泛化为通用 Resource Resolver，并为 TUI 提供执行前验证 API。

### 核心价值主张

1. **对用户（TUI）**: 执行前知道是否可运行，友好错误提示
2. **对开发者**: 减少重复代码，统一资源解析逻辑
3. **对 LLM Agent**: 结构化接口，可由知识库 + LLM 生成 requirements

---

## 架构设计

### 1. 三层资源解析架构

```
┌─────────────────────────────────────────────────────────┐
│              Layer 1: TUI/Plan 层                        │
│  职责: 用户交互、plan 验证、错误显示                     │
│                                                          │
│  API:                                                    │
│    validation = validate_execution_plan(                 │
│        state,                                            │
│        requested_stages=["hic", "repeat", "gene"]        │
│    )                                                     │
│                                                          │
│  输出:                                                   │
│    PlanValidation {                                      │
│        valid: bool                                       │
│        checks: {stage → StageCheck}                      │
│        first_blocker: str | None                         │
│        summary: "✗ Cannot run 'hic': missing assembly"   │
│        suggestions: ["Provide assembly_fasta_registered"]│
│    }                                                     │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│              Layer 2: RunConfig 层                       │
│  职责: 执行参数配置、资源 override                       │
│                                                          │
│  配置:                                                   │
│    local_exec = {                                        │
│        "hic_scaffolding": {                              │
│            "assembly_fasta_ref": "/path/to/custom.fa"    │
│        }                                                 │
│    }                                                     │
│                                                          │
│  传递: config → stage 执行时作为 override                │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│              Layer 3: Stage 内部层                       │
│  职责: 资源解析、执行逻辑                                │
│                                                          │
│  代码:                                                   │
│    resolution = resolve_stage_resource(                  │
│        state,                                            │
│        resource_spec=ASSEMBLY_SPEC,                      │
│        override=config.get("assembly_fasta_ref")         │
│    )                                                     │
│                                                          │
│  解析顺序:                                               │
│    1. override (来自 RunConfig)                          │
│    2. prefer_stages outputs (t2t → hic → review)         │
│    3. all stages (reverse STAGE_ORDER, auto-fallback)    │
│    4. asset_registry (assembly_fasta_registered)         │
└─────────────────────────────────────────────────────────┘
```

### 2. 核心数据结构

#### ResourceSpec - 资源需求声明

```python
@dataclass
class ResourceSpec:
    """声明式资源需求定义"""
    
    resource_type: str
    """资源类型标识: 'assembly', 'reads', 'clean_reads', 'masked_assembly'"""
    
    prefer_stages: list[str] = field(default_factory=list)
    """优先检查的 stages，按优先级排序"""
    
    output_keys: dict[str, list[str]] = field(default_factory=dict)
    """每个 stage 的输出 keys: {stage_name: [key1, key2]}"""
    
    asset_types: list[str] = field(default_factory=list)
    """asset registry 中的 asset_type，作为最后 fallback"""
    
    required: bool = True
    """是否必需（True=阻塞，False=警告）"""
    
    allow_multiple: bool = False
    """是否返回所有匹配（True=list，False=first match）"""
```

**设计要点**:
- 声明式：描述"需要什么"，而非"如何查找"
- 可扩展：添加新资源类型只需定义新 spec
- 灵活：支持 prefer_stages、output_keys、asset_types 三层 fallback

#### ResourceResolution - 解析结果

```python
@dataclass
class ResourceResolution:
    """资源解析结果"""
    
    found: bool
    """是否找到资源"""
    
    paths: list[str]
    """解析到的路径（allow_multiple=True 时可能多个）"""
    
    source: str
    """来源: 'run_config', '<stage_name>', 'asset_registry', 'missing'"""
    
    source_type: str
    """来源类型: 'override', 'stage_output', 'asset', 'missing'"""
    
    metadata: dict[str, Any] = field(default_factory=dict)
    """额外元数据（platform, format 等）"""
    
    @property
    def path(self) -> str | None:
        """单路径访问便捷属性"""
        return self.paths[0] if self.paths else None
```

#### StageCheck - Stage 可运行性检查

```python
@dataclass
class ResourceCheck:
    """单个资源检查结果"""
    resource_name: str      # "assembly", "reads"
    spec: ResourceSpec      # 检查的 spec
    found: bool             # 是否找到
    source: str | None      # 来源（stage 或 asset_registry）
    message: str            # 人类可读消息

@dataclass
class StageCheck:
    """单个 stage 前置条件检查"""
    stage: str                       # Stage 名称
    can_run: bool                    # 是否可运行
    missing: list[ResourceCheck]     # 缺失的资源
    warnings: list[str]              # 非阻塞警告
    suggestions: list[str]           # 解决建议
```

#### PlanValidation - 执行计划验证

```python
@dataclass
class PlanValidation:
    """整体执行计划验证结果"""
    valid: bool                      # 是否所有 stages 都可运行
    checks: dict[str, StageCheck]    # {stage_name: StageCheck}
    first_blocker: str | None        # 第一个阻塞的 stage
    summary: str                     # 人类可读总结
    suggestions: list[str]           # 整体建议
```

---

## 实施细节

### Part 1: 通用 Resource Resolver（✅ 已完成）

#### 文件: `app/stages/base.py`

**核心函数**:

```python
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
    
    Returns:
        ResourceResolution 包含 paths, source, metadata
    """
```

**实现要点**:
1. **Override 优先**: 如果 RunConfig 提供 override，直接返回
2. **Prefer stages**: 按 `prefer_stages` 顺序检查
3. **Auto-fallback**: 按 STAGE_ORDER 倒序检查所有 stages
4. **Asset fallback**: 最后查 asset_registry

**Helper 函数**:

```python
def load_assets_from_registry(
    state: TopLevelState,
    asset_types: list[str],
) -> list[dict[str, Any]]:
    """
    从 asset registry 加载指定类型的 assets
    
    统一 JSON 解析和错误处理
    减少重复代码
    """
```

**向后兼容**:

```python
def resolve_current_assembly(
    state: TopLevelState,
    *,
    override: str | None = None,
    prefer_stages: list[str] | None = None,
) -> tuple[str | None, str]:
    """
    Assembly 解析（向后兼容 wrapper）
    
    内部调用 resolve_stage_resource()
    保持现有代码不变
    """
```

### Part 2: Resource Specs Registry（✅ 已完成）

#### 文件: `app/stages/resource_specs.py`

**预定义 Specs**:

```python
# Assembly resources
ASSEMBLY_SPEC = ResourceSpec(
    resource_type="assembly",
    prefer_stages=["t2t", "hic_scaffolding", "assembly_review"],
    output_keys={
        "t2t": ["final_assembly_for_annotation"],
        "hic_scaffolding": ["final_assembly_for_annotation", 
                            "chromosome_scaffolded_assembly_candidate_ref"],
        "assembly_review": ["final_assembly_for_scaffolding_or_annotation", 
                            "final_assembly_ref"],
    },
    asset_types=["assembly_fasta_registered"],
    required=True,
)

MASKED_ASSEMBLY_SPEC = ResourceSpec(
    resource_type="masked_assembly",
    prefer_stages=["repeat_annotation"],
    output_keys={"repeat_annotation": ["softmasked_genome_ref"]},
    asset_types=[],  # Should not come from asset registry
    required=False,
)

# Reads resources
RAW_READS_SPEC = ResourceSpec(
    resource_type="raw_reads",
    prefer_stages=[],  # No stage produces raw reads
    output_keys={},
    asset_types=["raw_long_reads", "long_reads"],
    required=True,
)

CLEAN_READS_SPEC = ResourceSpec(
    resource_type="clean_reads",
    prefer_stages=["read_qc"],
    output_keys={"read_qc": ["filtered_long_reads_ref", "assembly_input_ref"]},
    asset_types=["clean_long_reads"],
    required=False,  # Can fallback to raw
)
```

**Stage Requirements Registry**:

```python
STAGE_REQUIREMENTS = {
    "read_qc": [
        {"name": "raw reads", "spec": RAW_READS_SPEC},
    ],
    "genome_survey": [
        {"name": "reads", "spec": ResourceSpec(...)},
    ],
    "assembly": [
        {"name": "reads", "spec": ResourceSpec(...)},
    ],
    "assembly_dedup": [
        {"name": "draft assembly", "spec": ResourceSpec(...)},
    ],
    "assembly_review": [
        {"name": "assembly candidates", "spec": ResourceSpec(...)},
    ],
    "hic_scaffolding": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
    ],
    "t2t": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
    ],
    "repeat_annotation": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
    ],
    "gene_annotation": [
        {"name": "assembly", "spec": ASSEMBLY_SPEC},
        {"name": "masked assembly", "spec": MASKED_ASSEMBLY_SPEC},
    ],
}
```

**LLM Integration 预留**:
```python
# TODO: 将来由三段论知识库 + LLM 生成
# Query: "What resources does {stage} need to run?"
# Returns: Structured requirements with rationale
```

### Part 3: Preflight Check API（✅ 已完成）

#### 文件: `app/stages/preflight.py`

**核心函数**:

```python
def check_stage_prerequisites(
    state: TopLevelState,
    stage: str,
) -> StageCheck:
    """
    检查单个 stage 是否可运行
    
    READ-ONLY - 不修改 state
    供 TUI planning 使用
    
    流程:
    1. 获取 stage requirements
    2. 对每个 requirement 调用 resolve_stage_resource()
    3. 判断是否 required
    4. 生成 suggestions
    
    Returns:
        StageCheck with can_run, missing resources, suggestions
    """
```

```python
def validate_execution_plan(
    state: TopLevelState,
    requested_stages: list[str],
) -> PlanValidation:
    """
    验证执行计划是否可行
    
    按 STAGE_ORDER 顺序检查 requested_stages
    遇到第一个 blocker 停止（后续 stages 不检查）
    
    Returns:
        PlanValidation with overall status, per-stage checks, suggestions
    """
```

**内部函数**:

```python
def _get_stage_requirements(stage: str) -> list[dict[str, Any]]:
    """
    获取 stage 的资源需求
    
    当前: 从 STAGE_REQUIREMENTS 读取
    未来: Query via LLM + knowledge base
    """
    from app.stages.resource_specs import STAGE_REQUIREMENTS
    return STAGE_REQUIREMENTS.get(stage, [])

def _generate_suggestions(stage: str, missing: list[ResourceCheck]) -> list[str]:
    """根据缺失资源生成解决建议"""
    suggestions = []
    for res in missing:
        if res.resource_name == "assembly":
            suggestions.append(
                "Provide 'assembly_fasta_registered' asset in manifest, "
                "or add upstream stages: assembly → assembly_review"
            )
        elif res.resource_name == "reads" or res.resource_name == "raw reads":
            suggestions.append("Provide 'raw_long_reads' asset in manifest")
    return suggestions
```

### Part 4: TUI Integration（⏸️ 待实施）

#### 文件: `app/entrypoints/ask_interactive.py`

**修改点 1: 添加 validation 调用**

```python
def _interactive_loop_inner(plan: ExecutionPlan, args) -> int:
    # ... existing stage selection logic ...
    
    # ✅ 新增：Preflight validation
    if plan.selected_stages:
        from app.stages.preflight import validate_execution_plan
        
        validation = validate_execution_plan(
            state=plan.state,
            requested_stages=plan.selected_stages
        )
        
        if not validation.valid:
            # 显示友好错误
            _render_validation_errors(validation)
            
            # 询问用户如何处理
            choice = _prompt_validation_action()
            
            if choice == "modify_stages":
                # 重新选择 stages
                plan.selected_stages = None
                continue
            elif choice == "provide_data":
                # 提示用户补充数据
                print("\n请在 manifest 中添加缺失的 assets，然后重新运行。")
                return 1
            elif choice == "proceed_anyway":
                # 强制继续（高级用户）
                print("\n⚠️  警告：忽略验证错误，继续执行...")
    
    # ... rest of execution flow ...
```

**修改点 2: 错误显示函数**

```python
def _render_validation_errors(validation: PlanValidation) -> None:
    """显示友好的 validation 错误"""
    print(f"\n❌ {validation.summary}\n")
    
    # 显示第一个 blocker 的详情
    if validation.first_blocker:
        blocker = validation.checks[validation.first_blocker]
        print(f"Stage: {blocker.stage}")
        print(f"Status: {'✓ can run' if blocker.can_run else '✗ cannot run'}\n")
        
        if blocker.missing:
            print("Missing resources:")
            for res in blocker.missing:
                print(f"  • {res.resource_name}: {res.message}")
        
        if blocker.suggestions:
            print("\n💡 Suggestions:")
            for suggestion in blocker.suggestions:
                print(f"  → {suggestion}")
    
    # 显示整体建议
    if validation.suggestions:
        print("\n📋 Next steps:")
        for suggestion in validation.suggestions:
            print(f"  • {suggestion}")
```

**修改点 3: 用户选择函数**

```python
def _prompt_validation_action() -> str:
    """询问用户如何处理 validation 错误"""
    print("\nWhat would you like to do?")
    print("  1. Modify stage selection (remove blocked stages)")
    print("  2. Provide missing data (add assets to manifest)")
    print("  3. Proceed anyway (advanced)")
    
    while True:
        choice = input("\nYour choice [1/2/3]: ").strip()
        if choice == "1":
            return "modify_stages"
        elif choice == "2":
            return "provide_data"
        elif choice == "3":
            return "proceed_anyway"
        else:
            print("Invalid choice. Please enter 1, 2, or 3.")
```

**集成时机**:
```
用户交互流程:
1. 询问 goal
2. 生成初始 plan
3. 澄清 stage 选择
4. ✅ **[NEW] Preflight validation**
5. 确认执行
6. 运行 stages
```

### Part 5: Stage 重构（⏸️ 部分完成）

#### 已完成（Phase 8）
- ✅ `app/stages/hic_scaffolding/nodes.py` - 使用 `resolve_current_assembly()`
- ✅ `app/stages/t2t/nodes.py` - 使用 `resolve_current_assembly()`
- ✅ `app/stages/repeat_annotation/nodes.py` - 使用 `resolve_current_assembly()`
- ✅ `app/stages/gene_annotation/nodes.py` - 使用 `resolve_current_assembly()`

#### 待实施

**read_qc 重构**:
```python
# Before (22 行重复代码)
def resolve_raw_long_reads(state):
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return _fail_update(state, "missing asset_registry_ref")
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return _fail_update(state, f"registry not found: {registry_ref}")
    try:
        registry = json.loads(registry_path.read_text())
    except json.JSONDecodeError:
        return _fail_update(state, "invalid registry JSON")
    assets = [a for a in registry["assets"] if a["asset_type"] == "raw_long_reads"]
    if not assets:
        return _fail_update(state, "raw_long_reads not found")
    # ... more logic ...

# After (5 行)
def resolve_raw_long_reads(state):
    from app.stages.base import load_assets_from_registry
    assets = load_assets_from_registry(state, ["raw_long_reads"])
    if not assets:
        return _fail_update(state, "raw_long_reads not found")
    # ... rest of logic ...
```

**genome_survey 重构**:
```python
# Before (30+ 行重复代码)
def resolve_survey_inputs(state):
    registry_ref = state.get("asset_registry_ref")
    # ... registry loading boilerplate ...
    clean_short = _assets_of_type(assets, "clean_short_dna_reads")
    raw_short = _assets_of_type(assets, "raw_short_dna_reads")
    # ... more logic ...

# After (15 行)
def resolve_survey_inputs(state):
    from app.stages.base import load_assets_from_registry
    clean_short = load_assets_from_registry(state, ["clean_short_dna_reads"])
    raw_short = load_assets_from_registry(state, ["raw_short_dna_reads"])
    # ... rest of logic ...
```

**assembly 重构（复杂，暂缓）**:
- assembly stage 有复杂的输入解析逻辑
- 涉及 read_qc 输出、asset registry、候选选择等
- 需要单独规划，不在 Phase 8.5 范围

---

## 测试策略

### Unit Tests

**test_preflight.py**:
```python
def test_check_stage_prerequisites_missing_assembly():
    """测试缺少 assembly 时的检查"""
    state = TopLevelState(
        data_dir="data",
        asset_registry_ref="data/assets.json",
        stage_outputs={},  # No upstream outputs
    )
    # assets.json 中没有 assembly_fasta_registered
    
    check = check_stage_prerequisites(state, "hic_scaffolding")
    
    assert not check.can_run
    assert len(check.missing) == 1
    assert check.missing[0].resource_name == "assembly"
    assert "assembly_fasta_registered" in check.suggestions[0]

def test_validate_execution_plan_valid():
    """测试有效的执行计划"""
    state = TopLevelState(...)
    # 提供所有必需资源
    
    validation = validate_execution_plan(
        state, 
        requested_stages=["hic_scaffolding", "repeat_annotation"]
    )
    
    assert validation.valid
    assert validation.first_blocker is None
    assert len(validation.checks) == 2

def test_validate_execution_plan_blocker():
    """测试有阻塞的执行计划"""
    state = TopLevelState(...)
    # 缺少 assembly
    
    validation = validate_execution_plan(
        state,
        requested_stages=["hic_scaffolding", "repeat_annotation"]
    )
    
    assert not validation.valid
    assert validation.first_blocker == "hic_scaffolding"
    assert "assembly" in validation.summary
```

### Integration Tests

**test_tui_validation.py**:
```python
def test_tui_validation_missing_data(tmp_path):
    """测试 TUI validation 在缺少数据时的行为"""
    # Setup: manifest 没有 assembly
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "assets": []  # Empty
    }))
    
    # Run TUI with hic_scaffolding requested
    # Mock user input: select hic_scaffolding
    # Expect: validation error displayed
    # Expect: prompt for action
    
def test_tui_validation_with_data(tmp_path):
    """测试 TUI validation 在有数据时的行为"""
    # Setup: manifest 包含 assembly
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "assets": [
            {"asset_type": "assembly_fasta_registered", "path": "assembly.fa"}
        ]
    }))
    
    # Run TUI with hic_scaffolding requested
    # Expect: validation passes
    # Expect: proceeds to execution
```

---

## 实施计划

### ✅ Phase 1: 核心架构（已完成，2h）
- [x] 扩展 `base.py` - `resolve_stage_resource()`, `load_assets_from_registry()`
- [x] 创建 `preflight.py` - API 定义和实现
- [x] 创建 `resource_specs.py` - Specs 和 requirements registry
- [x] 向后兼容验证 - 所有现有测试通过（79/79）

**成果**:
- `app/stages/base.py` - 138 → 320 行
- `app/stages/preflight.py` - 200 行（新）
- `app/stages/resource_specs.py` - 150 行（新）

### ⏸️ Phase 2: TUI Integration（待实施，2h）
- [ ] 修改 `ask_interactive.py` - 添加 validation 调用
- [ ] 实现 `_render_validation_errors()` - 友好错误显示
- [ ] 实现 `_prompt_validation_action()` - 用户选择处理
- [ ] 手动测试交互流程

**改动量**: ~30 行新代码

### ⏸️ Phase 3: Stage 重构（待实施，2h）
- [ ] read_qc - 使用 `load_assets_from_registry()`
- [ ] genome_survey - 使用 `load_assets_from_registry()`
- [ ] 运行测试验证零回归

**改动量**: -42 行重复代码

### ⏸️ Phase 4: 测试和文档（待实施，2h）
- [ ] `test_preflight.py` - Unit tests
- [ ] `test_tui_validation.py` - Integration tests
- [ ] 更新用户文档
- [ ] 更新 CLAUDE.md 项目指南

**总剩余工作量**: 7-8 小时

---

## 验收标准

### 功能标准
- [ ] `validate_execution_plan()` 正确检测缺失资源
- [ ] TUI 在 stage 选择后显示 validation 错误
- [ ] 用户可选择修改 plan 或补充数据
- [ ] `load_assets_from_registry()` 减少重复代码
- [ ] read_qc 和 genome_survey 代码简化

### 质量标准
- [ ] 所有现有测试通过（79/79，零回归）
- [ ] 新增 preflight unit tests 覆盖核心逻辑
- [ ] TUI integration tests 覆盖主要场景
- [ ] 代码 review 通过

### 文档标准
- [ ] Preflight API 文档完整
- [ ] TUI validation 用户文档
- [ ] LLM integration 接口说明
- [ ] Phase 8.5 设计文档落盘

---

## 风险与缓解

### 风险 1: TUI 集成破坏现有流程
**缓解**: 
- 保持向后兼容，validation 作为可选步骤
- 允许用户"Proceed anyway"绕过 validation
- 充分测试原有交互路径

### 风险 2: Stage 重构引入 bug
**缓解**:
- 只重构 read_qc 和 genome_survey（简单 stages）
- assembly 暂缓（复杂度高）
- 每个重构后立即运行测试

### 风险 3: LLM integration 接口设计不当
**缓解**:
- 当前 hardcoded registry 足够使用
- 接口预留但不强制使用
- 将来根据实际需求调整

---

## 成功指标

### 对用户
- ✅ TUI 在执行前显示友好错误（不再"运行后才知道失败"）
- ✅ 错误信息包含解决建议（不再"不知道如何修复"）
- ✅ 支持多种数据输入场景（自备 assembly/reads/masked）

### 对开发者
- ✅ 减少 60+ 行重复代码
- ✅ 添加新资源类型只需定义 spec（不需要写解析逻辑）
- ✅ 统一错误处理和 fallback 逻辑

### 对系统
- ✅ 零回归（79/79 测试通过）
- ✅ 清晰的三层架构（TUI/RunConfig/Stage）
- ✅ 预留 LLM 扩展接口（知识库驱动）

---

## 后续扩展（Phase 9+）

### 短期（1-2 个月）
1. **Assembly stage 重构** - 使用 `resolve_stage_resource()`
2. **Unified output keys** - 实施 `final_*_for_downstream`
3. **更多 resource specs** - HiC reads, RNA-seq reads, etc.

### 中期（3-6 个月）
1. **LLM-driven requirements** - 替换 hardcoded `STAGE_REQUIREMENTS`
2. **Knowledge base integration** - 三段论知识库查询
3. **Dynamic validation** - 根据 LLM 推理生成 suggestions

### 长期（6+ 个月）
1. **Auto-repair plans** - LLM 自动修复缺失资源
2. **Interactive data collection** - TUI 引导用户补充数据
3. **Multi-project support** - 跨项目资源共享

---

## 参考

- [DESIGN_SUMMARY.md](DESIGN_SUMMARY.md) - Phase 8 设计总结
- [PHASE_8.5_PROGRESS.md](PHASE_8.5_PROGRESS.md) - 当前进度
- [archive/RETHINK_RESOLVER_SCOPE.md](archive/RETHINK_RESOLVER_SCOPE.md) - Resolver 范围讨论
- [archive/ASSEMBLY_RESOLUTION_REFACTOR.md](archive/ASSEMBLY_RESOLUTION_REFACTOR.md) - 重构过程记录
