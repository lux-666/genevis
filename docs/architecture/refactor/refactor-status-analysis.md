# 重构手册可执行性分析报告

**生成日期：** 2026-07-19  
**分析目标：** `refactor-repair-manual.md` 可执行性验证  
**代码库状态：** `codex/feature-autohic-planner-bootstrap` 分支  
**修订日期：** 2026-07-19 - 采用 execution_mode 统一方案

> 📝 **修订说明：** 基于团队讨论，原"删除 local_exec"方案已修订为"统一到 execution_mode"方案。详见 `REVISION_NOTES.md`。本报告的工作量和风险评估已相应更新。

---

## 执行摘要

本次分析对重构手册进行了全面的代码库验证。通过搜索和检查实际代码，发现：

- **手册准确性：85%** - 大部分需要修改的组件确实存在
- **当前完成度：10-15%** - 部分基础设施已存在，但核心变更未开始
- **风险等级：中** - 采用 execution_mode 统一方案后风险可控
- **预估工作量：35-53 人天**（原 38-57 人天）

**关键发现：**
1. ✅ `local_exec`、`AssemblySelection`、`assembly_route` 等需要修改的组件确实存在
2. ❌ `resource_refs` 等核心新组件尚未实现
3. ⚠️ 手册遗漏了部分重要依赖和复杂度
4. 📋 建议采用分阶段、渐进式的重构策略
5. 🎯 **方案修订**：采用 execution_mode 统一方案，工作量减少3-4天，风险从高降到中

---

## 一、需要删除的组件验证

### 1.1 执行授权机制 ✅ 确认存在

| 组件 | 状态 | 位置 | 影响范围 |
|------|------|------|---------|
| `LocalExecStage` | ✅ 存在 | `app/schemas/run_config.py:39` | 1个文件 |
| `RunConfig.local_exec` | ✅ 存在 | `app/schemas/run_config.py:469` | 12+文件使用 |
| `local_execution_not_enabled` | ✅ 存在 | 4个 stage 文件 | 多处分支逻辑 |
| `validate_local_assembly_execution` | ✅ 存在 | `app/schemas/run_config.py:223` | 1个函数 |
| CLI `--local-exec` | ✅ 存在 | `app/entrypoints/run.py` | CLI 参数 |
| `execution_mode=local` | ✅ 存在 | 50+测试文件 | 大量测试代码 |

**影响评估：** 🟡 中 - 统一到 execution_mode 机制，测试只需简单替换参数值

### 1.2 Assembly 方法选择 ✅ 确认存在

| 组件 | 状态 | 位置 | 影响范围 |
|------|------|------|---------|
| `AssemblySelection` 类 | ✅ 存在 | `app/domains/assembly/selection.py:106` | 核心选择逻辑 |
| `assembly_selection` 字段 | ✅ 存在 | 12个文件 | 广泛使用 |
| `assembly_route` 字段 | ✅ 存在 | `app/schemas/run_config.py:202` | RunConfig 字段 |
| `DEFAULT_PRIMARY_BY_READ_TYPE` | ✅ 存在 | `app/domains/assembly/selection.py:95` | 默认方法映射 |

**影响评估：** 🔴 高 - 需要彻底重构 assembly 选择逻辑

### 1.3 CWD 路径问题 ✅ 确认存在

| 模式 | 出现次数 | 文件类型 | 影响 |
|------|---------|---------|------|
| `Path("descriptors/registry.yaml")` | 30+ | 生产代码10个，测试17个 | 🔴 关键 |
| `Path("config/envs.yaml")` | 2 | `env_resolver.py`, `run.py` | 🟡 中等 |

**关键文件：**
- `app/entrypoints/planner_catalog.py:12`
- `app/execution/env_resolver.py:24`
- 所有 stage 的 `nodes.py` 和 method adapter

**影响评估：** 🔴 高 - 影响所有使用 descriptor 的代码

### 1.4 资源交接机制 ⚠️ 部分存在

