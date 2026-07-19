# 重构关键决策讨论文档

**版本：** 1.0  
**日期：** 2026-07-19  
**状态：** 待讨论  
**关联文档：** `refactor-repair-manual.md`, `refactor-supplemental-requirements.md`

---

## 文档目的

本文档列出重构过程中需要团队集体决策的关键问题。每个决策点包括：
- 问题描述和背景
- 可选方案及权衡分析
- 推荐方案及理由
- 对其他部分的影响

**使用方法：**
1. 团队会议前提前阅读
2. 会议中讨论每个决策点
3. 达成共识后在"决策结果"栏记录
4. 根据决策更新相关文档和代码

---

## 决策清单

| ID | 决策点 | 紧急度 | 影响范围 | 状态 |
|----|--------|--------|---------|------|
| D0 | 执行授权机制：统一 vs 删除 | 🔴 高 | 所有测试 | ✅ 已决策 |
| D1 | Parameter Catalog 的未来 | 🔴 高 | Parameter 系统 | 待讨论 |
| D2 | resource_refs 实现细节 | 🔴 高 | 所有 Stage | 待讨论 |
| D3 | 测试迁移策略 | 🟡 中 | 50+测试文件 | ✅ 已决策 |
| D4 | Breaking Changes 处理 | 🟡 中 | 用户体验 | 待讨论 |
| D5 | Phase 2-3 过渡策略 | 🟡 中 | 实施风险 | 待讨论 |
| D6 | Annotation Stage 处理 | 🟢 低 | 长期规划 | 延后 |

---

## 决策 D0：执行授权机制 - 统一 vs 删除（已决策）

### 背景

当前系统有多套执行控制机制并存：
- `local_exec` 列表
- `execution_mode` 参数（测试中）
- Stage-specific 执行判断

需要决定是删除旧机制并引入新机制，还是统一到现有机制。

### 问题

应该：
- A. 删除 `local_exec`，通过 runner 依赖注入控制测试
- B. 统一到 `execution_mode` 机制，规范化现有测试模式

### 选项分析

#### 选项 A：删除 local_exec，使用 runner 注入

**方案：**
- 删除所有 `local_exec` 相关代码
- 测试通过注入 `fake_runner` 控制执行
- 生产代码完全不识别"测试模式"

**优点：**
- ✅ 架构最纯粹（依赖注入）
- ✅ 生产代码与测试完全解耦

**缺点：**
- ❌ 需要重写50+测试文件
- ❌ 工作量大（5-7天）
- ❌ 风险高（容易遗漏测试场景）
- ❌ 需要实现 FakeRunner 基础设施

#### 选项 B：统一到 execution_mode（推荐，已采用）

**方案：**
```python
ExecutionMode = Literal["auto", "execute", "plan_only"]

# auto - 根据命令判断（默认）
# execute - 强制执行（测试用）
# plan_only - 只规划不执行
```

**优点：**
- ✅ 工作量小（2-3天）
- ✅ 风险低（简单参数替换）
- ✅ 语义清晰
- ✅ 统一现有机制而非推倒重来
- ✅ 测试只需改参数值，无需重写逻辑

**缺点：**
- ❌ 生产代码仍识别执行模式
- ❌ 架构纯度略逊于选项 A

### 决策结果

**日期：** 2026-07-19  
**决策：** 选项 B - 统一到 execution_mode  
**理由：** 
1. 工作量从5-7天降到2-3天，节省60%
2. 风险从高降到中
3. 50+测试文件只需简单替换，不需重写
4. `execution_mode` 语义清晰，易于理解
5. 实用主义：统一现有机制优于引入全新机制

**行动项：**
- [x] 更新重构手册（2.1节和Phase 3.2）
- [x] 创建 REVISION_NOTES.md 说明变更
- [x] 更新 status-analysis.md 工作量评估
- [x] 更新 supplemental-requirements.md 测试方案
- [ ] 实施 Phase 3.2

---

## 决策 D1：Parameter Catalog 的未来

### 背景

