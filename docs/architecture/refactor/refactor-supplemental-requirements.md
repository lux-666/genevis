# 重构补充需求文档

**版本：** 1.0  
**日期：** 2026-07-19  
**关联文档：** `refactor-repair-manual.md`, `refactor-status-analysis.md`

---

## 1. 文档目的

本文档详细说明 `refactor-repair-manual.md` 中遗漏或未充分展开的重构需求，包括：
- Parameter 系统架构演进
- FOFN 处理策略
- 测试迁移方案
- 向后兼容性策略

这些需求对重构成功至关重要，必须在实施相关 Phase 前明确定义。

---

## 2. Parameter 系统架构

### 2.1 当前状态

Parameter 系统目前分散在三个层次：

| 层次 | 组件 | 职责 | 位置 |
|------|------|------|------|
| **Descriptor 层** | `planner.parameter_defaults` | 提供默认值 | `descriptors/*.yaml` |
| **Catalog 层** | Parameter Catalog | 类型、范围、枚举、互斥、命令映射 | `app/stages/*/parameters/catalog.py` |
| **Runtime 层** | Pydantic Schema | 运行时校验 | `app/stages/*/parameters/workflow.py` |

**问题：** 三层之间关系不清晰，存在重复定义和不一致。

### 2.2 目标架构

采用 **分层职责明确** 的架构：

```
┌─────────────────────────────────────┐
│   TUI / Planner                     │
│   - 读取 descriptor defaults        │
│   - 展示给用户                      │
│   - 收集确认后的值                  │
└──────────────┬──────────────────────┘
               │ 完整参数
               ▼
┌─────────────────────────────────────┐
│   Run Config                        │
│   - 存储完整参数                    │
│   - 序列化为 JSON/YAML              │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│   Runtime Validator                 │
│   - 使用 Pydantic Schema 校验       │
│   - 引用 Parameter Catalog 获取规则 │
│   - 必填字段，无默认值              │
└──────────────┬──────────────────────┘
               │ 校验通过
               ▼
┌─────────────────────────────────────┐
│   Method Adapter                    │
│   - 从 frozen spec 获取参数         │
│   - 构造工具命令行                  │
│   - 使用 Catalog 的命令映射         │
└─────────────────────────────────────┘
```

### 2.3 各层职责详细定义

#### Descriptor Layer

**职责：** 唯一的默认值来源

**内容：**
```yaml
# descriptors/assembly/hifiasm.yaml
method_name: assembly.candidate.hifiasm
stage: assembly
planner:
  parameter_defaults:
    threads: 16
    kmer_size: 51
    purge_level: 3
    # 注意：不包括 genome_size，因为它来自上游测量
```

**规则：**
- 只存储"没有上下文信息时的合理默认值"
- 不存储来自上游 stage 的值（如 genome_size）
- 不存储用户偏好（如特定项目的 threads）

#### Parameter Catalog

**职责：** 类型系统和命令映射

**保留内容：**
```python
# app/stages/assembly/parameters/catalog.py
ASSEMBLY_PARAMETERS = {
    "threads": ParameterSpec(
        type="integer",
        min_value=1,
        max_value=256,
        description="Number of threads",
        command_mapping="-t {value}"
    ),
    "kmer_size": ParameterSpec(
        type="integer",
        min_value=15,
        max_value=127,
        description="K-mer size for assembly graph",
        command_mapping="-k {value}"
    ),
    "genome_size": ParameterSpec(
        type="genome_size",
        description="Expected genome size",
        command_mapping="--genome-size {value}",
        # 注意：无 default，必须由 planner 或 runtime 提供
    ),
}
```

**删除内容：**
- ❌ `default` 字段（移到 descriptor）
- ❌ Parameter 推荐逻辑（移到 planner/LLM）

**新增内容：**
- ✅ 更严格的类型定义
- ✅ 参数间依赖关系（`requires`, `conflicts_with`）
- ✅ 条件性必填（例如：HiFi 模式下必须提供 X）

#### Runtime Schema

**职责：** 严格校验，不提供默认值