| 组件 | 状态 | 说明 |
|------|------|------|
| `prefer_stages` | ✅ 存在 | `app/stages/base.py:123` 和 `resource_specs.py` |
| `output_keys` | ✅ 存在 | `app/stages/base.py:126` 和 `resource_specs.py` |
| `final_*_for_downstream` | ✅ 存在 | 2个文件中使用 |
| `resource_refs` | ❌ 不存在 | **需要新建** |

**影响评估：** 🔴 高 - 核心架构变更，影响所有 stage

### 1.5 测试旁路 ✅ 确认存在

| 组件 | 状态 | 位置 |
|------|------|------|
| `has_explicit_path_override` | ✅ 存在 | `app/execution/env_dispatch.py:28` |
| "Fallback for tests" 注释 | ✅ 存在 | hifiasm.py, hifiasm_ont.py |
| `model_dump_json_compat` | ✅ 存在 | `app/schemas/warnings.py` |

**影响评估：** 🟡 中等 - 相对独立，但需要测试重写

---

## 二、需要建立的新组件状态

### 2.1 核心新组件

| 组件 | 状态 | 手册要求 | 实际情况 |
|------|------|---------|---------|
| `resource_refs` | ❌ 不存在 | state 中的中央资源表 | 完全未实现 |
| `load_descriptor_registry()` | ❌ 不存在 | 统一 facade | 每处都直接调用 |
| 统一 requires/produces | ⚠️ 部分 | 中央 contract | STAGE_REQUIREMENTS 已存在但未统一 |
| `STAGE_BUILDERS` mapping | ❌ 不存在 | Graph builder 使用 | 手动导入每个 builder |

### 2.2 已存在的基础设施（手册未识别）

| 组件 | 位置 | 用途 |
|------|------|------|
| `ResourceSpec` | `app/stages/base.py` | 资源规范基础类 |
| `STAGE_REQUIREMENTS` | `app/stages/resource_specs.py:103` | Stage 需求注册表 |
| `STAGE_OUTPUTS` | `app/stages/resource_specs.py:62` | Stage 输出注册表 |
| `STAGE_ORDER` | `app/workflow.py:10` | Workflow 顺序定义 |

**重要发现：** 手册假设需要"建立"某些组件，但它们已经存在，只是需要收窄和重构。

---

## 三、重构顺序可行性分析

### 原手册提出的9个 Commit

| Commit | 可行性 | 复杂度 | 依赖 | 风险 |
|--------|--------|--------|------|------|
| 1. Authority Invariants | ✅ 可行 | 低 | 无 | 低 |
| 2. 路径与 Registry Facade | ✅ 可行 | 中 | 无 | 低 |
| 3. 统一 execution_mode | ✅ 可行 | 中 | 无 | 中 - 测试简单替换 |
| 4. TUI 独占方法选择 | ⚠️ 复杂 | 高 | 无 | 高 - 核心逻辑 |
| 5. Resource Handoff | 🔴 最复杂 | 极高 | 无 | 极高 - 8个 stage |
| 6. Preflight Contract | ⚠️ 可行 | 高 | Commit 5 | 高 |
| 7. 删除测试旁路 | ✅ 可行 | 中 | 无 | 中 |
| 8. P2 去重 | ✅ 可行 | 中 | 可并行 | 低-中 |
| 9. 文档重写 | ✅ 可行 | 中 | 全部 | 低 |

### 关键问题

1. **Commit 3-6 依赖性不清晰** - 实际上 Commit 5 应该先于 Commit 3-4
2. **破坏性变更集中** - Commit 3-6 都是破坏性的，没有渐进策略
3. **测试策略缺失** - 没有说明如何在重构过程中保持测试通过

---

## 四、建议的修正重构顺序

### Phase 1：建立基础设施（无破坏性，2-3周）

**Commit 1a：创建统一 Descriptor/Env Config Loader Facade**
- 创建 `app/config.py` 或 `app/loaders.py`
- 提供 `load_descriptor_registry()` 和 `load_env_config()`
- 使用 `importlib.resources` 处理包资源
- **工作量：** 2-3天
- **风险：** 低