当前 Parameter 系统分散在三层：
- Descriptor `parameter_defaults` - 默认值
- Parameter Catalog - 类型、范围、命令映射
- Runtime Schema - Pydantic 校验

重构要求"唯一默认值来源"，但 Catalog 的其他职责如何处理？

### 问题

Parameter Catalog 应该：
- 完全保留（删除 defaults）？
- 完全删除（合并到 descriptor）？
- 部分保留（只保留验证规则）？

### 选项分析

#### 选项 A：保留 Parameter Catalog 作为类型系统（推荐）

**方案：**
```python
# Catalog 保留类型、范围、命令映射
ASSEMBLY_PARAMETERS = {
    "threads": ParameterSpec(
        type="integer",
        min_value=1,
        max_value=256,
        command_mapping="-t {value}",
        description="Number of threads"
    ),
    # 不包含 default
}

# Descriptor 提供默认值
# descriptors/assembly/hifiasm.yaml
planner:
  parameter_defaults:
    threads: 16

# Runtime 引用 Catalog 做验证
class HifiasmParameters(BaseModel):
    threads: int  # 必填
    
    @validator("threads")
    def validate_threads(cls, v):
        spec = ASSEMBLY_PARAMETERS["threads"]
        return spec.validate(v)
```

**优点：**
- ✅ 类型和验证规则集中管理
- ✅ 命令映射可复用（多个 method 可能用同一参数）
- ✅ Runtime schema 可以引用而非重复定义
- ✅ 易于扩展（如参数间依赖关系）

**缺点：**
- ❌ 保留了一个中间层，增加理解成本
- ❌ Catalog 和 descriptor 需要同步维护

**影响：**
- Descriptor 只存默认值
- Runtime schema 变薄，只做组合和业务校验
- Method adapter 从 Catalog 获取命令映射

#### 选项 B：完全删除 Parameter Catalog

**方案：**
```python
# 所有信息移到 descriptor
# descriptors/assembly/hifiasm.yaml
parameters:
  threads:
    type: integer
    min: 1
    max: 256
    default: 16
    command_mapping: "-t {value}"
    description: "Number of threads"

# Runtime 直接使用 Pydantic 定义
class HifiasmParameters(BaseModel):
    threads: int = Field(ge=1, le=256)
```

**优点：**
- ✅ 更简单，只有两层（descriptor 和 runtime）
- ✅ 所有 method 信息在一个文件中

**缺点：**
- ❌ 重复定义：多个 method 共享的参数（如 threads）重复定义
- ❌ 命令映射分散在多个 descriptor
- ❌ 类型信息用 YAML 描述，不如 Python 强类型
- ❌ Runtime 需要重复验证逻辑

**影响：**
- 删除所有 `app/stages/*/parameters/catalog.py`
- Descriptor 变复杂，需要定义类型系统
- Method adapter 需要从 descriptor 读取命令映射

#### 选项 C：只保留验证规则，删除命令映射

**方案：**
```python
# Catalog 只保留验证
ASSEMBLY_PARAMETERS = {
    "threads": ParameterSpec(
        type="integer",
        min_value=1,
        max_value=256,
    ),
}

# 命令映射移到 method adapter
# app/stages/assembly/method_application/hifiasm.py
def build_command(params):
    return [
        "hifiasm",
        "-t", str(params.threads),
        "-k", str(params.kmer_size),
        ...
    ]
```

**优点：**
- ✅ 验证规则集中管理
- ✅ 命令映射在实际使用处，更直观

**缺点：**
- ❌ 命令映射分散，难以统一修改
- ❌ Catalog 职责不清晰（为何只保留验证？）

**影响：**
- Catalog 收窄
- Method adapter 负责命令构造

### 推荐方案

**选项 A：保留 Parameter Catalog 作为类型系统**

**理由：**
1. **避免重复**：多个 method 共享的参数（threads, genome_size 等）只定义一次
2. **强类型**：Python 定义比 YAML 描述更可靠
3. **可扩展**：未来可以添加参数间依赖、条件性必填等
4. **职责清晰**：Descriptor=默认值，Catalog=类型系统，Runtime=业务校验

