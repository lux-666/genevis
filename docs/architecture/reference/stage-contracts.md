<a id="stage-contract-reference"></a>

# 阶段契约参考

> **状态：** 当前阶段已接受并实现；精修路线限定于下方明确支持的输入。
>
> **阅读场景：** 精确生物学阶段输入、输出、方法或评审触发。
>
> **来源：** [运行时编排](../topics/runtime-orchestration.md)。
>
> **下游：** 资源校验、方法规划、执行、评审。
>
> **相关契约：** [资源交接](resource-handoff.md)、[描述符](descriptors.md)、[执行生命周期](execution-lifecycle.md)。
>
> **代码依据：** `app/workflow.py`、`app/graph/builder.py`、`app/stages/`、`descriptors/registry.yaml`。

本文定义各阶段当前生物学输入、输出、可执行方法和评审触发。跨阶段文件传输由[资源交接](resource-handoff.md)拥有；注册方法身份由 `descriptors/registry.yaml` 拥有。

<a id="shared-stage-shape"></a>

## 共享阶段形态

可执行阶段在适用时遵循：

```text
enter -> validate_inputs -> plan_methods -> method_subgraph
      -> collect_outputs -> review_outputs -> commit_stage
```

- 校验只读取紧凑状态和已验证引用。
- 固定阶段只接收注册表默认方法名单；Assembly 可省略 `selected_method_names` 来延迟绑定，省略不等于空名单。
- 规划解析适用性和参数，不改固定名单；延迟 Assembly 先据上游 QC/Survey 推荐并确认，再绑定一个已验证 `primary_method`。
- 方法执行使用共享 freeze/run/collect 生命周期。
- 收集发布产物和紧凑证据引用。
- 评审可打开人工门禁。
- 提交在适用时发布中央逻辑资源投影，并返回 occurrence 交接；不能隐式推进冻结工作流。

只有 occurrence 结算时，阶段才返回绑定当前冻结 occurrence 的明确 `workflow_step_outcome`。`completed` 表示契约已交付；`skipped` 仅限下方定义的阶段专属无交付路径；`failed` 表示未交付。子图停止未必是结算：`blocked_human` 是仍激活 occurrence 上的可恢复暂停，不返回 occurrence 结果。方法处置、尝试结果、私有游标或路由标记不能隐含 occurrence 结果。父控制器和终态恢复共用结算边界，验证明确交接、证明有效名单精确覆盖、记录不可变交付、写唯一 occurrence 终态。延迟 Assembly 使用已验证阶段方法绑定作为有效名单，并要求其方法处置。

冻结序列决定到达哪些阶段，不证明生物学输入存在；每个到达阶段在规划或执行前自行解析和验证逻辑资源。

Review 是可选可复用 occurrence。组装生产/精修阶段不建立强制 Review 边界。显式 `assembly_review` 带精确 `review_target`：`resource_type: "assembly"` 及最近前置交接生产者 `producer_step_id`；冻结不补入缺失 Review。因此 `assembly -> hic_scaffolding -> assembly_review` 评审 Hi-C 交付；`t2t` 可以直接结束或进入 Base Polish，无需 Review。

规范完整基因组推荐如下：

```text
read_qc -> genome_survey -> assembly -> assembly_dedup -> assembly_review
        -> hic_scaffolding -> gapcloser -> t2t -> base_polish
        -> assembly_review -> done
```

该推荐定义预期生物学交接顺序，不强制整份列表。首个 Review 评估 Dedup 后保留的组装；最终 Review 绑定 Base Polish，用相同 QUAST/BUSCO 配置评估源和抛光候选，可保留任一个。`done` 是隐式父图终态，不是可请求阶段。

未注册未来工具不是候选。新增时须在同一变更提供描述符、可执行适配器、路由、运行配置契约和测试。

## 1. Inventory

目的：注册 manifest 资产，不转换内容。

输入：

- manifest 文件及目录声明。
- 可选样本、平台、组装层级、基因组大小元数据。

接受行为：