**Commit 1b：迁移所有调用点到新 Facade**
- 替换30+个 `Path("descriptors/registry.yaml")` 调用
- 替换2个 `Path("config/envs.yaml")` 调用
- 修正 `pyproject.toml` package data
- **工作量：** 2-3天
- **风险：** 低

**Commit 1c：添加 Authority Invariant 测试**
- workflow stage 集合与 graph builder mapping 一致性
- descriptor key 等于 method_name
- 内置资源与 CWD 无关
- **工作量：** 1-2天
- **风险：** 低

### Phase 2：建立新架构（渐进式，3-4周）

**Commit 2a：在 State 中添加 resource_refs（可选字段）**
- 在 state schema 中添加 `resource_refs: dict[str, Any] = Field(default_factory=dict)`
- 不影响现有逻辑
- **工作量：** 1天
- **风险：** 低

**Commit 2b：Inventory 填充 resource_refs**
- inventory 将 manifest assets 注册到 `resource_refs`
- 同时保留旧的 asset registry
- **工作量：** 1-2天
- **风险：** 低

**Commit 2c：逐个迁移 Stage 使用 resource_refs**
- 顺序：inventory → read_qc → genome_survey → assembly → dedup → review → hic → t2t
- 每个 stage 先尝试读取 `resource_refs`，失败则 fallback 到旧逻辑
- **工作量：** 8-12天（每个 stage 1-1.5天）
- **风险：** 中 - 每个 stage 独立测试

**Commit 2d：Preflight 改用 resource_refs**
- preflight 使用 `resource_refs` 做集合推演
- 保留对旧逻辑的 fallback
- **工作量：** 2-3天
- **风险：** 中

### Phase 3：删除旧机制（破坏性，4-5周）

**Commit 3a：删除 prefer_stages 和 output_keys**
- 确认所有 stage 已迁移到 `resource_refs`
- 从 `ResourceSpec` 删除 `prefer_stages` 和 `output_keys`
- 删除 `resolve_stage_resource` 中的旧逻辑
- **工作量：** 2-3天
- **风险：** 中 - 依赖 Phase 2 完成

**Commit 3b：删除 local_exec 机制**
- 从 RunConfig 删除 `local_exec` 字段
- 删除 `LocalExecStage` 枚举
- 删除 CLI `--local-exec` 参数
- 修改所有 stage routing 逻辑
- 重写50+个测试
- **工作量：** 5-7天
- **风险：** 高 - 大量测试需要更新

**Commit 3c：删除 AssemblySelection 和 Runtime 选择**
- TUI 输出 canonical method ID 和完整参数
- 删除 `AssemblySelection` 类
- 删除 `assembly_route` 字段
- 删除 `DEFAULT_PRIMARY_BY_READ_TYPE`
- Runtime 只做校验，不做选择
- **工作量：** 5-7天
- **风险：** 高 - 核心逻辑变更

**Commit 3d：删除测试旁路**
- 删除 `has_explicit_path_override`
- 删除 "Fallback for tests" 逻辑
- 统一执行边界为 descriptor + env config → EnvSpec → frozen spec
- 测试通过依赖注入
- **工作量：** 3-5天
- **风险：** 中

### Phase 4：清理和文档（1-2周）

**Commit 4a：P2 收敛项**
- LLM JSON 解析收敛
- Resume 协调收敛
- Graph/Lifecycle/Reports 依赖方向修正
- Ask → Run typed 调用
- 其他小 wrapper 删除
- **工作量：** 4-6天
- **风险：** 低-中

