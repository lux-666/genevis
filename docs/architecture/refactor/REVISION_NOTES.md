# 重构方案修订说明

**日期：** 2026-07-19  
**修订版本：** 2.0  
**关键变更：** 采用 `execution_mode` 统一方案替代"删除 local_exec"方案

---

## 修订原因

经过团队讨论，决定采用更务实的方案：

**原方案：** 删除 `local_exec`，通过 runner 依赖注入控制测试执行  
**新方案：** 统一到 `execution_mode` 机制，保留并规范化现有测试模式

---

## 关键变更对比

| 方面 | 原方案 | 新方案 | 影响 |
|------|--------|--------|------|
| **执行控制** | 删除 local_exec，通过 runner 注入 | 统一到 execution_mode | ✅ 更清晰 |
| **测试修改** | 重写50+测试文件，改用 fake_runner | 简单替换参数值 | ✅ 工作量大减 |
| **工作量** | 5-7天 | 2-3天 | ✅ 减少60% |
| **风险** | 高（大量测试重写） | 中（参数替换） | ✅ 风险降低 |
| **架构纯度** | 更纯粹（依赖注入） | 实用主义 | ⚠️ 稍妥协 |

---

## 新的 execution_mode 设计

### 值定义

```python
ExecutionMode = Literal["auto", "execute", "plan_only"]
```

- **`auto`** (默认) - 根据命令自动判断
  - `gene-agent run` → 执行
  - `gene-agent plan` → 不执行
  
- **`execute`** - 强制执行
  - 用于测试中明确需要执行
  - 替代原来的 `execution_mode="local"`
  
- **`plan_only`** - 只规划不执行
  - preflight 检查
  - dry-run 模式

### 统一的执行判断

```python
def should_execute_stage(
    stage_name: str,
    method_config: MethodConfig | None,
    execution_mode: ExecutionMode
) -> bool:
    """Unified execution authorization logic."""
    if method_config is None:
        return False
    if method_config.is_planner_only:
        return False
    if execution_mode == "plan_only":
        return False
    return True
```

---

## 迁移路径

### Phase 3.2 任务更新

**原任务：** 删除 local_exec 机制（5-7天）

**新任务：** 统一执行授权到 execution_mode（2-3天）

1. 定义 `ExecutionMode` 类型
2. 在 RunConfig 添加 `execution_mode` 字段
3. CLI 添加 `--execution-mode` 选项
4. 实现统一的 `should_execute_stage()` 函数
5. 各 stage 调用该函数
6. 测试中全局替换：`execution_mode="local"` → `execution_mode="execute"`
7. 弃用 `local_exec` 字段（给出迁移提示）
8. 过渡期后删除 `local_exec`

### 测试迁移

**原方案（复杂）：**
```python
# Before
def test_assembly(tmp_path):
    result = run_assembly(state, config, execution_mode="local")

# After - 需要重写
def test_assembly(tmp_path, fake_runner):
    result = run_assembly(state, config, runner=fake_runner)
    assert fake_runner.executed_commands[0] == [...]
```

**新方案（简单）：**
```python
# Before
def test_assembly(tmp_path):
    result = run_assembly(state, config, execution_mode="local")

# After - 只改参数值
def test_assembly(tmp_path):
    result = run_assembly(state, config, execution_mode="execute")
```

---

## 工作量变化

### Phase 3 工作量对比

| 任务 | 原工作量 | 新工作量 | 节省 |
|------|---------|---------|------|
| 3.1 删除 prefer_stages | 2-3天 | 2-3天 | 0 |
| 3.2 执行授权 | 5-7天 | 2-3天 | 3-4天 |
| 3.3 删除 AssemblySelection | 5-7天 | 5-7天 | 0 |
| 3.4 删除测试旁路 | 3-5天 | 3-5天 | 0 |
| **Phase 3 总计** | **15-22天** | **12-18天** | **3-4天** |

### 总体工作量对比

| Phase | 原估算 | 新估算 | 变化 |
|-------|--------|--------|------|
| Phase 1 | 5-8天 | 5-8天 | 无变化 |
| Phase 2 | 12-18天 | 12-18天 | 无变化 |
| Phase 3 | 15-22天 | 12-18天 | **-3~4天** |
| Phase 4 | 6-9天 | 6-9天 | 无变化 |
| **总计** | **38-57天** | **35-53天** | **-3~4天** |

**总工作量：** 从 8-12周 降低到 7-11周

---

## 风险变化

### Phase 3 风险降低

| 风险项 | 原风险 | 新风险 | 说明 |
|--------|--------|--------|------|
| 测试重写错误 | 🔴 高 | 🟡 中 | 简单替换，易验证 |
| 测试覆盖遗漏 | 🔴 高 | 🟢 低 | 保留原有测试结构 |
| 依赖注入复杂度 | 🟡 中 | 🟢 低 | 无需实现 fake_runner |
| 回归问题 | 🔴 高 | 🟡 中 | 测试逻辑不变 |

---

## 架构权衡

### 优点

✅ **工作量减少** - 节省3-4天  
✅ **风险降低** - 从高降到中  
✅ **测试影响小** - 简单替换  
✅ **语义清晰** - execution_mode 明确表达意图  
✅ **实用主义** - 统一现有机制而非推倒重来

### 缺点

❌ **架构纯度** - 生产代码仍识别"执行模式"  
❌ **测试与生产耦合** - execution_mode 横跨两者

### 权衡结论

对于本项目而言，**优点远大于缺点**：
- 50+测试文件的重写成本太高
- `execution_mode` 语义清晰，易于理解
- 统一机制比分散的判断更好维护
- 可以在未来需要时再重构为依赖注入

---

## 文档更新清单

- [x] `refactor-repair-manual.md` - 更新 2.1 节和 Phase 3.2
- [x] `refactor-repair-manual.md` - 更新删除检查部分
- [ ] `refactor-status-analysis.md` - 更新工作量和风险评估
- [ ] `refactor-supplemental-requirements.md` - 更新测试迁移方案
- [ ] `refactor-decision-points.md` - 记录此决策

---

## 实施建议

1. **Phase 3.2 优先级提高** - 因为工作量降低，可以提前完成
2. **全局替换脚本** - 准备自动化脚本替换测试中的 execution_mode
3. **渐进式弃用** - 保留 local_exec 一个过渡期，给出清晰的迁移提示
4. **文档同步** - 确保所有文档反映新方案

---

## 决策记录

**决策人：** 团队讨论  
**决策日期：** 2026-07-19  
**决策结果：** 采用 execution_mode 统一方案  
**理由：** 工作量大幅降低，风险可控，实用主义  
**反对意见：** 无  
**行动项：** 更新所有相关文档
