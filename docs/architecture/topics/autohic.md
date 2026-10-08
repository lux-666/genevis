<a id="autohic-hi-c-scaffolding"></a>

# AutoHiC Hi-C 染色体挂载

> **状态：** 作为唯一 Hi-C scaffolding 方法已实现
>
> **阅读场景：** 追踪染色体级 scaffolding、AutoHiC 参数审批、执行、结果采纳或重试。
>
> **来源：** [运行时编排](runtime-orchestration.md)中的父阶段选择、[数据与存储](data-and-storage.md)中的当前资源、[bootstrap](bootstrap.md)描述的活动主机映射、schema-3 [参数推荐](parameter-recommendation.md)。
>
> **下游：** 已采纳的 `assembly` 资源、下游阶段、持久执行/评审证据、[评审与恢复](review-and-resume.md)。
>
> **依赖契约：** [阶段契约](../reference/stage-contracts.md)、[描述符](../reference/descriptors.md)、[参数推荐](../reference/parameter-recommendation.md)、[方法执行生命周期](../reference/execution-lifecycle.md)、[执行证据](../reference/execution-evidence.md)、[资源交接](../reference/resource-handoff.md)。
>
> **代码依据：** `app/stages/hic_scaffolding/builder.py`、`app/stages/hic_scaffolding/routing.py`、`app/stages/hic_scaffolding/nodes.py`、`app/stages/hic_scaffolding/parameters/workflow.py`、`app/stages/hic_scaffolding/method_application/autohic.py`、`app/stages/hic_scaffolding/method_application/support.py`、`tests/test_hic_scaffolding.py`。

<a id="stage-boundary"></a>

## 阶段边界

仅当冻结的有序计划中包含 `hic_scaffolding` 时，才存在运行时 occurrence；缺少它既不访问节点，也不创建旁路或 skip 事件。该 occurrence 激活后，编译阶段直接运行 AutoHiC。没有单项目方法选择门、阶段内 skip 分支或空操作 AutoHiC 结果。

稳定方法 ID `hic_scaffolding.candidate.autohic` 标识描述符、目录、推荐、尝试、产物和 Ledger 事件，不意味着当前可选择其他 Hi-C 方法。

阶段入口通过资源解析器解析当前采纳的组装，并以资产注册表身份解析配对 Hi-C 成员；同时要求已注册 AutoHiC 描述符和本地执行模式。缺失或无效的阶段要求产生阶段明确的 requirements/failure 结果，不改写成“未选择 Hi-C”。阶段游标为 `blocked_missing_requirements`，但顶层监督会终止运行，不开启可恢复人工评审阻塞。

<a id="implemented-flow"></a>

## 已实现流程

```text
父图选择 hic_scaffolding
  -> 解析已采纳 contig 组装 + 配对 Hi-C 读段
  -> 构建 schema-3 AutoHiC 推荐
  -> 阻塞等待参数审批
  -> 冻结精确 AutoHiC 配置、脚本、依赖和输出
  -> 共享本地运行并持久化执行证据
  -> 收集染色体 FASTA、contact map、assembly 文件和 HTML 报告
  -> 阻塞等待独立采纳评审
  -> 采纳候选、保留旧组装、请求新重试或停止
```

参数审批和染色体组装采纳是两个不同的人工决策。批准稀疏映射只授权一次可执行尝试，不采纳该尝试的生物学输出；最终门禁读取已发布证据，决定下游应使用哪个组装。

<a id="adapter-and-execution-boundary"></a>

## 适配器与执行边界

AutoHiC 适配器只加载已验证的已批准 schema-3 参数链。它解析执行器值、规范已提供事实和已批准模型覆盖，不合并前一次重试映射，然后为共享生命周期物化冻结参数证据、AutoHiC 配置、保留脚本、命令记录、精确输出路径和类型化依赖。

机器级 AutoHiC、Juicer、3D-DNA、Python 和模型位置来自活动主机映射，可人工维护或通过 [bootstrap](bootstrap.md)维护，并在运行中冻结。组装、Hi-C 读对和提供的限制性内切酶是运行级事实。边界无法形成完整可执行尝试时，适配器拒绝冻结。

执行使用[方法执行](execution.md)描述的生产本地子进程 Runner。生成的 wrapper 调用配置的 AutoHiC 入口；Gene Agent 不在图中重写 Juicer、3D-DNA、错误检测或染色体分配。不存在 AutoHiC 远程/调度器 Runner。

就绪与产物校验是独立边界。冻结验证声明的可执行文件、模型、输入材料和输出路径，但不要求进程运行前输出已存在。进程报告成功后，收集会在首次发布前校验每个冻结 `expected_outputs` 成员。AutoHiC 要求染色体 FASTA、`.hic`、`.assembly` 和 HTML 报告。每项必须是文件，除非不可变索引已有可在崩溃回放中恢复的同一尝试本地产物。缺失或非文件且无该记录时，产生终态 `failure_kind: collect`，不打开评审候选或采纳门，也不发布四项评审产品、评审包或 QC 报告。

正常 wrapper 成功后，收集发布染色体级 FASTA、`.hic`、`.assembly` 和 HTML 评审产物，以及紧凑评审/QC 包和规范执行引用。共享 Runner 已拥有 stdout、stderr 与进程证据；收集不重复登记日志。发布有意不具事务性：若后续发布在早先产品移动并索引后失败，早先产品保留为已索引孤儿；收集不记录 `attempt_succeeded` 或 `artifact_registered` 语义事件，不产生评审候选或门，也不重试或回滚。

<a id="adoption-and-retries"></a>

## 采纳与重试

接受候选会验证已发布 FASTA，并将 `resource_refs["assembly"]` 替换为带来源的 Hi-C 组装。保留旧组装会保留其字节与候选关系，但在当前 Hi-C occurrence 下重新发布中央资源。请求参数重试会封存当前尝试，带不可变执行和评审证据返回推荐。

执行失败重试由明确操作者策略和有界预算控制；评审触发的重试有自己的预算。两者都创建新的 schema-3 推荐和尝试专属工作区，不修改旧映射或覆盖其产物。

<a id="current-limits"></a>

## 当前限制

- AutoHiC 是唯一注册的 Hi-C 方法，其他 scaffolder 没有实现选择路径。
- QC 包明确记录未应用自动 QC 阈值。最终生物学采纳是人工决策，尚未自动采纳。
- 生产执行在 CLI 主机本地；调度器提交、进程重新附着和远程编排尚未实现。
- 需求失败、参数拒绝、重试耗尽和执行失败是不同结果，没有任何隐藏 skip 分支。
