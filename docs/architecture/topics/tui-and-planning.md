<a id="tui-and-planning"></a>

# TUI 与规划

> **状态：** 已实现原始 HiFi/ONT、子代 Illumina 短读段 paired reads、Trio 母本/父本读段、配对 Hi-C、clean 长读段及已有组装入口。
>
> **阅读场景：** 修改对话输入采集、计划草稿、整次运行冻结或草稿恢复。
>
> **输入：** 操作者目标文本、确认的样本事实、描述符导出的方法目录，以及人工维护或由可选 [bootstrap 助手](bootstrap.md)维护的机器配置。
>
> **权威：** 冻结 RunConfig 拥有阶段 occurrence 和本地执行授权；上游证据具备后，Assembly 阶段拥有方法身份和参数。输入文本仅是路由提示，不是执行权威。
>
> **下游：** 不可变冻结输入与[运行时编排](runtime-orchestration.md)。
>
> **依赖契约：** [运行数据](../reference/data-contract.md)、[描述符](../reference/descriptors.md)、[阶段契约](../reference/stage-contracts.md)。
>
> **代码依据：** `app/entrypoints/tui.py`、`app/entrypoints/tui_intake.py`、`app/entrypoints/ask_planner.py`、`app/entrypoints/planner_catalog.py`、`app/schemas/tui_intake.py`、`app/storage/frozen_inputs.py`。

<a id="mutable-draft-boundary"></a>

## 可变草稿边界

`gene-agent tui` 是可选的对话式新运行入口。确认前，它拥有带版本的可变草稿，生命周期如下：

```text
intake -> planning -> ready_to_confirm -> frozen
                                      -> running
                                      -> blocked_review | terminal
```

草稿不是图 checkpoint，也不是可执行契约。未冻结时可以修改或恢复。正常对话中唯一的顶层 TUI 命令是 `/resume`，通过对应恢复路径选择已有草稿或阻塞会话。

已有准备好的文件时，真实运行使用确定性入口 `gene-agent run --manifest PATH --run-config PATH --data-dir PATH`。它将文件视为已定稿草稿，汇入同一整次运行冻结边界和执行器。公共 RunConfig、occurrence、授权和评审策略字段见[运行数据契约](../reference/data-contract.md)。未完成的计划草稿在阶段选择前仍可使用 `None`，这表示草稿未完成，不构成第二种可执行运行时含义。

当前输入模型直接支持 PacBio HiFi、ONT、子代 Illumina 短读段 paired reads、Trio 母本/父本读段、明确配对的 Hi-C lane、明确无 Hi-C 的事实、物种事实及提供时的端粒基序。短读段必须由操作者分别显式声明 R1 与 R2；TUI 不根据文件名或目录顺序推断配对。短读段事实物化为 `raw_short_dna_reads` 的 `layout: paired` 输入，并可驱动独立 `read_qc`（fastp）及可选 `genome_survey`，但不会单独虚造 Assembly 路线。Trio 父母读段虽使用短 DNA 输入类型承载，仍以独立的母本/父本角色绑定，不作为子代短读段 QC 输入。HiFi 与子代短读段可以在同一 Assembly 采集流程中同时确认；Hi-C 清洗则依据当前意图和已确认的配对 Hi-C 输入绑定。显式中途入口采集带平台的 clean 长读段或一个已有组装 FASTA；见[中途阶段入口](tui-midstage-entry.md)。CLR 和 CycloneSEQ 仍通过文件 manifest 声明。

<a id="intent-is-not-the-execution-plan"></a>

## 意图与执行计划的边界

意图解析器决定 TUI 还需要询问哪些事实，不授权工具，也不拥有最终阶段集合。采集完成后，规划器接收已确认 manifest 视图与描述符注册表导出的方法目录，生成可编辑的阶段与注意事项草稿。固定阶段方法取自注册表内部默认值，不是用户方法字段。Assembly 延迟绑定：阶段先依据上游 QC/Survey 证据推荐并确认 `assembly_selection`，再推荐已绑定方法的参数。

输入解析可识别组装工具名，但仅作为临时资产角色提示，用于判断需要收集 HiFi、ONT 还是两者。它不是持久化 `WorkflowIntent` 字段，不得创建第二执行路径、预选 `assembly_selection` 或向计划草稿模型传递方法名。包含方法字段的持久化输入文档必须在 schema 校验时失败。