**实现：**
```python
# app/stages/assembly/parameters/workflow.py
class HifiasmParameters(BaseModel):
    threads: int  # 必填，无默认值
    kmer_size: int  # 必填，无默认值
    genome_size: str  # 必填，无默认值
    purge_level: int  # 必填，无默认值
    
    @validator("threads")
    def validate_threads(cls, v):
        spec = ASSEMBLY_PARAMETERS["threads"]
        if not (spec.min_value <= v <= spec.max_value):
            raise ValueError(f"threads must be in [{spec.min_value}, {spec.max_value}]")
        return v
    
    # 其他 validator 引用 catalog 的规则
```

**规则：**
- 所有执行参数必须为必填字段
- Validator 引用 Parameter Catalog 的规则，不重复定义
- 参数缺失时抛出明确错误，指出应由 planner 提供

### 2.4 上游测量值的处理

**问题：** genome_size 等值来自 genome_survey，不是"默认值"。

**解决方案：** Parameter Evidence 系统

```python
# state.parameter_evidence
{
    "assembly": {
        "genome_size": {
            "value": "2.3Gb",
            "source": "genome_survey",
            "source_stage": "genome_survey",
            "measurement_method": "GenomeScope2",
            "confidence": "high"
        }
    }
}
```

**流程：**
1. genome_survey commit 时写入 `state.parameter_evidence`
2. Planner 读取 evidence，展示给用户："检测到基因组大小 2.3Gb（来自 GenomeScope2）"
3. 用户可接受或覆盖
4. TUI 写入 run config 时注明来源：`{"genome_size": "2.3Gb", "source": "genome_survey"}`
5. Runtime 使用该值，evidence 可用于审计

### 2.5 实施计划

| 步骤 | 时间点 | 描述 |
|------|--------|------|
| 1 | Phase 1 | 增强 Parameter Catalog，删除 `default` 字段 |
| 2 | Phase 2 | 在 state 增加 `parameter_evidence` |
| 3 | Phase 2 | Genome survey commit 写入 evidence |
| 4 | Phase 3 | TUI 读取 descriptor defaults + evidence，输出完整参数 |
| 5 | Phase 3 | Runtime schema 改为必填，引用 catalog 规则 |
| 6 | Phase 4 | 文档更新，说明三层架构 |

---

## 3. FOFN 处理策略

### 3.1 问题描述

FOFN (File of Filenames) 是某些工具要求的输入格式，但不应作为跨 stage 资源传递协议。

**当前问题：**
- `read_qc.make_fofn` 作为 workflow method 参与 stage handoff
- 多个 stage 搜索 FOFN key（`assembly_input_fofn`, `filtered_reads_fofn` 等）
- FOFN 优先级和 suffix 猜测逻辑复杂

### 3.2 需要 FOFN 的工具（已验证）

| 工具 | 用途 | 位置 |
|------|------|------|
| NextDenovo | Assembly | `app/stages/assembly/method_application/nextdenovo.py` |
| purge_dups | Deduplication | `app/stages/assembly_dedup/purge_dups.py` |

### 3.3 目标架构

**Canonical Read Refs：**

```python
# state.resource_refs["long_reads"]
ResourceRef(
    resource_type="long_reads",
    paths=[
        Path("/data/reads/chunk1.fastq.gz"),
        Path("/data/reads/chunk2.fastq.gz"),
        Path("/data/reads/chunk3.fastq.gz"),
    ],
    source_stage="read_qc",
    metadata={
        "total_bases": 150000000000,
        "read_count": 5000000,
    }
)
```

**FOFN 生成：** 在 method adapter 内部按需生成

```python
# app/stages/assembly/method_application/nextdenovo.py
def freeze_nextdenovo(state, params):
    # Get canonical read refs
    read_refs = state.resource_refs["long_reads"]
    
    # Generate FOFN in work dir
    work_dir = get_work_dir(state, "assembly", "nextdenovo")
    fofn_path = work_dir / "input.fofn"
    
    with fofn_path.open("w") as f:
        for path in read_refs.paths:
            f.write(f"{path}\n")
    
    # Build command with FOFN
    return FrozenAttemptSpec(
        command=["nextdenovo", "config.cfg"],
        config_content=f"input_fofn = {fofn_path}\n...",
        ...
    )
```

### 3.4 FOFN Helper 函数

提供统一的 FOFN 生成函数：