### 决策结果

**日期：** _待填写_  
**决策：** _待填写_  
**理由：** _待填写_  
**行动项：** _待填写_

---

## 决策 D2：resource_refs 实现细节

### 背景

Phase 2 需要在 state 中添加 `resource_refs` 作为中央资源表。需要确定其数据结构。

### 问题

`resource_refs` 应该使用什么数据结构？

### 选项分析

#### 选项 A：Dict[str, Any]

**方案：**
```python
class State(BaseModel):
    resource_refs: dict[str, Any] = Field(default_factory=dict)

# 使用
state.resource_refs["long_reads"] = [Path("read1.fq"), Path("read2.fq")]
state.resource_refs["assembly"] = Path("assembly.fasta")
```

**优点：**
- ✅ 最灵活，可以存任何类型
- ✅ 实现简单快速

**缺点：**
- ❌ 类型不安全，容易出错
- ❌ 无法知道 resource 的 metadata（来源、类型等）
- ❌ 难以扩展

**影响：**
- 快速实现
- 需要大量类型检查代码
- 难以维护

#### 选项 B：Dict[str, ResourceRef]（推荐）

**方案：**
```python
class ResourceRef(BaseModel):
    """Reference to a workflow resource."""
    resource_type: str  # "long_reads", "assembly", etc.
    paths: list[Path]  # One or more file paths
    source_stage: str | None = None  # Which stage produced it
    metadata: dict[str, Any] = Field(default_factory=dict)

class State(BaseModel):
    resource_refs: dict[str, ResourceRef] = Field(default_factory=dict)

# 使用
state.resource_refs["long_reads"] = ResourceRef(
    resource_type="long_reads",
    paths=[Path("read1.fq"), Path("read2.fq")],
    source_stage="inventory",
    metadata={"total_bases": 150000000000}
)
```

**优点：**
- ✅ 类型安全，Pydantic 自动校验
- ✅ 包含 metadata，可追溯来源
- ✅ 易于扩展（添加字段不破坏现有代码）
- ✅ 自文档化（schema 就是文档）

**缺点：**
- ❌ 略微复杂
- ❌ 需要定义 ResourceRef schema

**影响：**
- 需要创建 `ResourceRef` 类
- Stage commit 需要构造 ResourceRef
- 更好的类型提示和 IDE 支持

#### 选项 C：专门的 ResourceRegistry 类

**方案：**
```python
class ResourceRegistry:
    """Central resource registry with validation and querying."""
    
    def __init__(self):
        self._resources: dict[str, ResourceRef] = {}
    
    def register(self, name: str, ref: ResourceRef):
        """Register a resource."""
        self._resources[name] = ref
    
    def get(self, name: str) -> ResourceRef | None:
        """Get a resource by name."""
        return self._resources.get(name)
    
    def get_required(self, name: str) -> ResourceRef:
        """Get a required resource, raise if missing."""
        if name not in self._resources:
            raise ResourceNotFoundError(f"Required resource '{name}' not found")
        return self._resources[name]
    
    def list_available(self) -> list[str]:
        """List all available resource names."""
        return list(self._resources.keys())

class State(BaseModel):
    resource_registry: ResourceRegistry = Field(default_factory=ResourceRegistry)
```

**优点：**
- ✅ 封装了查询和验证逻辑
- ✅ 可以添加复杂的查询方法
- ✅ 更好的错误消息

**缺点：**
- ❌ 过度设计（当前需求不需要这么复杂）
- ❌ Pydantic 序列化需要自定义 serializer
- ❌ 增加理解成本

**影响：**
- 需要实现 ResourceRegistry 类
- State 序列化需要特殊处理
- 可能过度工程化

### 推荐方案

**选项 B：Dict[str, ResourceRef]**

