# 架构契约定义（重构统一版）

**版本：** 1.0  
**日期：** 2026-07-20  
**状态：** 实施中

---

## 1. 总体原则

本次重构采用**务实统一**策略，而非理想化重写：

1. **保留 `execution_mode="local"`** - 统一测试和生产的执行控制机制
2. **建立 `resource_refs` 中央资源表** - 消除多套资源查找逻辑
3. **统一方法选择边界** - TUI 选择，runtime 验证，消除重复选择代码
4. **Runner 抽象** - `execution_mode="local"` 映射到 FakeRunner，生产用 LocalRunner

---

## 2. 核心契约

### 2.1 State Schema 扩展

在现有 `TopLevelState` 中**新增**字段：

```python
class TopLevelState(TypedDict, total=False):
    # ... 现有字段保持不变 ...
    
    # 新增：中央资源引用表
    resource_refs: NotRequired[dict[str, ResourceRef]]
```

**ResourceRef 定义**：

```python
@dataclass
class ResourceRef:
    """中央资源引用，跨 stage 资源交接的唯一协议"""
    resource_type: str              # 'long_reads', 'assembly', 'masked_assembly' 等
    paths: list[str]                # 一个或多个文件路径
    source_stage: str | None        # 产生此资源的 stage 名称
    metadata: dict[str, Any]        # 可选元数据（size, count, quality 等）
```

**逻辑资源名定义**：

| 资源名 | 说明 | 典型来源 |
|--------|------|---------|
| `long_reads` | 长读长测序数据 | inventory, read_qc |
| `short_reads` | 短读长测序数据 | inventory |
| `assembly` | 组装结果（会被后续 stage 更新） | assembly → dedup → review → hic → t2t |
| `masked_assembly` | 掩码后的组装 | repeat_annotation |
| `predicted_proteins` | 预测的蛋白质 | gene_annotation |
| `hic_reads` | Hi-C 测序数据 | inventory |
| `genome_size_estimate` | 基因组大小估计 | genome_survey |

---

### 2.2 资源解析机制（统一后）

**resolve_stage_resource 新逻辑**：

```python
def resolve_stage_resource(
    state: TopLevelState,
    *,
    resource_spec: ResourceSpec,
    override: str | None = None,
) -> ResourceResolution:
    """
    统一的资源解析，按顺序查找：
    1. RunConfig 显式 override（如果提供）
    2. state.resource_refs[resource_type]（中央资源表）
    3. asset_registry（初始 manifest assets）
    
    不再扫描：prefer_stages, output_keys, 反向遍历 STAGE_ORDER
    """
    # 1. Override
    if override:
        return ResourceResolution(found=True, paths=[override], source='override', ...)
    
    # 2. 中央资源表
    if "resource_refs" in state:
        resource_ref = state["resource_refs"].get(resource_spec.resource_type)
        if resource_ref:
            return ResourceResolution(
                found=True,
                paths=resource_ref.paths,
                source=resource_ref.source_stage or 'resource_refs',
                source_type='resource_refs',
                metadata=resource_ref.metadata
            )
    
    # 3. Asset registry (初始 manifest)
    if resource_spec.asset_types:
        resolution = _check_asset_registry(state, resource_spec)
        if resolution.found:
            return resolution
    
    # 4. 资源缺失
    if resource_spec.required:
        raise ResourceNotFoundError(f"Required resource '{resource_spec.resource_type}' not found")
    
    return ResourceResolution(found=False, source='missing', ...)
```

**ResourceSpec 简化**：

```python
@dataclass
class ResourceSpec:
    resource_type: str          # 逻辑资源名
    asset_types: list[str]      # 作为 fallback 的 asset 类型
    required: bool = True
    allow_multiple: bool = False
    
    # 删除的字段：
    # ❌ prefer_stages: list[str]
    # ❌ output_keys: dict[str, list[str]]
```

---

### 2.3 Stage 资源契约

每个 stage 声明其资源需求和产出：

```python
# app/stages/resource_specs.py
STAGE_CONTRACTS = {
    "inventory": {
        "requires": [],
        "produces": ["long_reads", "short_reads", "hic_reads"],  # 根据 manifest
    },
    "read_qc": {
        "requires": ["long_reads"],
        "produces": ["long_reads"],  # 过滤后替换原资源
    },
    "genome_survey": {
        "requires": ["long_reads"],
        "produces": ["genome_size_estimate"],
    },
    "assembly": {
        "requires": ["long_reads", "genome_size_estimate"],
        "produces": ["assembly"],
    },
    "assembly_dedup": {
        "requires": ["assembly"],
        "produces": ["assembly"],  # 更新同一资源
    },
    "assembly_review": {
        "requires": ["assembly"],
        "produces": ["assembly"],  # 继续更新
    },
    "hic_scaffolding": {
        "requires": ["assembly", "hic_reads"],
        "produces": ["assembly"],
    },
    "t2t": {
        "requires": ["assembly"],
        "produces": ["assembly"],
    },
    "repeat_annotation": {
        "requires": ["assembly"],
        "produces": ["masked_assembly"],
    },
    "gene_annotation": {
        "requires": ["assembly"],  # 或 masked_assembly，contextual
        "produces": ["predicted_proteins"],
    },
}
```

