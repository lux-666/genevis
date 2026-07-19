# 重构进展报告

**日期：** 2026-07-20  
**分支：** `refactor/unified-resource-execution`  
**状态：** 进行中 (已完成基础架构)

---

## 已完成的工作

### 1. 架构契约定义 ✅ (Commit: 71e4919)

**文件：** `docs/architecture/refactor/architecture-contracts.md`

**核心决策：**
- 保留 `execution_mode="local"` 并统一到这个设计
- 建立 `resource_refs` 中央资源表
- 统一方法选择边界（TUI 选择，runtime 验证）
- Runner 抽象（LocalRunner, FakeRunner）
- FOFN 作为 adapter 内部格式，不是跨 stage 协议

### 2. Resource_refs 中央资源表 ✅ (Commit: 7bfa0e3)

**新增文件：**
- `app/schemas/resource_ref.py` - ResourceRef 数据类
- `app/stages/resource_resolution.py` - 统一资源解析

**修改文件：**
- `app/schemas/state.py` - 添加 `resource_refs` 字段到 TopLevelState

**实现功能：**
- ResourceRef 数据类（resource_type, paths, source_stage, metadata）
- `resolve_resource()` 统一解析逻辑：override → resource_refs → asset_registry
- `update_resource_ref()` helper 用于 stage commit
- 简化的 ResourceSpec（移除 prefer_stages, output_keys）

### 3. Runner 抽象 ✅ (Commit: d7ac35a)

**新增文件：**
- `app/execution/fake_runner.py` - 测试 mock runner
- `app/execution/runner.py` - Runner protocol 和工厂函数

**实现功能：**
- Runner protocol 统一执行接口
- FakeRunner 记录命令但不执行（用于测试）
- `create_runner(execution_mode)` 工厂函数
  - `"local"` → LocalRunner（真实执行）
  - `"test"/"fake"` → FakeRunner（mock 执行）

### 4. Inventory Stage 迁移 ✅ (Commit: bac4c39)

**修改文件：**
- `app/stages/inventory/nodes.py`

**实现功能：**
- `_populate_resource_refs_from_assets()` 从 asset records 填充 resource_refs
- 映射 asset_type → 逻辑资源名：
  - `raw/clean_long_reads` → `long_reads`
  - `raw/clean_short_dna_reads` → `short_reads`
  - `hic_reads_*` → `hic_reads`
  - `assembly_fasta_registered` → `assembly`
- `register_assets()` 返回 resource_refs

---

## 当前架构状态

### 资源解析路径

**新路径（已实现）：**
```
resolve_resource(resource_spec) 
  → 1. RunConfig override
  → 2. state.resource_refs[resource_type]
  → 3. asset_registry
```

**旧路径（仍在使用）：**
```
resolve_stage_resource(resource_spec)
  → 1. RunConfig override
  → 2. prefer_stages output_keys 扫描
  → 3. 反向遍历 STAGE_ORDER
  → 4. asset_registry
```

### Stage 迁移状态

| Stage | resource_refs 填充 | 使用 resolve_resource | 使用 Runner |
|-------|-------------------|---------------------|-------------|
| inventory | ✅ 已完成 | N/A (不读取资源) | N/A (不执行工具) |
| read_qc | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |
| genome_survey | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |
| assembly | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |
| assembly_dedup | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |
| assembly_review | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |
| hic_scaffolding | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |
| t2t | ⏳ 待完成 | ⏳ 待完成 | ⏳ 待完成 |

---

## 下一步计划

### 优先级 1：完成最小纵向链路

**目标：** 迁移 inventory → read_qc → assembly，验证端到端工作

#### 任务 1.1：迁移 read_qc stage
- [ ] 修改 `enter_read_qc()` 使用 `resolve_resource()` 读取 `long_reads`
- [ ] 修改 stage 执行使用 Runner 抽象
- [ ] 修改 commit 函数更新 `resource_refs["long_reads"]`（过滤后的 reads）
- [ ] 保留 fallback 到旧机制以确保兼容