**理由：**
1. **平衡**：比 Dict[str, Any] 更安全，比 ResourceRegistry 更简单
2. **类型安全**：Pydantic 提供自动校验
3. **可扩展**：ResourceRef 可以逐步添加字段
4. **标准做法**：Pydantic 的常见模式

**ResourceRef 初始设计：**
```python
class ResourceRef(BaseModel):
    """Reference to a workflow resource.
    
    Attributes:
        resource_type: Logical resource type (long_reads, assembly, etc.)
        paths: One or more file paths
        source_stage: Stage that produced this resource (None for initial assets)
        metadata: Additional resource-specific metadata
    """
    resource_type: str
    paths: list[Path]
    source_stage: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    
    @validator("paths")
    def paths_not_empty(cls, v):
        if not v:
            raise ValueError("Resource must have at least one path")
        return v
```

### 决策结果

**日期：** _待填写_  
**决策：** _待填写_  
**理由：** _待填写_  
**行动项：** _待填写_

---

## 决策 D3：测试迁移策略（已决策）

### 背景

50+测试文件使用 `execution_mode="local"`。需要决定如何处理这些测试。

**注：** 此决策与 D0 相关，D0 决定统一到 execution_mode 后，此决策自动确定。

### 问题

测试迁移应该多激进？

### 决策结果

**日期：** 2026-07-19  
**决策：** 简单替换 execution_mode 参数值  
**理由：** 已决定统一到 execution_mode 机制（D0），因此测试只需：
- 全局替换：`execution_mode="local"` → `execution_mode="execute"`
- 无需重写测试逻辑
- 工作量约1天

**行动项：**
- [x] 更新测试迁移文档
- [ ] 准备全局替换脚本
- [ ] 在 Phase 3.2 执行迁移

### 选项分析

#### 选项 A：全部改为 Mock（激进）

**方案：**
- 所有测试使用 `fake_runner`
- 删除所有真实工具执行
- 测试只验证命令构造和参数传递

**优点：**
- ✅ 测试运行极快（秒级）
- ✅ 不依赖真实工具，CI 简单
- ✅ 架构最纯粹

**缺点：**
- ❌ 不测试真实工具集成
- ❌ 无法发现工具版本变化导致的问题
- ❌ 无法验证命令行参数实际可用

**影响：**
- 需要其他方式验证真实集成（手动测试？）
- 可能错过工具兼容性问题

#### 选项 B：保留少量集成测试（推荐）

**方案：**
- 大部分测试（~45个文件）改为 Mock
- 保留关键路径的集成测试（~5个文件）
- 集成测试标记 `@pytest.mark.integration`

**集成测试覆盖：**
- ONT + hifiasm_ont 完整流程
- HiFi + hifiasm 完整流程
- Assembly → Dedup → Review 资源传递
- 一个完整的 end-to-end 场景

**优点：**
- ✅ 平衡速度和覆盖
- ✅ 可以发现真实集成问题
- ✅ CI 可以选择性运行（日常跳过，发布前运行）

**缺点：**
- ❌ 需要维护工具环境
- ❌ 集成测试可能不稳定

**影响：**
- CI 需要支持条件运行
- 需要文档说明如何运行集成测试

#### 选项 C：分层测试策略（复杂）

**方案：**
- Unit 测试：完全 Mock
- Integration 测试：真实工具，隔离运行
- E2E 测试：完整流程，真实数据

**优点：**
- ✅ 测试金字塔最佳实践
- ✅ 各层职责清晰

**缺点：**
- ❌ 复杂度高，维护成本大
- ❌ 当前项目规模可能不需要

**影响：**
- 需要重新组织测试目录
- 需要不同的 CI 策略

### 推荐方案

**选项 B：保留少量集成测试**

**理由：**
1. **实用主义**：大部分测试快速，关键路径有保障
2. **风险管理**：避免工具兼容性问题
3. **实施成本**：相比选项C更简单

**集成测试标准：**
```python
@pytest.mark.integration
@pytest.mark.requires_tools("hifiasm", "samtools")
def test_assembly_hifiasm_integration(tmp_path):
    """Integration test: real hifiasm execution."""
    # Real execution, no mock
    ...

# CI 运行
pytest -m "not integration"  # 日常开发
pytest -m "integration"      # 发布前
pytest                       # 运行所有
```

