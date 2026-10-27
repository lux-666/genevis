<a id="runtime-orchestration"></a>

# 运行时编排

> **状态：** 已接受；剩余跨切面缺口见[架构一致性](../reference/conformance.md)。
>
> **阅读场景：** 追踪 CLI 分发、冻结会话启动、父图路由、阶段选择或终态行为。
>
> **来源：** 结构化公共运行或[TUI 与规划](tui-and-planning.md)，以及已校验的整次运行冻结契约。
>
> **下游：** 编译阶段子图、[方法执行](execution.md)、[评审与恢复](review-and-resume.md)、终态报告。
>
> **依赖契约：** [阶段契约](../reference/stage-contracts.md)、[运行数据](../reference/data-contract.md)、[资源交接](../reference/resource-handoff.md)。
>
> **代码依据：** `app/entrypoints/run.py`、`app/workflow.py`、`app/graph/builder.py`、`app/graph/routing.py`、`app/control/occurrence_settlement.py`、`app/control/inspection.py`、`app/control/terminal.py`、`app/control/resume.py`、`app/graph/checkpoint_compaction.py`。

冻结依赖快照的 `runtime` 记录源码版本、已安装包版本、可用时的 Git 提交及工作区是否有修改。版本来自 `app/version.py`，构建元数据复用同一来源；安装包没有 checkout 时 Git 字段为空，不借用父目录仓库。恢复读取原快照，不重算运行来源。该来源记录用于审计，不承诺代码升级后的 checkpoint 迁移。

<a id="public-entry-boundary"></a>

## 公共入口边界

安装后的 `gene-agent` 命令分发到 `app.entrypoints.run:main`。真实/操作者新运行严格使用：

```text
gene-agent run --manifest PATH --run-config PATH --data-dir PATH
```