**Stage commit 规则**：

```python
def commit_stage(state: TopLevelState, stage_name: str, outputs: dict):
    """
    Stage 完成后更新资源表
    """
    contract = STAGE_CONTRACTS[stage_name]
    
    # 根据契约更新 resource_refs
    for resource_type in contract["produces"]:
        if resource_type in outputs:
            state["resource_refs"][resource_type] = ResourceRef(
                resource_type=resource_type,
                paths=outputs[resource_type]["paths"],
                source_stage=stage_name,
                metadata=outputs[resource_type].get("metadata", {})
            )
    
    # 同时保留 stage_outputs 用于私有数据（指标、evidence 等）
    state["stage_outputs"][stage_name] = {
        "metrics": outputs.get("metrics", {}),
        "evidence_refs": outputs.get("evidence_refs", []),
        # 但不包含跨 stage 资源路径
    }
```

---

### 2.4 execution_mode 语义（统一设计）

**定义**：`execution_mode` 是控制 stage 是否真实执行工具的开关。

**取值**：
- `"local"` - 本地执行（测试模式或小规模运行）
- `"none"` 或缺失 - 不执行，仅规划

**使用位置**：
1. **RunConfig** 中声明哪些 stage 启用本地执行
2. **Stage output** 中记录该 stage 的执行模式
3. **Runner 选择** - `execution_mode="local"` 映射到 FakeRunner（测试）或 LocalRunner（生产）

**Runner 抽象**：

```python
class Runner(ABC):
    @abstractmethod
    def run(self, frozen_spec: FrozenAttemptSpec) -> ExecutionResult:
        """执行冻结的命令规范"""
        pass

class LocalRunner(Runner):
    """生产环境真实执行"""
    def run(self, frozen_spec: FrozenAttemptSpec) -> ExecutionResult:
        # 真实执行 subprocess
        return subprocess_run(frozen_spec.command, ...)

class FakeRunner(Runner):
    """测试环境模拟执行"""
    def run(self, frozen_spec: FrozenAttemptSpec) -> ExecutionResult:
        # 记录命令但不执行
        self.executed_commands.append(frozen_spec.command)
        return ExecutionResult(status="success", exit_code=0, ...)
```

**在 stage node 中使用**：

```python
def enter_assembly(
    state: TopLevelState,
    config: AssemblyConfig,
    runner: Runner | None = None
) -> AssemblyResult:
    """
    Assembly stage 入口
    """
    # 如果未提供 runner，根据 execution_mode 创建
    if runner is None:
        execution_mode = config.get("execution_mode", "none")
        if execution_mode == "local":
            runner = LocalRunner()
        else:
            # 规划模式，不执行
            return plan_only_result()
    
    # 使用统一的 runner 执行
    frozen_spec = freeze_assembly(state, config)
    result = runner.run(frozen_spec)
    
    return result
```

**测试中注入 FakeRunner**：

```python
def test_assembly_command(fake_runner):
    result = enter_assembly(state, config, runner=fake_runner)
    assert fake_runner.executed_commands[0] == ["hifiasm", "-t", "8", ...]
```

---

### 2.5 方法选择边界（统一）

**原则**：TUI 是唯一选择入口，runtime 只验证。

**TUI 职责**：
1. 读取 descriptor registry 获取可用方法
2. 根据 manifest read type 过滤兼容方法
3. 展示给用户，收集确认
4. 写入 run config：**canonical method ID + 完整参数**

**Runtime 职责**：
1. 读取 run config 中的 method ID
2. 验证 method 与 read type 兼容性
3. 验证参数完整性（使用 Pydantic schema）
4. **不选择、不补默认值、不根据 read type 切换方法**

**需要统一/删除的**：

1. **删除 runtime 的方法选择逻辑**：
   - `DEFAULT_PRIMARY_BY_READ_TYPE` 常量
   - 根据 read type 动态选择 method 的代码

2. **统一 AssemblySelection 的用途**：
   - 保留 `AssemblySelection` 模型，但只作为**审计记录**
   - 存储在 `stage_outputs["assembly"]` 中，记录选择来源和理由
   - **不作为 runtime 决策依据**