```python
# app/domains/read_inputs.py (收窄后)
def write_fofn(
    read_paths: list[Path],
    output_path: Path,
    *,
    validate: bool = True
) -> Path:
    """Write a FOFN file for tools that require it.
    
    Args:
        read_paths: List of read file paths
        output_path: Where to write the FOFN
        validate: Whether to check paths exist
    
    Returns:
        Absolute path to the written FOFN
    """
    if validate:
        for path in read_paths:
            if not path.exists():
                raise FileNotFoundError(f"Read file not found: {path}")
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        for path in read_paths:
            f.write(f"{path.absolute()}\n")
    
    return output_path.absolute()
```

### 3.5 迁移计划

| 步骤 | 时间点 | 描述 |
|------|--------|------|
| 1 | Phase 2.2 | Inventory 将 manifest reads 注册为 path list |
| 2 | Phase 2.3 | read_qc commit 更新 `long_reads` 为过滤后的 paths |
| 3 | Phase 2.3 | NextDenovo adapter 改为从 resource_refs 生成 FOFN |
| 4 | Phase 2.3 | purge_dups adapter 改为从 resource_refs 生成 FOFN |
| 5 | Phase 3.1 | 删除 `read_qc.make_fofn` workflow method |
| 6 | Phase 3.1 | 删除跨 stage FOFN key 搜索 |
| 7 | Phase 4 | 收窄 `app/domains/read_inputs.py` 为纯 helper |

### 3.6 验收标准

- [ ] `resource_refs["long_reads"]` 存储 list[Path]，不存储 FOFN
- [ ] 需要 FOFN 的 adapter 在自己 work dir 生成
- [ ] 无跨 stage 的 FOFN 协议
- [ ] `rg "assembly_input_fofn|filtered_reads_fofn" app` 返回0结果

---

## 4. 测试迁移方案

> 📝 **方案修订：** 原方案"删除 execution_mode，使用 runner 注入"已修订为"统一到 execution_mode 机制"。详见 `REVISION_NOTES.md`。

### 4.1 当前测试状态（已验证）

- **50+ 测试文件** 使用 `execution_mode="local"`
- **测试模式：** 通过 `execution_mode` 控制是否真实执行工具
- **新方案：** 统一并规范化 execution_mode，无需重写测试

### 4.2 目标测试架构

**原则：** 保留并统一 execution_mode 机制

```python
# Before (Phase 2)
def test_assembly(tmp_path):
    result = run_assembly_stage(
        state=state,
        config=config,
        execution_mode="local"  # 旧值
    )

# After (Phase 3) - 简单替换
def test_assembly(tmp_path):
    result = run_assembly_stage(
        state=state,
        config=config,
        execution_mode="execute"  # 新值
    )
```

### 4.3 ExecutionMode 设计

```python
ExecutionMode = Literal["auto", "execute", "plan_only"]

# "auto" - 默认，根据命令判断
#   gene-agent run -> 执行
#   gene-agent plan -> 不执行

# "execute" - 强制执行（测试使用）
#   替代原来的 execution_mode="local"

# "plan_only" - 只规划不执行
#   preflight、dry-run
```

### 4.4 测试迁移模式

#### 模式 1：简单替换（大多数测试）

```python
def test_assembly_freezes_correct_command():
    """Test that assembly stage builds correct command."""
    state = create_test_state(...)
    config = AssemblyConfig(method="hifiasm", threads=8, ...)
    
    # 只需修改参数值
    result = run_assembly_stage(state, config, execution_mode="execute")
    
    # 断言保持不变
    assert result.status == "success"
    assert result.assembly_path.exists()
```

**迁移步骤：**
1. 全局搜索替换：`execution_mode="local"` → `execution_mode="execute"`
2. 验证测试通过
3. 无需修改测试逻辑

#### 模式 2：真实执行（少量关键端到端测试）

```python
@pytest.mark.integration
@pytest.mark.requires_tools("hifiasm")
def test_assembly_end_to_end_hifiasm(tmp_path):
    """End-to-end test with real hifiasm execution."""
    # 使用 execution_mode="execute" 并真实执行
    result = run_assembly_stage(
        state=state,
        config=config,
        execution_mode="execute"  # 真实执行
    )
    
    # Assert real output files exist
    assert result.assembly_path.exists()
    assert result.metrics["contig_count"] > 0
```

**标记规范：**
- `@pytest.mark.integration` - 需要真实工具
- `@pytest.mark.requires_tools("hifiasm", "samtools")` - 声明依赖的工具
- CI 中可选择性运行：`pytest -m "not integration"`

### 4.5 迁移计划