Assembly Review 是推荐，不是完成条件。默认 Assembly 推荐可以追加 `assembly_dedup -> assembly_review`。规范的完整基因组/T2T occurrence 序列由[阶段契约](../reference/stage-contracts.md)拥有，并由 `app.workflow.recommended_workflow_template` 提供给规划器；本页不维护第二份字面数组。

最终 Review 是同一可复用阶段的第二次 occurrence。冻结将它绑定到最近的组装交接生产者 Base Polish；执行过 Base Polish 后，即使保留源组装，也评估源组装和所选抛光候选这对输入；若顾问在执行前保留源，则只评估源，验收报告标记 `not_executed_source_retained`，不计为 Racon 执行覆盖。操作者可在 occurrence 列表中删除、移动或重复 Review 及其他阶段。含 T2T 且之后没有 Review 的意图或已确认 RunConfig 仍合法；推荐不能变为校验策略。

`selected_stage_occurrences` 是唯一可编辑草稿序列。重复阶段仍是独立选择，各自具有本地进程授权。方法身份和参数不是用户编写或交互采集的草稿字段。TUI 仅编辑 occurrence 顺序、本地执行授权和确认。确认通过结构化运行共用的规范 occurrence/冻结契约编译草稿；缺失必需事实时在边界拒绝，不转为参数询问。需要上游证据的运行时推荐继续延迟。规划模型失败或返回非法草稿时停止本次规划，持久草稿仍可恢复；TUI 不自行编造确定性备用阶段计划。

草稿区分三种选择状态：

```text
None  -> 尚未回答阶段序列；询问阶段选择
[]    -> 明确仅运行 Inventory；不询问授权、方法或参数
[...] -> 精确有序的生物学 occurrence；继续正常规划
```

纯文本与 questionary 阶段提示都将空推荐显示为 `Inventory only`，回车即接受。LLM 计划草稿载荷要求 JSON 列表，但不要求非空。接受空列表后清除过期方法和参数选择，最终摘要显示 `Inventory only`，冻结为 `requested_stages: []`。

<a id="whole-run-freeze"></a>

## 整次运行冻结

最终确认调用公共结构化 `run` 命令使用的同一事务式发布边界：

```text
已确认草稿 -> 已验证冻结事务 -> execute_frozen_session
```

精确冻结文件、发布顺序、哈希、幂等性和崩溃行为见[运行数据契约](../reference/data-contract.md)。部分完成或无效的冻结必须拒绝，不能静默重建输入。

最终确认并通过 TTY 检查后，TUI 为固定阶段计算唯一规范的有效本地方法闭包，并捕获延迟 Assembly 所需的完整已配置候选范围。只读就绪检查探测固定闭包及必需 LLM 端点；Assembly 选定方法后再做该方法预检。发布所需检查通过后才保存依赖快照；就绪失败不创建会话。Bootstrap 仍是覆盖所有已配置方法的机器级诊断。冻结后执行器从快照解析依赖，不读取后来修改的 `config/envs.yaml`。

整次运行冻结不生成所有未来方法命令。规划期共享事实采集由声明的延迟 Assembly 范围推导：本地 Assembly 在可获得时需要规范化 `read_type`，并且必须有 manifest 基因组大小先验或位于其前方的本地 Genome Survey；否则规划和冻结校验拒绝。各阶段之后再创建[方法执行](execution.md)所述更窄的方法尝试冻结。

可变草稿也携带[推荐策略契约](../reference/recommendation-policy.md)定义的两个独立推荐模式。它们是规划选择，不是方法选择；在同一整次运行冻结边界变为不可变。

<a id="post-freeze-convergence"></a>

## 冻结后汇合

可变 TUI 生命周期止于整次运行冻结。操作者确认计划草稿后，TUI 不得保留专属执行器、评审循环、倒计时策略或进度模型，而是调用结构化运行共用的 `execute_frozen_session`。冻结前一刻 stdin 和 stdout 必须仍是真实 TTY，否则草稿保持可编辑，不发布冻结契约。

结构化 `gene-agent run` 不把完整 manifest 和 RunConfig 重建成 `TuiSessionDraft`。文件本身就是已定稿草稿：校验失败在冻结前停止；成功则冻结规范语义并立即进入共享交互执行器。这保留对话采集模型尚未表达的字段，并将计划草稿编辑限定在 TUI 创建路径。