3. **删除 assembly_route 的运行时逻辑**：
   - `assembly_route` 只在 TUI 中作为用户偏好提示
   - Run config 中不再包含 `assembly_route`，只有明确的 `method` 字段

**新的 run config 格式**：

```yaml
# run_config.yaml
manifest_ref: manifest.yaml
requested_stages: [assembly]

assembly:
  method: assembly.candidate.hifiasm  # Canonical method ID
  execution_mode: local
  parameters:
    threads: 16
    kmer_size: 51
    genome_size: 2.3Gb
```

---

## 3. FOFN 处理策略

**原则**：FOFN 是工具适配器的内部序列化格式，不是跨 stage 协议。

**Canonical 表示**：`resource_refs["long_reads"].paths` 是 `list[str]`

**FOFN 生成**：在需要 FOFN 的 method adapter 内部生成

```python
# app/stages/assembly/method_application/nextdenovo.py
def freeze_nextdenovo(state, params):
    # 从中央资源表获取 reads
    read_refs = resolve_stage_resource(state, resource_spec=RAW_READS_SPEC)
    
    # 在 work dir 生成 FOFN
    work_dir = get_work_dir(state, "assembly", "nextdenovo")
    fofn_path = work_dir / "input.fofn"
    write_fofn(read_refs.paths, fofn_path)
    
    # 使用 FOFN 构建命令
    return FrozenAttemptSpec(
        command=["nextdenovo", "config.cfg"],
        config_content=f"input_fofn = {fofn_path}\n...",
        ...
    )
```

**删除**：
- `read_qc.make_fofn` 作为 workflow method
- 跨 stage 的 FOFN key 搜索（`assembly_input_fofn`, `filtered_reads_fofn`）

---

## 4. 迁移策略

### 阶段 1：建立新机制（不破坏现有）

1. 在 State 添加 `resource_refs` 字段（可选）
2. Inventory 填充 `resource_refs`
3. 实现新的 `resolve_stage_resource` 逻辑，保留 fallback 到旧机制
4. 实现 Runner 抽象（LocalRunner, FakeRunner）

### 阶段 2：迁移最小纵向链路

迁移 `inventory → read_qc → assembly`，验证端到端工作：
- Inventory 注册 `long_reads` 到 resource_refs
- read_qc 从 resource_refs 读取，更新 resource_refs
- assembly 从 resource_refs 读取，写入 `assembly`
- 使用 Runner 抽象执行

### 阶段 3：逐步移除旧机制

- 删除 `prefer_stages` 和 `output_keys` 扫描
- 删除 `final_*_for_downstream` 模式
- 统一 AssemblySelection 用途（只作审计，不作决策）
- 删除 runtime 的 method selection 逻辑
- 删除跨 stage FOFN 协议

### 阶段 4：CWD 独立性（独立任务）

- 创建统一 config loader
- 使用 importlib.resources 加载 descriptors
- 更新所有 `Path("descriptors/registry.yaml")` 调用点

---

## 5. 验收标准

### 核心目标
- [ ] 只有一套资源解析逻辑（resource_refs → asset_registry）
- [ ] 只有一套方法选择逻辑（TUI 选择，runtime 验证）
- [ ] 只有一套执行抽象（Runner interface，execution_mode 映射）
- [ ] 从任意 CWD 可运行（config loader 独立）

### 自动化检查
```bash
# 应该返回 0 结果（或只在明确标记的 archive 中出现）
rg "prefer_stages|output_keys" app/
rg "DEFAULT_PRIMARY_BY_READ_TYPE" app/
rg "assembly_input_fofn|filtered_reads_fofn" app/
rg 'Path\("descriptors/registry\.yaml"\)' app/
```

### 功能验证
- [ ] inventory → read_qc → assembly 链路端到端测试通过
- [ ] 使用 FakeRunner 的单元测试通过
- [ ] 从 `/tmp` 运行 `gene-agent inspect --help` 成功

---

## 6. 与原手册的差异

本契约与 `refactor-repair-manual.md` 的主要差异：

| 项目 | 原手册 | 本契约（务实版） |
|------|--------|-----------------|
| execution_mode | 完全删除 | **保留统一**，映射到 Runner |
| local_exec | 删除 | 合并到 execution_mode 语义 |
| AssemblySelection | 删除 | **保留**作为审计记录 |
| assembly_route | 删除 | TUI 内部使用，不进 run config |
| 测试策略 | 依赖注入 | **依赖注入 + execution_mode** |
| 兼容性 | 明确拒绝 | 同样明确拒绝 + 清晰指引 |

**核心思想**：不追求理想架构，而是**统一到一个可行的设计**，消除并行的多套机制。