### 决策结果

**日期：** _待填写_  
**决策：** _待填写_  
**理由：** _待填写_  
**行动项：** _待填写_

---

## 决策 D4：Breaking Changes 处理

### 背景

删除 `local_exec`、`assembly_route` 等字段会破坏旧的 run config。

### 问题

如何处理向后兼容性？是否提供自动迁移工具？

### 选项分析

#### 选项 A：仅文档，不提供自动迁移（推荐）

**方案：**
- 旧 config 加载时明确拒绝
- 提供清晰的错误消息
- 提供详细的迁移文档
- 提供 before/after 示例

**优点：**
- ✅ 实现简单
- ✅ 不引入迁移代码的维护负担
- ✅ 强制用户理解新架构

**缺点：**
- ❌ 用户体验稍差（需要手动修改）
- ❌ 批量迁移需要人工处理

**影响：**
- 需要高质量的迁移文档
- 需要清晰的错误消息

#### 选项 B：提供自动迁移脚本

**方案：**
```bash
gene-agent migrate-config old_config.json > new_config.json
```

**优点：**
- ✅ 用户体验好
- ✅ 批量迁移容易

**缺点：**
- ❌ 需要实现迁移逻辑
- ❌ 某些场景无法自动迁移（如 `assembly_route: auto`）
- ❌ 迁移代码需要长期维护
- ❌ 可能给用户错误的"自动迁移总能工作"印象

**影响：**
- 增加开发工作量
- 增加测试覆盖范围
- 需要维护迁移逻辑

#### 选项 C：兼容层（不推荐）

**方案：**
- 保留旧字段，但标记为 deprecated
- 运行时自动转换为新格式
- 一段时间后删除兼容层

**优点：**
- ✅ 用户无感知过渡

**缺点：**
- ❌ 违反重构原则（"不保留兼容层"）
- ❌ 增加代码复杂度
- ❌ 延长过渡期，混淆新旧架构
- ❌ 最终还是要删除，只是延后了痛苦

**影响：**
- 违背重构目标
- 不推荐

### 推荐方案

**选项 A：仅文档，不提供自动迁移**

**理由：**
1. **符合重构原则**：明确失败，不静默转换
2. **简单可靠**：无迁移代码的 bug 风险
3. **受影响用户少**：大多数用户使用 TUI，不手写 config
4. **教育意义**：促使用户理解新架构

**需要提供：**
- 清晰的错误消息（指向具体迁移文档）
- 完整的迁移文档（before/after 示例）
- CHANGELOG 详细说明 breaking changes
- FAQ 覆盖常见迁移问题

### 决策结果

**日期：** _待填写_  
**决策：** _待填写_  
**理由：** _待填写_  
**行动项：** _待填写_

---

## 决策 D5：Phase 2-3 过渡策略

### 背景

Phase 2 建立新机制但保留旧逻辑，Phase 3 删除旧机制。如何确保过渡平滑？

### 问题

Phase 2 应该维持多久的"双轨运行"？何时开始 Phase 3？

### 选项分析

#### 选项 A：最短过渡期（2周）

**方案：**
- Phase 2 完成后立即验收
- 验收通过后 1-2周内启动 Phase 3
- 在 staging 环境运行 1周

**优点：**
- ✅ 重构进度快
- ✅ 减少双轨维护时间

**缺点：**
- ❌ 风险高，可能遗漏边界场景
- ❌ 回滚成本高

**适用场景：**
- 团队对代码非常熟悉
- 测试覆盖充分

#### 选项 B：标准过渡期（4周）（推荐）

**方案：**
- Phase 2 完成后验收
- 在 staging 环境运行 2-3周
- 监控 fallback 频率和错误
- 修复发现的问题
- 验证稳定后启动 Phase 3