- 接收已通过[运行数据](data-contract.md#49-declared-input-content-is-logically-non-empty)常规文件和逻辑/解压非空校验的冻结 manifest。
- 校验并记录提供资产。
- 发布资产注册表。
- 资产存在时初始化 `long_reads`、`short_reads`、`hic_reads`、`assembly`。
- 将 `assembly_fasta_registered` 注册为中央 `assembly`。

已有组装不改写或约束冻结 occurrence 序列。`assembly_review` 是通常推荐的首个分析 occurrence，不是公共运行入口限制。

长 DNA/RNA 分类歧义打开评审门。Inventory 不执行 QC、Survey 或组装工具。

## 2. Read QC

目的：检查长读段，可选执行获批长度过滤，准备工具局部交接产物。

逻辑输入/输出：

- 要求 `long_reads`。
- 获批过滤后更新 `long_reads`。

注册方法：

- `read_qc.long_read_stats.seqkit_stats`。
- `read_qc.length_filter.skip`。
- `read_qc.length_filter.seqkit_seq`。

SeqKit 统计始终描述选定长读段资源。只有冻结阶段请求已有正 `min_len` 和明确批准标志时才过滤。不存在阶段局部 Read QC 评审门或已实现的过量读段损失自动阈值；阈值缺失或未批准选择注册 skip。FOFN 是内部产物，下游消费规范路径列表。输入缺失/不可用或执行失败终止阶段，不打开 Read QC 评审门。

接受的 Read QC 事实包闭合且可重算：

- 执行前将有序读段成员冻结为依赖；注册成员保留资产 ID 与注册表引用，运行内成员保留完整 `ArtifactRef`。
- 逐文件 SeqKit 统计是诊断，保留精确有序源身份。
- 每次运行（含单文件）发布一份不可变全读段汇总，直接从原始读段流计算，不合并或加权四舍五入的逐文件行。
- 汇总拥有读段数、总碱基、最小/最大/算术平均长度、全集 N50、Q20/Q30 碱基百分比、平均质量。FASTA 质量明确不适用，不填零。零序列零碱基 FASTQ 的规范值为 `Q20=0.0`、`Q30=0.0`、`AvgQual=0.0`，无论 SeqKit 该版本输出零还是空。解析器在不变量比较前对逐文件/全集行统一应用规则。
- 汇总直接依赖冻结读段，不能声称来源是逐文件统计。仅含汇总指标的摘要依赖汇总；嵌入逐文件诊断则列两个引用。
- 过滤资源发布不可变 read-set manifest，将每个派生成员绑定源资产/引用及批准参数。

批准过滤后零保留仍可完成方法并发布零值汇总，但 occurrence 以 `no_reads_retained` 失败，不以空文件替换 `long_reads`。这不同于冻结前即拒绝的声明空输入。

## 3. Genome Survey

目的：估计基因组大小和 k-mer 指标。

逻辑输入：

- 完整 k-mer Survey 至少需要一个可用 `short_reads` 或 `long_reads`。
- 优先短读段；族内 clean/raw 优先级归规范资源解析器。
- 无可用读段时，明确操作者大小可支持 `skipped_no_usable_reads` 证据路径；两者皆无则失败。

输入完整性由声明决定。仅长读段且未声明短读段是合法、有意的 Survey 输入；阶段不猜用户漏文件。两族均声明时优先短读段。`genome_size_estimate` 仍是操作者先验，优先级由[共享事实](shared-facts.md#estimated_genome_size)拥有。

注册方法：

- `genome_survey.kmer_count.jellyfish`。
- `genome_survey.model.genomescope2`。

发布不可变 Survey 证据和紧凑标量观测，不创建跨阶段路径资源。Assembly 需要大小时若既无明确值也无有效估计，工作流以 `GENOME_SIZE_UNAVAILABLE` 失败。Checkpoint 的 `survey_input_family`、`resolved_assets` 只暴露 short/long 逻辑族。各读段保留注册表冻结 `compression`：Jellyfish 直接消费 `none`/`unknown`，`gzip` 通过 `gzip -cd` 流式读取，无需操作者先解压。其他声明编解码在拥有明确执行契约前拒绝 manifest；后缀不是压缩权威。GenomeScope2 使用冻结 `shared_parameters.species_facts.ploidy`，缺失则二倍体 `2`。仅长读段估计标低置信，短读段标高置信。Jellyfish 发布类型化 k-mer `ArtifactRef`，GenomeScope2 直接消费，无路径字符串转换。

gzip 输入的冻结脚本调用两个命令，因此所选机器方法映射必须在探测闭包声明 `jellyfish` 和 `gzip`。活动映射经[整次运行就绪](execution-lifecycle.md#whole-run-readiness-boundary)冻结，`envs.yaml.example` 只是种子。

结果明确：有可用读段且授权本地执行时，两方法均须完成；有读段但未授权本地时，明确大小可生成完成的 `provided_only`，两个方法为 `skipped`，否则失败；无读段但有大小时使用定义的 `stage_skipped` 路线。持久 `GenomeSurveyResult.status` 必须与 occurrence 结果一致，结算后不能停在 `planned`。

## 4. Assembly

目的：从长读段构建可审计组装候选。

输入/输出：

- 要求 `long_reads`。
- 需要时消费明确或 Survey 派生的基因组大小。
- 可将已注册 Hi-C/RNA-seq 存在性用于参数推荐上下文。
- 发布 `assembly`。

注册方法：

- `assembly.candidate.flye`。
- `assembly.candidate.hifiasm`。
- `assembly.candidate.hifiasm_ont`。
- `assembly.candidate.nextdenovo`。

兼容性按所选读段类型和上游证据验证。推荐载荷显式标记上游证据状态；生产者未完成的字段为 null，不得当观测。路线/方法决策属于阶段，遵循冻结[推荐策略](recommendation-policy.md)。顾问模式从输入兼容的冻结候选中推荐，分别持久化请求、响应、解析推荐，并由人工确认绑定 `assembly_selection`。确定性基线跳过模型；路线/平台证据不足以确定兼容默认方法时拒绝。响应发布后崩溃按请求哈希回放，不再调用模型。

Assembly、AutoHiC 参数推荐在方法选择后，顾问在冻结尝试前通过 parameter_approval。两者及 T2T 的显式确定性参数基线生成空 L2，以不可变策略决策批准单轮，不打开人工参数门。T2T 基线要求提供基序，不调用模型；默认 advisor 使用 finishing 完整参数提案与结果反馈，进入 finishing_recommendation。动态 Assembly 从冻结依赖快照解析并通过阶段预检，不读取实时环境。精确事实位置见[运行数据](data-contract.md#101-assembly-method-recommendation)。每次推荐为尝试局部稀疏映射，重试不继承省略值，也不能用模型输出绕过审批。`assembly.threads` 是方法无关 RunConfig 执行资源，冻结到所选方法 L0。Hifiasm/Hifiasm-ONT 的已提供 `species_facts.ploidy` 作为 L1 绑定 `--n-hap`；缺失时保留目录二倍体默认 `2`。

缺必需参数、读段不兼容、执行失败、缺预期输出均为明确失败，不是规划成功。未授权本地执行不是成功 Assembly 规划：除非另有明确定义复用路径能交付已物化且契约有效候选，否则无交付失败，所选方法 `skipped`。仅 planned candidate 包不能支撑 `stage_completed`。

## 5. Assembly Dedup

目的：保留原始组装候选，同时创建保守去冗余证据。

输入/输出：

- 要求 `assembly`；`read_depth` 策略还要求 `long_reads`。
- 以保留的候选关系更新 `assembly`。

注册方法：

- `assembly_dedup.purge_dups`。
- `assembly_dedup.eval.quast`（仅启用顾问循环时选中）。

RunConfig 同时冻结本地执行和 `assembly_dedup.automatic_purge_approved` 后，阶段按所选策略物化证据：默认 read_depth 包含深度、cutoff、自比对、重复区、purged 序列；self_alignment 省略覆盖度估计。保留原候选供 Review 比较或保留。没有明确 purge 授权则原样透传。物化输入缺失显式表示，不合成成功 purge。

由 Inventory 注册的已有组装，其候选 primary 成员是 FASTA 资产。read_depth 绑定当前运行已注册的完整长读段集合为比对证据，缺读段失败；平台为 unknown 或无法映射 minimap2 preset 时，在冻结尝试前返回结构化失败，purge 方法为 not_started。self_alignment 不消费读段，不要求映射 preset。不能把 FASTA 资产 ID 当读段 ID，也不能把本次比对读段推断为原始组装来源。Assembly 产生的候选仍按 primary 读段 ID 和平台严格匹配。

`recommendation_mode: deterministic_baseline` 保留单次路径；`coverage_mode: self_alignment` 选择无覆盖估计替代路线。默认仍是 `read_depth` 与自动 `calcuts`。

`recommendation_mode: advisor` 先对源候选运行 QUAST，再打开持久 `dedup_recommendation`。每个获批提案可从同一原始组装执行新配置；下一提案前 QUAST 比较源和全部完成候选。模型输入是完整 QUAST 表、实际命令、历史设置和推荐，没有硬编码质量分数。可选参考基因组对所有候选使用同一注册参考。

`max_attempts` 限制 purge 执行次数。每次使用唯一工作/产物目录及候选身份。重复配置和不存在候选选择校验失败。最终提案可选任一完成尝试或保留源。确认复用既有前台 `review_policy` 倒计时，只能自动遵循精确保存提案。无效推荐或明确 `human_review` 没有合格自动决策。

中断后恢复请求/响应产物而不再调用模型。审批绑定请求、来源、历史、提案、occurrence；命令冻结重验绑定和参数相等性。方法处置在循环选定交付或失败时结算，不在中间成功尝试后结算。执行/收集失败停止 occurrence，保留完成候选作证据。

`self_alignment` 生成 `purge_dups self.paf.gz`，省略覆盖相关 `-2 -T/-c`；`end_only` 控制 `get_seqs -e`。空重复 BED 合法，输出 haplotig FASTA 保留。该策略是可选替代，不是普遍质量保证。

基线结果表：

| 输入候选 | 本地执行 | 自动 purge 批准 | Occurrence 结果 | 阶段交付 | `purge_dups` 处置 | 组装交付 |
|---|---|---|---|---|---|---|
| 已物化 | 已授权 | 是，成功 | `completed` | `executed` | `completed` | `materialized`，新 purged 候选 |
| 已物化 | 已授权 | 否 | `completed` | `pass_through` | `skipped`，`automatic_purge_not_approved` | `reused`，原候选 |
| 已物化 | 未授权 | 任意 | `completed` | `pass_through` | `skipped`，`local_execution_not_enabled` | `reused`，原候选 |
| 缺失/未物化 | 任意 | 任意 | `failed` | 无 | `not_started`，`no_materialized_assembly` | 无 |
| 已物化 | 已授权 | 是，执行/收集失败 | `failed` | 无 | `failed`，附原因 | 无 |

透传有意不使用 `stage_skipped`：方法虽未运行，Dedup occurrence 仍交付已验证组装关系。缺输入也不是失败 purge 尝试，因为没有开始。Checkpoint `purge_dups_status` 只是 occurrence 绑定方法处置的投影。

## 6. Assembly Review

目的：评估组装候选并采纳下游使用的组装。

输入/输出：

- 要求 `assembly`。
- 评审或获批校正后更新 `assembly`。

Assembly Review 是可复用独立子图。Inventory 通过与上游产物相同的中央 `assembly` `ResourceRef` 注册已有组装。到达 Review 时，阶段验证组装和当前操作所需候选/证据，不得以裸 `fasta_path` 绕过资源契约。此契约适用于任何拥有中央交接的 occurrence 之后，包括 Assembly、Dedup、Hi-C、GapCloser、T2T、Base Polish 或先前 Review，不论字节是否改变。加载候选前验证生产者规范完成终态和唯一不可变 `resource_delivered`，再要求中央引用的候选、主输出、路径、源阶段/步骤与交付和冻结 `review_target` 一致。可变标签或单独阶段名不足。重复 occurrence 从同一不可变阶段种子的新副本开始，不采用 occurrence 专属方法配置，并以步骤产物、尝试、评审记录和 Ledger 事实区分。

注册方法：

- `assembly_review.eval.quast`。
- `assembly_review.eval.busco`。

QUAST/BUSCO 经有序共享生命周期收集。QUAST 默认无参考；manifest 可注册恰好一个 `reference_genome_fasta`，Inventory 暴露为 `reference_genome`；每次 QUAST 将其通过 `-r` 使用前绑定资产身份、大小和校验和。该运行参考 FASTA 独立于主机 NCBI 快照，与会话输入一起冻结。

BUSCO/NCBI 仅从冻结依赖 manifest 的主机数据库注册表解析。Review 要求 manifest `user_metadata.target_species` 为 NCBI 拉丁学名（如 `Solea ovata`），这是唯一运行所属物种入口；俗名、LLM 推导名、其他 metadata 键、`Genus sp.` 等未定名不参与解析。缺失/格式错误在输入准备时带 manifest 字段路径失败；无法解析的名称在配置 NCBI 快照就绪检查失败。

主机注册表拥有唯一 `databases.ncbi.reference_dir`。每个直属子目录是一份同前缀 `*.ncbi.json`、`*.ncbi.lineage`、`*.ncbi.lineage2`、`*.ncbi.tsv` 快照。就绪检查遍历全部安装子目录，以精确、同属、同科、同目后备搜索目标。完整 lineage 提供匹配词，lineage2 提供等级分类提示。选择最佳可用层级，同级优先 primary 记录更多者，再按快照目录名。全部子目录都无有用证据才失败。

BUSCO 先对选定验证快照应用确定性分类规则：精确目标 taxid 的完整 `*.ncbi.lineage`，或无精确目标时全部同属行的共同完整分类，包含 `Viridiplantae`/`Embryophyta` 时，必须选择已安装 `embryophyta_odb10`。lineage2 仅提供等级提示。不得从快照目录名、近缘物种 `Annotation BUSCO Lineage` 或主机默认推断分类；植物目标缺固定 lineage 时在冻结前阻塞。

固定植物规则不适用时，从所选快照的近缘物种 `Annotation BUSCO Lineage` 映射，与主机注册表取交集，冻结前要求恰好一个兼容已安装 lineage。固定植物规则优先于更窄植物注释。快照创建/安装负责参考数据生成验证；用户无需检查行或选择 lineage。`assembly_review.busco.lineage` 不是公共选择入口；若 RunConfig 包含它，只作一致性断言，与物种推导不符则带字段路径失败。模板/TUI 不生成它。RunConfig 不拥有数据库路径、版本、来源；主机 `default_lineage` 不作为无法解析物种的后备。Review 可接受候选、采纳另行完成的 polish/purge 动作或停止；决策绑定当前请求和类型化候选引用。

NCBI 比较证据和统计验证解析精确相同的快照字节。依赖冻结记录每个已装子快照四文件联合身份。目标解析和稳定来源失败分类共享；首个有用层级证据选择与全层统计队列构建保持各自阶段契约。

目标物种由 manifest 拥有。Inventory 将冻结拉丁学名投影为 Assembly 方法/参数共享事实，拒绝冲突 RunConfig 副本。缺失、格式错误、无法解析、BUSCO 映射歧义或主机缺 lineage，均在执行前阻止 Review；系统不编造目标、不用 LLM 解析俗名、不回退无关全局 lineage。

BUSCO 重复、大小膨胀、异常深度、证据缺失/不一致等信号阻止无支持自动接受。

## 7. Hi-C Scaffolding

目的：生成并评审染色体级 AutoHiC 组装。

输入/输出：

- 进入时要求 `assembly`、`hic_reads`。
- 采纳决策后重新发布中央 `assembly`，只有采纳 scaffold 才改变字节和候选关系。

注册方法：

- `hic_scaffolding.candidate.autohic`。

只有到达冻结 occurrence 才运行。缺组装、配对 Hi-C 或配置时，私有游标为 `blocked_missing_requirements`，明确 occurrence 结果却是 `failed`，因此父结算到 `failed_terminal`，无 `active_interrupt`。游标只是诊断，不是可恢复或终态结果。AutoHiC 拥有 Juicer/3D-DNA 与模型辅助校正流水线。L2 可调整限制性内切酶、编辑轮次、校正开关、错误阈值。顾问模式每个结构有效稀疏推荐都需参数审批；显式确定性基线按[策略契约](recommendation-policy.md)批准初始尝试。执行成功再开采纳门。只有不可变参数审批/采纳门可将当前 occurrence 暂停为 `blocked_human`。操作者可采纳染色体组装、保留旧组装或请求新参数尝试；执行失败重试需明确策略和预算。

采纳 scaffolding 时验证当前候选与主产物，在替换中央资源前于当前 occurrence 发布派生 executed candidate；不可变采纳决策标明该候选。保留旧组装保留字节和关系，但在当前 Hi-C occurrence 下重新发布中央引用。

冻结就绪验证输入、本地工具、模型、声明输出位置，不预检尚未生成文件。进程成功且首次新发布前，收集要求所有冻结 `expected_outputs`（FASTA、`.hic`、`.assembly`、HTML）为文件，或已有匹配尝试的索引发布可恢复。缺任一且无恢复记录，则以 `failure_kind: collect` 终止，无评审候选、采纳门、评审包或产品发布。发布非事务：后续存储失败可留下早先已索引孤儿；孤儿无 `attempt_succeeded`、`artifact_registered` 语义事件，不开门、不自动重试或回滚。

## Finishing 顾问共同契约

GapCloser、T2T、Base Polish 的新 RunConfig 默认 `recommendation_mode: advisor`，初始预检冻结对应 `<stage>.eval.quast` 与生物方法；无本地执行授权仍不得运行。每阶段对源及全部已完成候选做同设置 QUAST，将原生校验/端部支持/前后比对报告纳入结构化请求。run/accept/keep_source/human_review 均经 `finishing_recommendation` 人工门；已完成候选按 attempt_N 精确选择，非法响应、跨阶段参数、重复配置、越预算与不可用候选禁止执行。max_attempts（默认 3）存入不可变设置引用，checkpoint 中的副本不能扩预算。

每次尝试从同一已验证源组装开始；attempts/NNN 隔离工作及产物，设置/请求/响应/assessment 都落盘。批准决定、源、推荐、历史与工作流 occurrence 在生成脚本前复核，执行依赖包含批准/推荐引用。恢复复用已发布响应和共享 Runner 证据。结算只在整个循环终止时聚合一次方法处置，防止多轮 disposition 冲突。采纳早先候选时交付其主输出与比较证据。

T2T 顾问目前要求 auto_patch，采纳仍需人工。源请求含端部 motif-copy 测量；反馈含扫描、过滤前每条 PAF 两端的 MAPQ/重叠/距离/尾部基序测量及全部拒绝原因。独立 alignment_observations.tsv 绑定冻结尝试、执行与 PAF，不改变补丁候选表语义；统计遍历全部行，最多 100 行示例，拒绝计数按比对端而非唯一读段计算。候选 supporting_reads 去重计数投影为 supporting_read_count，数值汇总覆盖所有行。最终报告重用所选轮次的冻结输入。Base Polish 暴露 Racon window/error/scoring 的有界参数，每候选只跑一轮。顾问必须将同批校正读段一致性视为样本内证据，不据此宣称独立 QV 或默认无限校正。

`deterministic_baseline` 对 GapCloser/Base Polish 使用操作者配置单轮；对 T2T 使用冻结目录 L0、空 L2 的 schema-3 策略批准单轮，不调用阈值或基序模型。默认 finishing 有自己的[任务证据契约](data-contract.md#finishing-task-evidence)，不沿用 schema-3 参数 manifest。旧 T2T schema-3 初始 LLM 代码保留既有记录读取与受控兼容调用，新 RunConfig 不通过该路径请求模型；活动旧会话必须保留匹配检出，不迁移 checkpoint。真实工具与真实样本验收是独立门禁。

## 8. GapCloser

目的：用明确分类的 PacBio HiFi 通过 TGS-GapCloser 填内部 N 缺口。首个实现仅支持无错误校正路线 `--ne --tgstype pb --minmap_arg '-x asm20'`。ONT、CLR、CycloneSEQ、未知、混合平台集合均拒绝，不重新分类；长度过滤后的含噪读段不推断为已校正读段。

注册方法 `gapcloser.tgs_gapcloser` 通过现有解析器消费当前已验证候选与完整当前 HiFi 集合。验证 plain/gzip FASTA/FASTQ 并规范化为运行内 FASTA，索引输入记录保留源身份。原组装和读段不覆盖。每 occurrence 独立工具目录，因为上游工具使用固定 checkpoint 标签。

默认 advisor 根据源缺口、QUAST 和历次填补/校验报告推荐完整参数；显式 deterministic_baseline 使用 RunConfig 操作者设置，单轮无 LLM。执行用共享 freeze/run/collect。原生 1.2.1 两项产品 `.scaff_seqs`、`.gap_fill_detail` 必需；上游 README 后缀不同，适配器遵循[已核验上游源码](https://github.com/BGI-Qingdao/TGS-GapCloser/tree/e1a7e01c6259e6651a8007e4e4c3b887bef38b19)。收集校验 scaffold 身份、片段坐标、复制源序列与 N 段，发布输入/输出缺口数和长度报告。这证明结构一致，不证明共识准确或生物学改善。无内部缺口 scaffold 必须不变；未填 N 段消费相同源 N，不能丢弃源前后缀。插入序列和重叠闭合（含无 F 段的相邻 S 段）必须在保留锚点间恰好桥接一个内部源缺口。只可修剪该缺口侧翼，并报告移除碱基。

Advisor 使用 finishing_recommendation 人工门批准新 run、选择任意已完成候选或保留源。单轮基线使用 gapcloser_adoption 人工门，提供 accept_candidate、keep_prior_assembly、stop_human_review，超时不自动采纳。只有验证后的采纳替换中央资源；保留则 reused 交付。缺本地授权拒绝。组装无内部缺口时复用当前候选，不运行工具，方法记 skipped。执行后填零缺口是合法报告结果，不代表改善。真实工具验收与 fixture 集成分开报告。

接受的位置/交接设计：

- 完整推荐在 `hic_scaffolding` 后、`t2t` 前放 `gapcloser`。
- 已实现阶段要求当前中央 `assembly`。
- 成功采纳发布 occurrence 所属候选，替换供 T2T 或后续 Review 消费的中央交接。

后续 Review 绑定此 GapCloser 交付；后续 Base Polish 消费采纳或保留的组装。

## 9. T2T Finishing

目的：检查端部端粒证据，保守延长受支持的组装末端，不覆盖输入 FASTA。

输入/输出：

- 要求当前采纳 `assembly` 和 `long_reads`。
- 消费完整有序、保留平台的长读段集合，包括 ONT、PacBio HiFi、CLR、CycloneSEQ。
- 评审后重新发布中央引用，只有选定物化补丁才改变字节/候选关系。

T2T 消费到达该冻结 occurrence 时的中央组装，不直接消费 `hic_reads`。`t2t.assembly_fasta_ref` 仅是当前中央组装相等断言，不能绕过早先采纳。

注册方法：

- `t2t.alignment.minimap2`。

阈值推荐或扫描前，从规范已提供物种事实的 schema-3 快照解析一个端粒基序。已提供 `species_facts.telomere_motif` 直接验证并经证据链记录；否则持久 LLM 边界从提供学名解析。缺身份、模型未解析、v3 前 motif/config checkpoint 字段均拒绝，不能成为基序证据，也不假定常见重复。Advisor 将验证后的基序与来源绑定到 finishing 设置及每轮 frozen.json，以 finishing_recommendation 人工批准完整阈值。Deterministic baseline 仅接受已提供基序，绑定 schema-3 快照，策略批准空 L2，解析目录 L0 后扫描或运行 minimap2；未提供基序明确失败。读段在一个冻结尝试内按平台预设分组，生成一份合并不可变 PAF。严格应用批准支持阈值，不增隐藏下限或接合点分布否决。记录来源归属的端粒扫描、尝试/执行绑定候选证据、接合点感知补丁、精修报告。

`auto_patch` 先创建验证候选；advisor 经 finishing_recommendation 比较/选择，单轮基线经 t2t_review 人工采纳。采纳前不改变共享 assembly。反链尾部转为目标正向；非零接合距离先修剪目标再延长。歧义、嵌合、重叠或冲突证据不静默物化，源 FASTA 永不覆盖。

采纳改变组装时，T2T 验证当前候选与精确补丁主产物，发布绑定不可变采纳决策的派生 executed candidate 后替换资源，保留前驱候选和主输出谱系。保留旧组装则字节/关系不变，在当前 T2T occurrence 下重新发布引用。

## 10. Base Polish

使用一轮仅 HiFi 的 minimap2/Racon 校正当前已验证组装的替换和小 indel。固定方法 `base_polish.racon`，完整当前集合必须明确为 `pacbio_hifi`。规范化以唯一 ID 保留每条读段。Racon 使用 `--include-unpolished --no-trimming`，scaffold ID 必须不变。Advisor 推荐 window/error/scoring 配置，每个候选从相同源开始执行一轮；deterministic_baseline 使用操作者参数单轮。两者都不声称必能改善高质量 HiFi 组装。

采纳前，同一读段以相同 minimap2 `map-hifi -c --eqx --secondary=no` 设置映射源和候选。两个 PAF、候选 FASTA、工具版本、源绑定比较报告必需。报告比对读段数、映射碱基、碱基一致性、目标覆盖、scaffold/缺口/长度变化，并明确独立 QV 缺失。PAF match/block 计数须与 eqx CIGAR 一致，计入 minimap2 从这些统计排除的歧义碱基 `nn`。它是样本内比对一致性，不是无偏错误估计；一致性或 N50 更高都不自动批准。缺失/畸形产物在门禁前收集失败。

Advisor 使用 finishing_recommendation，单轮基线使用 base_polish_adoption；两者支持人工采纳、保留源、停止，advisor 还可批准新配置或选择早先候选。采纳才改变资源；保留源发布 reused 交付。两个替代项与比较都是不可变产物。交接保留另一个候选，使后续 Review 即使在保留源之后，也用该 occurrence 相同 QUAST 设置/BUSCO lineage 评估两者；最终 Review 可选任一个。缺评估信号显式保留，不是质量通过。QUAST 参考相关错误、BUSCO 完整/单拷贝/重复/片段/缺失计数补充连续性；没有独立参考/读段真值则共识 QV 未测。

接受的位置/交接设计：

- 完整推荐在 `t2t` 后放 `base_polish`。
- 要求当前中央 `assembly` 及选定方法所需抛光证据。
- 成功采纳发布 occurrence 所属抛光组装作为中央交接。
- 后续最终 `assembly_review` 绑定精确 Base Polish 生产步骤。

服务器验收路线显式授权全部请求 occurrence。验收区分真实进程证据、声明方法 skip、验证阶段交付、质量评估、人工决策。只有 FASTA 或完成会话不足以证明全路线执行。收集证据必须标明完整索引输出；早先成功尝试不能抹除方法最终失败/skip。

同一只读诊断支持明确范围 `complete`（默认）、`contig`、`review`、`hic`、`dedup`、`gapcloser`、`t2t`、`base-polish`、`finishing`。各范围要求精确 occurrence 序列；切片不报告为完整路线覆盖。独立切片经 Inventory 注册源 FASTA，验证首个工具消费该候选主输出。终态 Review 要求每个候选均有 QUAST/BUSCO 执行与收集指标。Base Polish 执行后进入 Review 的范围要求同设置不可变源/所选抛光对；advisor 未执行便保留源时，仅评估源并标记 not_executed_source_retained，不算 Racon 执行覆盖。无 Review 范围明确无最终 QUAST/BUSCO 质量评估。合法 skip 仍使工具执行覆盖不完整。

单阶段 Hi-C 范围可比较执行后采纳门的只读审计与分离恢复后完成审计，绑定同一会话、冻结输入和请求，要求 attempt-start 事件、执行证据、收集输出哈希不变。它是执行回放检查，不是生物学验收，也不防御操作者故意伪造基线。参数重试是新执行，不能通过不重跑断言。