三个路径均必填。manifest 单独拥有 `session_id`；CLI 不暴露 session、checkpointer、本地执行、launch 或 ask 覆盖参数。`data-dir` 是布局根目录，运行位于 `<data-dir>/runs/<session_id>/`。处理器校验并规范化两个源文档，冻结字面 occurrence 计划及其依赖闭包，再进入共享执行器。固定阶段方法名单来自注册表默认值；Assembly 可省略名单，以便在上游 QC/Survey 证据具备后推荐并确认方法。精确请求/响应/推荐恢复链只在[运行数据契约](../reference/data-contract.md#101-assembly-method-recommendation)定义。确切 RunConfig 字段、occurrence 身份、授权、空计划、发布顺序和不可变 workflow-step 规则属于[运行数据契约](../reference/data-contract.md)，不在本主题重复维护。

已接受的冻结前边界要求：创建或发布冻结事务前，manifest 校验必须证明每个声明的生物学成员都是非空逻辑内容的常规文件。支持的压缩成员必须能解出至少一个字节，损坏压缩必须失败；仅容器字节大小不足以满足条件。精确输入不变量由[运行数据契约](../reference/data-contract.md#49-declared-input-content-is-logically-non-empty)拥有。

RunConfig 拥有字面 occurrence 序列。规划推荐永远不会变成校验规则，也不会静默插入或重排 Review。可复用 Review 的精确位置和生产者绑定契约见[阶段契约](../reference/stage-contracts.md)。

`gene-agent tui` 是使用同一冻结发布和执行器的可选对话输入/规划入口。其他公共子命令为 `inspect`、`storage-audit`、`resume`、`review` 和 `bootstrap`。

`execute_frozen_session` 校验冻结控制文档，取得每会话执行锁和活动 `FrozenExecutionLease`，创建 Ledger 会话，构建小型 `TopLevelState`，把运行配置值投影到所属阶段游标，使用所得执行器拥有的 `FrozenInputBundle` 构建父图，并以 session ID 作为 LangGraph thread ID 调用。Inventory 校验和资产注册消费已经验证的内存 manifest、RunConfig、规范声明和运行所属路径，不重新打开未经校验的控制文件。该 bundle 是闭包上下文，不是图通道或 checkpoint 载荷。新进程在建图前再次校验冻结事务；这是数据契约定义的可信本地操作者模型下的运行一致性检查。公共 run 始终使用 SQLite。结构化新运行全部阶段等待为 0 时允许无 TTY；存在非零等待时必须连接真实 stdin/stdout TTY，否则失败且不发布冻结输入。TUI 是交互入口，确认草稿后仍要求 TTY；冻结后共用同一执行器。已冻结的阻塞 checkpoint 通过 `inspect`、`review`、`resume` 人工恢复，不重启自动循环。

前台门禁使用冻结的评审策略和注册 resolver，而原执行器保持附着。进程中断留下持久人工阻塞，不使用守护进程截止时间；后续 `review` 和 `resume` 不重建已经过去的倒计时。精确值、resolver 资格、不可变绑定和恢复状态由[评审契约](../reference/data-contract.md#12-review-contract)拥有，并在[评审与恢复](review-and-resume.md)中说明。

<a id="unified-interactive-execution"></a>

## 统一交互执行

已接受的新运行边界有两个不同的冻结前生产者，但冻结后只有一条执行路径：

```text
结构化运行文件 -> 校验 -> freeze --+
                                   +-> execute_frozen_session
TUI 输入 -> 计划草稿 -> 确认 -> freeze-+
```

`gene-agent run` 将 manifest 与 RunConfig 视为已经定稿的草稿，不进入 TUI 计划草稿编辑器，也不要求二次确认。无效文件在冻结前失败；有效文件冻结后直接执行。TUI 仍是反复收集、检查、描述、编辑和确认可变草稿的入口，最终创建同一冻结契约。

全零等待的结构化新运行允许无 TTY，其余新运行要求 stdin 和 stdout 在冻结前连接真实 TTY。两者使用同一执行器和持久门禁协议，具体动作由[等待模式矩阵](review-and-resume.md#foreground-review-policy)拥有。`review` 和 `resume` 是持久会话中断后的恢复工具，不是第二种正常生命周期。

多个 contig 或项目仍是多个独立运行。外部调用者为每个单元创建一份 manifest 和 RunConfig，分配无冲突 `session_id`，按等待配置为需要交互的进程分配终端，并重复调用同一命令。产品不增加批处理调度器、跨会话 checkpoint 或持久聚合进度所有者；调用者可在运行目录外合并只读检查结果。

冻结后两个生产者都调用 `execute_frozen_session` 边界。该边界拥有阶段进度、评审摘要、回车确认倒计时、人工决策、自动超时决策和终态摘要的一套终端体验。入口点不得实现并行门禁或进度循环。

阶段配置投影由 `app/control/stage_seeds.py` 负责。`app/control/session.py` 负责图调用、阻塞 checkpoint 识别及调用异常后的终态裁决，返回结构化结果，不打印终端输出。`control/terminal.py` 证明和持久化终态；CLI 只把调用结果映射为提示和退出码。异常发生后以持久 checkpoint 和 Ledger 为准，不能用入口层内存状态覆盖已完成事实。

附着和分离进度调用[运行数据契约](../reference/data-contract.md#410-one-workflow-progress-reducer)定义的同一 reducer。Checkpoint、标记和锁仍是执行诊断，不是第二进度权威。

<a id="parent-graph"></a>

## 父图

编译后的父图形状固定：

```text
START -> route_controller -> selected stage subgraph
                            -> route_controller -> ... -> END
```

进图前，操作者计划投影为控制器消费的不可变 occurrence 记录。精确 ID、游标规则、Review 目标和结算身份由运行数据与阶段契约拥有。

`app.workflow.STAGE_ORDER` 是注册、默认规划和展示辅助，不是运行时顺序或后继表。规范完整基因组 occurrence 推荐由[阶段契约](../reference/stage-contracts.md)拥有，并由 `app.workflow.recommended_workflow_template` 生成；运行时绝不把该助手当作后继表。

推荐是可编辑规划指导，不是 RunConfig 要求。控制器不读取 FASTQ 内容、不选择组装器、不构建命令、不解释生物学指标。Assembly 选择属于 Assembly 阶段，在上游证据之后进行，不属于父控制器。可复用 Review 跟随已证明的组装交付来源；精确位置示例与未来生产者规则留在阶段契约。

控制器包装很薄，但并非纯函数。每条完成阶段路径都为当前 occurrence 返回明确 `workflow_step_outcome`。`blocked_human` 路径不返回 occurrence 结果，保持该 occurrence 激活。父图调用共享 occurrence 结算边界，按冻结计划校验交接；两条路径都不能从游标移动、阶段状态或路由标记推导结果。完成时先记录不可变组装交付，再准确记录一个 `stage_completed`；定义的 skip 或失败则准确记录一个 `stage_skipped` 或 `stage_failed`。终态恢复在写会话终态前调用同一边界。父图还拥有 `stage_entered`，并在结算后调用共享进度 reducer；不在阻塞或失败边界写 `session_terminal` 或生成操作者报告。阶段内 skip 与缺少 occurrence 不同：父图永不为省略阶段合成 skip 事件。任何声称父图无副作用的架构描述都是错误的。

<a id="stage-boundary"></a>

## 阶段边界

父图中的每个域节点都是编译阶段子图。阶段可以解析逻辑资源、规划已注册方法、进入共享方法生命周期、发布生物学产物、打开评审门并提交新资源。这些是可用边界，不是十个阶段必须完全相同的序列。

`app/graph/builder.py` 显式注册十个域阶段；子图可组合，但不存在动态插件注册表。`gapcloser` 执行仅 HiFi 的 TGS-GapCloser 路线；`base_polish` 执行仅 HiFi 的 Racon 轮次并保留成对前后证据。结果采用/保留共用持久门禁，人工或自动动作遵循[等待模式矩阵](review-and-resume.md#foreground-review-policy)。恢复只应用于绑定到被中断不可变 occurrence 的决策，不重写剩余计划、固定方法名单或已绑定 Assembly 选择。

`stage_seeds` 是不可变的阶段范围 RunConfig 投影。每次重复 occurrence 都从同一阶段种子的全新副本及其独立复制的固定方法名单开始；Assembly 种子可不带名单，让 occurrence 在阶段入口依据冻结依赖绑定方法。它绝不复用前一个 occurrence 的完成游标。

manifest 声明 `assembly_fasta_registered` 时，Inventory 将其注册为中央组装资源。`assembly_review` 是推荐的首个分析 occurrence，但公共 `gene-agent run` 不按 `STAGE_ORDER` 拒绝、重排或修改冻结 occurrence 序列。每个到达的 occurrence 自行验证阶段契约所需资源；省略阶段没有 occurrence，也没有合成 skip。

<a id="stop-and-terminal-behavior"></a>

## 停止与终态行为

`blocked_human` 以指向不可变请求的紧凑中断停止图推进。`failed_terminal` 停止运行并记录有界错误证据。成功路径到达 `done` 并完成终态。

只有完成或失败的会话才可进行权威终态收尾。证明终态 checkpoint 后，权威终结器写入或复用唯一规范 `session_terminal` Ledger 单例。失败终态只有在证明连续已结算前缀以一个匹配失败 occurrence 结束且无更晚终态后，才记录 `failure_scope=occurrence`；若前一 occurrence 已完整结算且下一 occurrence 从未进入，则记录 `failure_scope=controller_boundary`，不会为后者伪造 `stage_failed`。随后终结器尝试 checkpoint 压缩。压缩保留最新终态 checkpoint，不删除不可变产物；压缩失败只是运维警告，不是另一种生命周期结果。`ProjectionBoundary` 尽力生成报告和工作流状态投影：普通投影异常成为有界警告，不污染生命周期、评审状态、checkpoint 或终态权威。报告没有恢复权威，也不要求全部可独立重建。
