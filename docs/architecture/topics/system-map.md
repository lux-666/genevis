<a id="system-map"></a>

# 系统全景

> **状态：** 已实现；规划和欠账节点均有明确链接。
>
> **阅读场景：** 初次了解架构，或判断一项改动应由哪个子系统负责。
>
> **输入：** 操作者意图、已注册输入、描述符和机器配置。
>
> **下游：** [TUI 与规划](tui-and-planning.md)、[运行时编排](runtime-orchestration.md)、[方法执行](execution.md)和[数据与存储](data-and-storage.md)。
>
> **依赖契约：** [阶段契约](../reference/stage-contracts.md)、[运行数据](../reference/data-contract.md)和[架构治理](../reference/governance.md)。
>
> **代码依据：** `app/entrypoints/run.py`、`app/workflow.py`、`app/graph/builder.py`、`app/control/occurrence_settlement.py`、`app/schemas/state.py`、`app/execution/lifecycle.py`、`app/storage/paths.py`。

<a id="what-the-system-is"></a>

## 系统定位

Gene Agent 是可持久恢复的基因组工作流协调器。两个公共启动入口汇入同一套嵌套生命周期：

```text
结构化 manifest + run config --\
                              -> 整次运行输入冻结
可选的可变 TUI 草稿 -----------/
  -> 父图路由到一个编译后的阶段子图
  -> 阶段选择并准备方法
  -> 冻结、执行并收集一次方法尝试
  -> 发布证据
  -> 阶段返回明确的 occurrence 结果及交付声明（如有）
  -> 共享结算边界持久化交付与终态事实
  -> Review 决定前进、重试、阻塞或终止
```

父 LangGraph 保持精简，但仍有副作用：控制器按紧凑状态路由，记录 `stage_entered`，并调用终态恢复也使用的 occurrence 结算边界。执行器拥有的权威终结器记录运行级 `session_terminal` 并投影终态报告。生物学方法选择和输出解释留在阶段子图内；进程调用留在共享执行内核内。

<a id="three-graph-scales"></a>

## 三个图层级

| 层级 | 所有者 | 职责 |
|---|---|---|
| 父图 | `app/graph/`、`app/workflow.py` | 分发当前冻结 occurrence，为其明确结果调用共享结算，并约束终态边界 |
| 阶段子图 | `app/stages/<stage>/` | 解析输入、选择和准备方法、收集输出、评审，并在适用时提交逻辑资源 |
| 方法生命周期 | `app/execution/` 与阶段适配器 | 持久化一次精确尝试，每次状态转换至多运行一个待执行进程，发布证据后返回阶段收集逻辑 |

阶段共享边界术语，不强制共享内部拓扑。只注册资产的阶段，不应被描述成具有参数化 AutoHiC 尝试的全部节点。

<a id="module-ownership"></a>

## 模块职责

| 模块 | 所有职责 |
|---|---|
| `app/entrypoints/` | 公共结构化 `run` CLI、可选可变 TUI/规划界面、共享整次运行冻结交接、租约范围内的 inspect/review/resume 入口、尽力提供的终态展示 |
| `app/workflow.py`、`app/graph/` | 阶段注册与默认规划、冻结 occurrence 路由、checkpointer 构造、控制器调用生命周期边界 |
| `app/domains/` | 不依赖阶段的生物学规则、无工作流副作用的共享类型化输入解释 |
| `app/stages/` | 编译后的生物学子图、方法路由、命令准备、结果收集、评审、资源提交 |
| `app/llm/` | 严格的模型 DTO 渲染、调用、响应结构解析；不拥有审批、路由或持久化 |
| `app/schemas/` | 跨模块的状态、计划、描述符、产物、执行和 manifest 契约 |
| `app/execution/` | 尝试冻结/执行/收集内核、Runner 边界、环境选择和持久化进程证据 |
| `app/storage/` | 运行目录、不可变发布与索引、Ledger、冻结输入、持久证据记录 |
| `app/review/` | 门禁声明、允许的决策、occurrence 校验、超时请求规范化、不可变请求/意图/应用记录，以及注册的类型化 prepare/apply 分发 |
| `app/control/` | 共享 occurrence 结算、进度与 review 投影、租约校验后的恢复事务协调、inspect、权威终结、生命周期恢复 |
| `app/reports/` | 尽力生成操作者状态与会话轨迹投影，不拥有控制或恢复权威 |

模块通过类型契约和引用连接。目录顺序不表示流水线；任何模块都不得私设一套机制替代上述责任所有者。

<a id="durable-fact-owners"></a>

## 持久事实的所有者

checkpoint 是恢复游标，不是工作流数据库。当前逻辑文件通过 `resource_refs` 传递；原始输入身份属于资产注册表；不可变正文与输出文件属于产物；产物索引证明身份和字节；Ledger 记录因果事件及 occurrence/method/attempt 与规范会话终态单例。组装交付历史是绑定 occurrence 的 Ledger 事实，不是可变资源标签。共享 review 状态投影决定当前能否提交决策，或必须先恢复已有意图。报告只是可选的操作者视图。新增事实的归属见[数据与存储](data-and-storage.md)。

<a id="current-product-boundary"></a>

## 当前产品边界

阶段注册表包括 Inventory、Read QC、Genome Survey、Assembly、Assembly Dedup、Assembly Review、Hi-C Scaffolding、GapCloser、T2T Finishing 和 Base Polish。操作者计划编译为不可变 occurrence；注册表顺序只指导注册和展示，不决定运行时后继。GapCloser 支持仅 HiFi 的 TGS-GapCloser，并由人工决定采纳；Base Polish 执行一轮 HiFi Racon，产出成对证据并由人工采纳。精确的 occurrence、Review 绑定及输入支持规则见[运行时编排](runtime-orchestration.md)和[阶段契约](../reference/stage-contracts.md)。

真实运行通过 `gene-agent run --manifest PATH --run-config PATH --data-dir PATH` 启动；`gene-agent tui` 是可选对话入口，两者使用同一冻结会话执行器。文件所有权、TTY、RunConfig 和冻结语义由运行数据与运行时文档定义。其他公共命令用于检查、审计、评审、恢复或 bootstrap。生产方法以 CLI 所在主机的本地子进程执行；服务器运行意味着在服务器检出目录启动同一 CLI，不存在代理内 SSH 或调度器 Runner。

<a id="navigate-by-edge"></a>

## 按连接关系导航

- 用户请求、计划编辑、整次运行冻结：[TUI 与规划](tui-and-planning.md)。
- 父图路由、阶段进入、终态行为：[运行时编排](runtime-orchestration.md)。
- 冻结命令与进程证据：[方法执行](execution.md)。
- 人工阻塞与持久恢复：[评审与恢复](review-and-resume.md)。
- 精确契约：沿主题页明确列出的参考链接阅读。
- 已接受但尚不可用的 TUI 输入：[TUI 中途阶段入口](tui-midstage-entry.md)。
- 已知差距：[架构一致性](../reference/conformance.md)。