#### 任务 1.2：迁移 assembly stage
- [ ] 修改 `enter_assembly()` 使用 `resolve_resource()` 读取 `long_reads`, `genome_size_estimate`
- [ ] 修改 stage 执行使用 Runner 抽象
- [ ] 修改 commit 函数更新 `resource_refs["assembly"]`
- [ ] 保留 fallback 到旧机制

#### 任务 1.3：端到端测试
- [ ] 创建测试：inventory → read_qc → assembly 完整链路
- [ ] 验证 resource_refs 正确传递
- [ ] 验证 FakeRunner 在测试中正常工作
- [ ] 验证 LocalRunner 在真实场景中正常工作

### 优先级 2：移除重复机制

**前置条件：** 所有 stage 已迁移到新机制

- [ ] 删除 `base.py` 中的 `prefer_stages`, `output_keys` 相关代码
- [ ] 删除 `final_*_for_downstream` 模式
- [ ] 删除跨 stage FOFN key 搜索
- [ ] 验证旧机制已完全移除

### 优先级 3：统一方法选择

- [ ] 调查 AssemblySelection 当前使用
- [ ] 设计统一的方法选择边界
- [ ] 删除 runtime 的方法选择逻辑
- [ ] 保留 AssemblySelection 作为审计记录

### 优先级 4：修复 CWD 独立性

**独立任务，可并行进行**

- [ ] 创建统一 config loader
- [ ] 使用 importlib.resources 加载 descriptors
- [ ] 更新所有 `Path("descriptors/registry.yaml")` 调用点

### 优先级 5：文档更新

- [ ] 更新架构文档反映新契约
- [ ] 更新 stage 文档说明 resource_refs
- [ ] 创建迁移指南
- [ ] 更新示例配置

---

## 技术债务和风险

### 当前已知问题

1. **双轨运行**：新旧资源解析机制并存
   - **风险：** 可能导致不一致行为
   - **缓解：** 保持 fallback 逻辑直到全面迁移完成

2. **测试覆盖**：未添加新机制的测试
   - **风险：** 回归问题难以发现
   - **缓解：** 在完成最小链路后立即添加测试

3. **FOFN 处理**：仍未迁移
   - **风险：** NextDenovo, purge_dups 可能仍使用旧 FOFN 协议
   - **缓解：** 在迁移相关 stage 时一并处理

### 破坏性变更

**目前为止：无破坏性变更**

所有修改都是增量的，保留了旧机制的 fallback。破坏性变更将在优先级 2（移除重复机制）阶段进行。

---

## 验收标准（最终目标）

### 核心目标
- [ ] 只有一套资源解析逻辑（resource_refs → asset_registry）
- [ ] 只有一套方法选择逻辑（TUI 选择，runtime 验证）
- [ ] 只有一套执行抽象（Runner interface，execution_mode 映射）
- [ ] 从任意 CWD 可运行（config loader 独立）

### 自动化检查
```bash
# 应该返回 0 结果
rg "prefer_stages|output_keys" app/
rg "final_.*_for_downstream" app/
rg "assembly_input_fofn|filtered_reads_fofn" app/
rg 'Path\("descriptors/registry\.yaml"\)' app/
```

### 功能验证
- [ ] inventory → read_qc → assembly 链路端到端测试通过
- [ ] 使用 FakeRunner 的单元测试通过
- [ ] 从 `/tmp` 运行 `gene-agent inspect --help` 成功
- [ ] 所有现有测试继续通过

---

## Git 历史

```
bac4c39 feat: inventory stage populates resource_refs
d7ac35a feat: implement Runner abstraction with FakeRunner
7bfa0e3 feat: implement resource_refs central resource table
71e4919 docs: add unified architecture contracts for refactoring
```

**分支状态：** 所有变更已提交，working tree clean