**优点：**
- ✅ 风险可控
- ✅ 有时间发现和修复问题
- ✅ 团队可以熟悉新机制

**缺点：**
- ❌ 时间较长
- ❌ 需要维护双轨代码

**适用场景：**
- 标准情况，推荐

#### 选项 C：生产验证（6-8周）

**方案：**
- Phase 2 完成后在生产环境运行
- Feature flag 控制新旧机制
- 收集真实用户反馈
- 6-8周后启动 Phase 3

**优点：**
- ✅ 最充分的验证
- ✅ 真实用户数据

**缺点：**
- ❌ 时间很长
- ❌ 需要实现 feature flag
- ❌ 双轨代码维护负担重

**适用场景：**
- 关键系统，风险厌恶

### 推荐方案

**选项 B：标准过渡期（4周）**

**理由：**
1. **平衡**：既有充分验证，又不过度延长
2. **实用**：2-3周足以发现大部分问题
3. **成本**：不需要 feature flag 等复杂机制

**Phase 2-3 过渡检查清单：**
- [ ] 所有 Phase 2 任务完成
- [ ] 端到端测试通过
- [ ] 在 staging 运行至少 2周
- [ ] Fallback 频率 < 5%（说明新机制已充分覆盖）
- [ ] 无 P0/P1 bug
- [ ] 团队对新机制已熟悉
- [ ] Phase 3 计划已准备好

**监控指标：**
- 每个 stage 的 resource_refs 使用率
- Fallback 到旧机制的次数和原因
- 资源找不到的错误数量
- 性能指标（确保无回归）

### 决策结果

**日期：** _待填写_  
**决策：** _待填写_  
**理由：** _待填写_  
**行动项：** _待填写_

---

## 决策 D6：Annotation Stage 处理（延后讨论）

### 背景

重构手册明确说明 annotation 相关 stage 的业务重构延后。但共享 schema 变更会影响它们。

### 问题

Annotation stage 在本次重构中应该：
- 完全不碰？
- 做最小机械调整？
- 一起重构？

### 临时决策

**当前方案：** 做最小机械调整，业务重构延后

**机械调整包括：**
- 更新 import 路径（如果共享模块移动）
- 更新 schema 签名（如果接口变化）
- 保持编译通过

**不包括：**
- Resource handoff 迁移（继续使用旧机制）
- 循环依赖修复
- Review 逻辑重构

### 长期规划

Annotation 专门重构应该在 Phase 4 完成后启动，作为独立项目。

### 决策结果

**日期：** _待填写_  
**决策：** 延后到主线重构完成后  
**理由：** 降低本次重构复杂度  
**行动项：** Phase 4 完成后创建 annotation 重构计划

---

## 附录：决策模板

当需要新增决策点时，使用此模板：

```markdown
## 决策 DX：[决策标题]

### 背景

[为什么需要这个决策]

### 问题

[需要决策的核心问题]

### 选项分析

#### 选项 A：[选项名称]

**方案：**
[详细描述]

**优点：**
- ✅ [优点1]
- ✅ [优点2]

**缺点：**
- ❌ [缺点1]
- ❌ [缺点2]

**影响：**
[对其他部分的影响]

### 推荐方案

[推荐选项及理由]

### 决策结果

**日期：** _待填写_  
**决策：** _待填写_  
**理由：** _待填写_  
**行动项：** _待填写_
```

---

## 决策流程

1. **提出决策**：任何团队成员可以提出新决策点
2. **准备材料**：按模板补充背景、选项分析
3. **团队讨论**：在会议中讨论，收集意见
4. **达成共识**：通过投票或共识达成决策
5. **记录结果**：在"决策结果"栏记录日期、决策、理由
6. **执行行动项**：根据决策更新相关文档和代码
7. **定期 review**：每个 Phase 结束时 review 相关决策

---

## 下一步

1. ✅ **已完成** - 创建决策文档
2. 📋 **待安排** - 团队决策会议
3. 📋 **会议后** - 根据决策更新所有相关文档
4. 📋 **会议后** - 开始 Phase 1 实施