| 阶段 | 时间点 | 任务 | 工作量 |
|------|--------|------|--------|
| 1 | Phase 3.2 | 定义 ExecutionMode 类型 | 0.5天 |
| 2 | Phase 3.2 | 全局替换测试中的 execution_mode 值 | 0.5天 |
| 3 | Phase 3.2 | 验证所有测试通过 | 1天 |
| 4 | Phase 3.2 | 删除 `local_execution_not_enabled` 分支 | 1天 |
| **总计** | | | **3天**（原计划11天） |

### 4.6 验收标准

- [ ] ExecutionMode 类型定义完成（auto, execute, plan_only）
- [ ] 测试中 `execution_mode="local"` 全部替换为 `execution_mode="execute"`
- [ ] 所有测试通过
- [ ] 生产代码使用统一的 `should_execute_stage()` 逻辑

---

## 5. 向后兼容性策略

### 5.1 问题描述

Phase 3 统一到 `execution_mode` 后，使用 `local_exec` 字段的旧 run config 需要迁移。同时 `assembly_route` 等字段也将被删除。

**影响范围：**
- 用户保存的 run config 文件
- 文档中的示例
- 其他系统生成的 config

### 5.2 策略：明确拒绝 + 清晰指引

**原则：** 不提供自动迁移，但提供清晰的错误消息和手动迁移指南。

#### 错误消息设计

```python
# app/schemas/run_config.py
class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 拒绝未知字段
    
    @model_validator(mode="before")
    def check_legacy_fields(cls, values):
        """Detect legacy fields and provide migration guidance."""
        legacy_fields = {
            "local_exec": "The 'local_exec' field is no longer supported. "
                         "Use 'execution_mode' instead. Set execution_mode='execute' for stages you want to run. "
                         "See: docs/migration/local-exec-removal.md",
            
            "assembly_route": "The 'assembly_route' field is no longer supported. "
                             "Please specify the exact assembly method (e.g., 'assembly.candidate.hifiasm'). "
                             "See: docs/migration/method-selection.md",
            
            "assembly_selection": "The 'assembly_selection' field is no longer supported. "
                                 "Please specify method and parameters explicitly. "
                                 "See: docs/migration/method-selection.md",
        }
        
        found_legacy = []
        for field, message in legacy_fields.items():
            if field in values:
                found_legacy.append(f"  - {field}: {message}")
        
        if found_legacy:
            error_msg = (
                "This run config uses fields that were removed in the refactor:\n"
                + "\n".join(found_legacy)
                + "\n\nFor detailed migration instructions, see: docs/migration/README.md"
            )
            raise ValueError(error_msg)
        
        return values
```

#### 错误消息示例

```
$ gene-agent run --run-config old_config.json

Error: This run config uses fields that were removed in the refactor:
  - local_exec: The 'local_exec' field is no longer supported. Use 'execution_mode'
    instead. Set execution_mode='execute' for stages you want to run.
    See: docs/migration/local-exec-removal.md
  - assembly_route: The 'assembly_route' field is no longer supported. Please
    specify the exact assembly method (e.g., 'assembly.candidate.hifiasm').
    See: docs/migration/method-selection.md

For detailed migration instructions, see: docs/migration/README.md
```

### 5.3 迁移文档结构

创建 `docs/migration/` 目录：

```
docs/migration/
├── README.md                   # 总览和快速参考
├── local-exec-removal.md       # local_exec 迁移指南
├── method-selection.md         # assembly_route/selection 迁移指南
├── resource-refs.md            # 内部变更，用户无感
└── examples/
    ├── before.yaml             # 旧格式示例
    └── after.yaml              # 新格式示例
```

#### README.md 结构

```markdown
# Run Config 迁移指南

本次重构删除了一些字段，以建立更清晰的架构。本指南帮助您迁移旧的 run config。

## 快速参考

| 旧字段 | 新方案 | 详细文档 |
|--------|--------|---------|
| `local_exec` | 直接包含 method config | [local-exec-removal.md](local-exec-removal.md) |
| `assembly_route` | 指定 canonical method ID | [method-selection.md](method-selection.md) |
| `assembly_selection` | 完整 method 参数 | [method-selection.md](method-selection.md) |

## 迁移示例

### Before
```yaml
manifest_ref: manifest.yaml
requested_stages: [assembly]
local_exec: [assembly]
assembly_route: auto
```

### After
```yaml
manifest_ref: manifest.yaml
requested_stages: [assembly]
execution_mode: execute  # 替代 local_exec
assembly:
  method: assembly.candidate.hifiasm
  threads: 16
  kmer_size: 51
  genome_size: 2.3Gb
