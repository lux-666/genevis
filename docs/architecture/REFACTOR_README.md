# 重构文档说明

本目录包含系统架构重构的完整文档。

## 主文档（当前目录）

**`refactor-repair-manual.md`** - 重构实施手册（主文档）
- 这是重构的**核心指导文档**
- 包含完整的实施计划、Phase定义、验收标准
- 实施重构时应首先阅读此文档

## 支持文档（./refactor/ 子目录）

以下文档提供额外的分析、决策依据和补充说明：

1. **`status-analysis.md`** - 可执行性分析报告
   - 验证手册的可执行性
   - 代码库现状分析
   - 风险评估和工作量估算
   - 用途：理解重构背景和决策依据

2. **`supplemental-requirements.md`** - 补充需求文档
   - Parameter 系统架构设计
   - FOFN 处理策略
   - 测试迁移方案
   - 向后兼容性策略
   - 用途：实施时查阅具体技术细节

3. **`decision-points.md`** - 关键决策讨论文档
   - 需要团队讨论的6个关键决策
   - 每个决策的选项分析和权衡
   - 决策记录模板
   - 用途：团队会议讨论和决策记录

## 阅读顺序建议

**首次了解重构：**
1. `refactor-repair-manual.md` - 了解整体方案
2. `refactor/status-analysis.md` - 了解现状和依据
3. `refactor/decision-points.md` - 参与决策讨论

**实施重构时：**
1. `refactor-repair-manual.md` - 主要参考
2. `refactor/supplemental-requirements.md` - 查阅具体实现细节
3. `refactor/decision-points.md` - 确认已做决策

**日常维护：**
- 主要参考 `refactor-repair-manual.md` 中的架构原则
- 支持文档作为历史记录保存
