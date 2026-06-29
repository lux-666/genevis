# Phase 9: End-to-End Pipeline Integration Test

## 目标

验证完整流程的状态流转、工具执行、TUI耦合和断点机制。

## 交付物

### 1. 阶段流转和断点分析

**非阻塞阶段**（自动执行）：
- `read_qc`: seqkit stats → length filter → FOFN → commit
- `genome_survey`: jellyfish → genomescope2 → commit

**条件阻塞阶段**：
- `assembly`: 
  - Gate 1: route_selection（LLM置信度<0.7或手动策略）
  - Gate 2: candidate_selection（多候选无自动决策）
- `hic_scaffolding`: execution_mode != "local"时软阻塞
- `t2t`: review gate（重大结构变化需审核）

**强制阻塞阶段**：
- `assembly_review`: 总是触发human gate（除非auto策略）

### 2. TUI耦合机制

所有阻塞点遵循统一模式：
```python
# Stage设置阻塞
status = "blocked_human"
active_interrupt = {gate_id, decisions_allowed}
supervision_state = "blocked_human"
→ 路由到 "block" → END

# TUI响应
检测supervision_state → 解析active_interrupt → 渲染UI → 
捕获决策 → resume_review_state() → 恢复执行
```

**结论**：无冗余断点，所有阻塞点都是基因组学流程的自然决策点。

### 3. 工具执行模式

统一的 **freeze → run → collect** 模式：
- freeze: 生成执行脚本
- run: 执行工具（本地或HPC）
- collect: 解析输出，发布artifacts

### 4. 端到端测试

**测试脚本**: `tests/hpc/test_09_e2e_pipeline.sh`

**验证内容**：
- ✅ 阶段顺序正确性（通过ledger验证）
- ✅ 阻塞行为检测（supervision_state和active_interrupt）
- ✅ 干净退出验证（阻塞时不崩溃）
- ✅ 工具执行验证（脚本生成和执行）
- ✅ 资产传递验证（跨阶段asset_registry传播）
- ✅ Checkpoint持久化（可恢复性）
- ✅ TUI合约验证（gate结构完整性）

**运行方式**：
```bash
# 单独运行
cd tests/hpc
./test_09_e2e_pipeline.sh

# 或在完整测试套件中运行
./run_all.sh 9
```

## 断点总结

| 阶段 | 断点 | 触发条件 | Gate ID | TUI选项 |
|-----|------|---------|---------|---------|
| assembly | route_selection | 置信度<0.7 或 手动策略 | route_selection | select_ont/hifi/cyclone |
| assembly | candidate_selection | 多候选无自动决策 | candidate_selection | 候选对比+选择 |
| assembly_review | human_gate | 总是（除非auto） | assembly_review | 选择候选/拒绝/重组装 |
| hic_scaffolding | execution_gate | execution_mode != local | - | 提交HPC作业/跳过 |
| t2t | t2t_review | 重大结构变化 | t2t_review | 接受/拒绝patch |

## 关键实现细节

### 状态字段

```python
supervision_state:
  - "idle"            # 父图路由中
  - "running_stage"   # 阶段执行中
  - "blocked_human"   # 等待人工决策
  - "failed_terminal" # 不可恢复失败
  - "completed"       # 所有阶段完成

active_interrupt: {
  "stage": "assembly",
  "gate_id": "route_selection",
  "decisions_allowed": ["select_ont", "select_hifi", "select_cyclone"]
}
```

### Resume流程

```python
# TUI调用
resume_review_state(session_id, stage, gate_id, decision)

# Gate handler处理
1. 应用决策到stage_outputs
2. 清除active_interrupt
3. 设置supervision_state = "running_stage"
4. 更新checkpoint

# 流程恢复
parent graph → route_controller → 检测supervision_state → 
路由到current_stage → 从checkpoint恢复
```

## 快速参考

查看阻塞模式参考卡片：
```bash
bash docs/STAGE_BLOCKING_REFERENCE.sh
```

## 后续工作

1. 运行test_09验证端到端流程
2. TUI集成：实现gate渲染和resume逻辑
3. 添加用户决策指南文档
4. 实现gate等待时长监控
