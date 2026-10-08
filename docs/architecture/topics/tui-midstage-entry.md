<a id="tui-midstage-entry"></a>

# TUI 中途阶段入口

> **状态：** 已规划；当前 TUI 尚不可用。
>
> **阅读场景：** 实现或讨论从 clean 长读段或已有组装启动 TUI 运行。
>
> **来源：** 当前 [TUI 与规划](tui-and-planning.md)边界和后端对已注册 manifest 资产的支持。
>
> **下游：** 整次运行冻结、[运行时编排](runtime-orchestration.md)、Assembly 或 Assembly Review。
>
> **依赖契约：** [阶段契约](../reference/stage-contracts.md)、[资源交接](../reference/resource-handoff.md)、[运行数据](../reference/data-contract.md)。
>
> **代码依据：** `app/schemas/tui_intake.py` 和 `app/entrypoints/tui_intake.py` 中的现有缺口；`app/schemas/manifest.py`、`app/stages/inventory/` 及已有组装集成测试中的后端能力。

<a id="accepted-entry-modes"></a>

## 已接受的入口模式

| 入口模式 | TUI 事实 | Manifest 资产 | 推荐的首个分析阶段 |
|---|---|---|---|
| `raw_reads` | 现有 HiFi/ONT 输入采集 | `raw_long_reads` | 现有行为 |
| `clean_long_reads` | 一个或多个 clean 长读段文件，及明确的 `ont` 或 `pacbio_hifi` 平台 | `clean_long_reads` | `assembly` |
| `existing_assembly` | 恰好一个可读的组装 FASTA | `assembly_fasta_registered` | `assembly_review` |

后端已理解这两类资产，并能通过 Inventory 路由已有组装。当前 TUI 未暴露 `entry_mode`、clean 读段事实或已有组装事实。因此本页描述已接受的目标，不代表功能今天已经可用。

<a id="intended-intake-boundary"></a>

## 目标输入边界

明确表述才选择中途阶段模式；泛泛的“做组装”不会触发。两类意图同时出现时，已有组装优先于 clean 读段。选定模式改变确定性需求队列：

- 原始模式保留现有 HiFi/ONT/Hi-C 问题。
- clean 模式仅询问 clean 读段、平台和必要规划事实，例如操作者估计的基因组大小。
- 已有组装模式仅询问一个组装 FASTA。

路径始终是用户明确提供的文件。TUI 不扫描目录、不按文件名猜 ONT/HiFi、不从多个组装中代选，也不创建任意阶段导入器。非法、不可读、空、歧义或多个组装输入应保持未解决。

<a id="planning-and-freeze"></a>

## 规划与冻结

已接受模式和确认资产持久化到可变草稿，通过现有 manifest 模型物化。规划器通常为 clean 读段推荐 Assembly，为已有组装推荐 Assembly Review；推荐不是入口限制。操作者可编辑有序 occurrence 计划，全局注册表顺序不得擅自补回、拒绝或删除阶段。执行到各阶段时，仍由阶段自己的契约验证逻辑资源。

文件编写的公共 `gene-agent run` 入口按原样冻结 occurrence 列表，不施加已有组装专属顺序。冻结从注册表默认值导出固定阶段方法名单，并写入规范冻结工作流步骤。Assembly occurrence 可省略方法名单，让阶段依据上游证据推荐并确认 `assembly_selection`；省略表示延迟绑定，不是空方法列表。Inventory 注册传入的组装资源；列表中未出现的阶段没有 occurrence；到达的阶段自行验证输入或报告输入失败。

最终确认沿用所有 TUI 运行的整次运行冻结。此功能不得引入其他执行器、未经校验的路径覆盖、第二套 manifest 格式，或绕过 Inventory 及产物/资源模型。

<a id="completion-boundary"></a>

## 完成标准

类型化草稿、确定性与 LLM 意图解析、需求队列、manifest 投影、计划生成、恢复和修订行为、用户文档、针对性测试全部一致，才算实现完成。在此之前，现行操作者文档必须继续明确：对话 TUI 尚不支持这两种入口。