```

## 获取帮助

如果迁移遇到问题，请：
1. 查看具体字段的迁移文档
2. 参考 docs/examples/ 中的完整示例
3. 提交 issue 到 GitHub
```

### 5.4 TUI 升级路径

TUI 自动生成的 config 不受影响，因为：
- TUI 生成新格式 config
- 用户不直接编辑 TUI 生成的 config

**但需要处理：** 用户保存的 TUI 输出

```python
# app/entrypoints/ask_interactive.py
def load_previous_run_config(path: Path) -> RunConfig | None:
    """Load previous run config, with legacy detection."""
    try:
        return RunConfig.load(path)
    except ValueError as e:
        if "removed in the refactor" in str(e):
            console.print("[yellow]Warning:[/yellow] This config uses removed fields.")
            console.print(str(e))
            console.print("\n[cyan]Would you like to:")
            console.print("  1. Create a new config from scratch")
            console.print("  2. Exit and manually migrate")
            
            choice = Prompt.ask("Choice", choices=["1", "2"])
            if choice == "1":
                return None  # Start fresh
            else:
                raise SystemExit(1)
        raise
```

### 5.5 实施计划

| 步骤 | 时间点 | 描述 |
|------|--------|------|
| 1 | Phase 3 开始前 | 创建 `docs/migration/` 文档 |
| 2 | Phase 3.2 | 在 RunConfig 添加 legacy field 检测 |
| 3 | Phase 3.2 | 测试错误消息清晰度 |
| 4 | Phase 3.3 | 添加 `assembly_route`/`selection` 检测 |
| 5 | Phase 3 结束 | 更新所有 docs/examples/ |
| 6 | Phase 4 | 在 CHANGELOG 中说明 breaking changes |

### 5.6 验收标准

- [ ] 旧 config 加载时给出清晰错误消息
- [ ] 错误消息指向具体迁移文档
- [ ] 迁移文档提供完整的 before/after 示例
- [ ] TUI 能优雅处理旧 config
- [ ] CHANGELOG 记录所有 breaking changes

---

## 6. 风险和依赖关系

### 6.1 跨需求依赖

```
Parameter 系统
    ↓ (descriptor defaults)
FOFN 策略
    ↓ (resource_refs)
测试迁移
    ↓ (删除 execution_mode)
向后兼容
```

**关键依赖：**
1. FOFN 策略依赖 resource_refs（Phase 2）
2. 测试迁移必须在删除 local_exec 前完成（Phase 3.2）
3. 向后兼容文档必须在 Phase 3 开始前准备好

### 6.2 风险评估

| 需求 | 风险等级 | 主要风险 | 缓解措施 |
|------|---------|---------|---------|
| Parameter 系统 | 🟡 中 | 三层关系理解不一致 | 详细架构图和示例 |
| FOFN 策略 | 🟢 低 | 只影响2个 adapter | 逐个迁移并测试 |
| 测试迁移 | 🔴 高 | 工作量大（50+文件） | 分批迁移，每批独立验证 |
| 向后兼容 | 🟡 中 | 用户体验差 | 清晰错误消息和文档 |

### 6.3 未解决的问题

以下问题需要团队讨论（见 `refactor-decision-points.md`）：

1. **Parameter Catalog 是否完全保留？**
   - 还是合并到 descriptor？
   - 还是只保留验证规则？

2. **resource_refs 的 schema 设计？**
   - `Dict[str, Any]` vs `Dict[str, ResourceRef]`？
   - ResourceRef 包含哪些 metadata？

3. **测试迁移的激进程度？**
   - 是否保留少量 integration 测试真实执行？
   - CI 中如何处理需要真实工具的测试？

4. **向后兼容的友好程度？**
   - 是否提供自动迁移脚本？
   - 还是只提供文档？

---

## 7. 总结

本文档详细说明了重构手册中未充分展开的4个关键需求。这些需求对重构成功至关重要，必须在实施前与团队达成共识。

**下一步：**
1. 团队 review 本文档
2. 讨论并决策第6.3节的未解决问题（见 decision-points 文档）
3. 根据讨论结果更新本文档和重构手册
4. 开始 Phase 1 实施