**Commit 4b：文档更新**
- README.md
- docs/architecture.md
- docs/stage_contracts.md
- docs/descriptor_spec.md
- docs/examples/*.yaml
- **工作量：** 2-3天
- **风险：** 低

---

## 五、遗漏的重构需求

### 5.1 Parameter 系统架构

**问题：** 手册提到删除 parameter catalog 默认值，但未明确 parameter catalog 的角色。

**当前状态：**
- `app/stages/assembly/parameters/catalog.py` 等文件存在
- Parameter catalog 描述类型、范围、枚举、互斥
- Descriptor 的 `parameter_defaults` 提供默认值
- Runtime schema 做校验

**需要明确：**
1. Parameter catalog 是否保留作为类型/约束定义？
2. Descriptor parameter_defaults 如何与 catalog 协作？
3. Method adapter 如何获取参数？

### 5.2 FOFN 处理策略

**问题：** 手册提到删除跨 stage FOFN 协议，但未说明：

1. 哪些 method adapter 需要 FOFN？（NextDenovo, purge_dups）
2. Canonical read refs 的格式是什么？
3. 如何从 canonical refs 生成 FOFN？
4. `app/domains/read_inputs.py` 如何迁移？

**当前使用 FOFN 的组件：**
- `app/stages/assembly/method_application/nextdenovo.py`
- `app/stages/assembly_dedup/purge_dups.py`
- `app/stages/read_qc/method_application/make_fofn.py`

### 5.3 测试迁移策略

**问题：** 50+个测试使用 `execution_mode=local`，手册未提供迁移指南。

**需要定义：**
1. 删除 `local_exec` 后，测试如何指定执行范围？
2. 测试中的依赖注入标准是什么？
3. 是否需要 test fixture 迁移工具？

### 5.4 向后兼容性

**问题：** 删除 `local_exec`、`assembly_route` 等字段后，旧 run config 如何处理？

**需要定义：**
1. 错误消息内容和格式
2. 是否需要提供迁移工具？
3. 文档中如何说明 breaking changes？

---

## 六、风险缓解建议

### 6.1 分阶段发布策略

1. **Phase 1 (基础设施)** - 低风险，可直接合并到主分支
2. **Phase 2 (新架构)** - 中风险，使用 feature flag 或专门分支
3. **Phase 3 (删除旧机制)** - 高风险，需要充分测试和回滚计划
4. **Phase 4 (清理)** - 低风险，可与 Phase 3 合并发布

### 6.2 测试策略

1. **Phase 1：** 确保所有现有测试继续通过
2. **Phase 2：** 对每个迁移的 stage，添加 `resource_refs` 相关测试
3. **Phase 3：** 在删除前，确保新机制覆盖所有旧机制的测试场景
4. **Phase 4：** 端到端测试矩阵（手册第9节）

### 6.3 回滚计划

1. **每个 Phase 完成后打 Git tag**
2. **建立回滚测试清单**
3. **Phase 2 保留 fallback 逻辑，允许快速回退**
4. **Phase 3 前建立完整的测试 baseline**

### 6.4 并行工作策略

可以并行进行的任务：
- Phase 1 (基础设施) 与 Phase 4 的部分任务（LLM parser, 小 wrapper 删除）
- Phase 2 的不同 stage 迁移（在完成 inventory 后）
- Phase 3 的不同删除任务（在 Phase 2 完成后）

---

## 七、工作量详细估算

| Phase | 任务 | 人天 | 风险 | 依赖 |
|-------|------|------|------|------|
| **Phase 1** | | | | |
| 1a | Loader Facade | 2-3 | 低 | 无 |
| 1b | 迁移调用点 | 2-3 | 低 | 1a |
| 1c | Authority 测试 | 1-2 | 低 | 无 |
| **Phase 1 小计** | | **5-8** | **低** | |
| **Phase 2** | | | | |
| 2a | 添加 resource_refs | 1 | 低 | 无 |
| 2b | Inventory 迁移 | 1-2 | 低 | 2a |
| 2c | Stage 迁移 (8个) | 8-12 | 中 | 2b |
| 2d | Preflight 迁移 | 2-3 | 中 | 2c |
| **Phase 2 小计** | | **12-18** | **中** | |
| **Phase 3** | | | | |
| 3a | 删除 prefer_stages | 2-3 | 中 | Phase 2 |
| 3b | 统一 execution_mode | 2-3 | 中 | 无 |
| 3c | 删除 AssemblySelection | 5-7 | 高 | 无 |
| 3d | 删除测试旁路 | 3-5 | 中 | 无 |
| **Phase 3 小计** | | **12-18** | **中-高** | |
| **Phase 4** | | | | |
| 4a | P2 收敛项 | 4-6 | 低-中 | 部分可并行 |
| 4b | 文档更新 | 2-3 | 低 | Phase 3 |
| **Phase 4 小计** | | **6-9** | **低-中** | |
| **总计** | | **35-53** | **中-高** | |

**关键路径：** Phase 1 → Phase 2 → Phase 3 → Phase 4  
**可并行：** Phase 1 + 部分 Phase 4，Phase 2 内部 stage 迁移  
**建议时间线：** 6-8周（考虑测试和缓冲）

---

## 八、优先级建议

### P0 - 立即执行（Week 1-2）

1. ✅ **创建这份分析报告** - 已完成
2. 🔄 **更新重构手册** - 基于本报告修正
3. 🔄 **Phase 1 Commit 1a-1c** - 建立基础设施

### P1 - 短期（Week 3-5）

4. **Phase 2 Commit 2a-2d** - 建立 resource_refs 机制
5. **补充遗漏需求文档** - Parameter 系统、FOFN 策略、测试迁移

### P2 - 中期（Week 6-8）

6. **Phase 3 Commit 3a-3d** - 删除旧机制
7. **完整测试验收** - 运行手册第9节测试矩阵

### P3 - 长期（Week 9-10）

8. **Phase 4 Commit 4a-4b** - 清理和文档
9. **最终验收** - 手册第10节删除完成检查

---

## 九、关键决策点

在开始实施前，需要明确以下决策：

### 决策 1：Parameter Catalog 的未来

**选项 A：** 保留 parameter catalog 作为类型/约束定义  
**选项 B：** 完全删除，所有信息合并到 descriptor  
**选项 C：** 保留但收窄为纯验证规则

**推荐：** 选项 A - 保留作为类型系统

### 决策 2：resource_refs 的实现细节

**选项 A：** Dict[str, Any] - 灵活但类型不安全  
**选项 B：** Dict[str, ResourceRef] - 需要定义 ResourceRef schema  
**选项 C：** 专门的 ResourceRegistry 类

**推荐：** 选项 B - 类型安全且可扩展

### 决策 3：测试迁移策略

**选项 A：** 一次性重写所有测试  
**选项 B：** 保留 execution_mode 作为测试专用字段  
**选项 C：** 渐进式迁移，Phase 2 完成前保留旧机制

**推荐：** 选项 C - 风险最低

### 决策 4：Breaking Changes 处理

**选项 A：** 立即拒绝旧 config（如手册建议）  
**选项 B：** 提供迁移工具自动转换  
**选项 C：** 支持有限期的兼容层

**推荐：** 选项 A + 清晰的错误消息和迁移指南

---

## 十、结论和行动建议

### 10.1 手册总体评估

| 维度 | 评分 | 说明 |
|------|------|------|
| **准确性** | 85% | 大部分组件验证正确，但遗漏已有基础设施 |
| **完整性** | 75% | 核心思路清晰，但遗漏部分依赖和细节 |
| **可行性** | 80% | 整体可行，但需要调整顺序和策略 |
| **工作量估算** | 65% | 偏乐观，未充分考虑测试和迁移成本 |
| **总体评分** | 76% | **良好但需要修正** |

### 10.2 立即行动项

1. **提交本分析报告** - 作为重构的 baseline 文档
2. **更新重构手册** - 基于本报告的发现进行修正
3. **补充遗漏需求** - 创建单独文档详细说明 Parameter 系统、FOFN 策略等
4. **与团队对齐** - 讨论关键决策点并达成共识
5. **启动 Phase 1** - 从低风险的基础设施建设开始

### 10.3 成功标准

重构成功的标志：
1. ✅ 所有手册第10节的删除检查项为零
2. ✅ 手册第9节的测试矩阵全部通过
3. ✅ 从仓库外 CWD 运行成功
4. ✅ 文档与代码完全一致
5. ✅ 没有引入性能回归
6. ✅ 所有现有功能继续正常工作

---

**报告结束**

*下一步：基于本报告更新 refactor-repair-manual.md，并创建 Phase 1 的具体实施计划。*
