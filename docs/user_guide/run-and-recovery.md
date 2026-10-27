<a id="run-and-recovery"></a>

# 运行与恢复

这是启动、检查、评审、恢复 Gene Agent 会话的唯一操作者指南。行为说明见[运行时编排](../architecture/topics/runtime-orchestration.md)和[评审与恢复](../architecture/topics/review-and-resume.md)；精确生命周期由[运行数据契约](../architecture/reference/data-contract.md)及其链接拥有。CLI 标志由 `app/entrypoints/run.py` 定义，部署页链接此处，不复制运行契约。

<a id="acceptance-order-code-tests-before-real-end-to-end-runs"></a>

## 验收顺序：先代码测试，再真实端到端运行

**单元/集成测试 → 小数据真实工具冒烟 → 分段端到端 → 完整真实端到端。**

完整十阶段模板是最终验收目标，不是下一次调试运行。先盘点既有测试与真实运行证据，再补已证明缺口。伪工具通过只证明应用契约，不证明真实生物工具能工作；主机就绪、FASTA 或完成会话单独都不能证明。

<a id="1-audit-and-complete-unit--integration-coverage"></a>

### 1. 审计并补齐单元与集成覆盖

本层使用 fixture、伪可执行文件和受控失败，不运行大型真实工具。逐阶段检查非法/缺失输入、产物身份/交接、路由/门禁/恢复、持久失败、明确 skip。证明收集失败不能发布成功交接，恢复不重复已完成进程。扩展既有测试，不另建并行框架。

以下是已有测试代码清单，不代表全部边界或真实工具路线已验收：

| 边界 | 已有证据 | 剩余验收 |
|---|---|---|
| 阶段输入校验 | `tests/test_inventory.py`、`test_read_qc.py`、`test_genome_survey.py`、`test_assembly.py`、`test_hic_scaffolding.py`、`test_gapcloser.py`、`test_base_polish.py`、`test_t2t.py` | 检查路线实际非法输入矩阵，真实执行前补未覆盖情况 |
| 产物交接 | `test_assembly_candidate_bundle.py`、`test_resource_resolution.py`、`test_occurrence_settlement.py`、`test_workflow_plan.py` | 小型端到端用真实产物证明连续交接 |
| 路由/门禁/恢复 | `test_graph_routing.py`、`test_review_gates.py`、`test_review_application.py`、`test_resume_transaction.py`、`test_durable_resume.py` | 真实工具门禁跨进程/SQLite 关闭重开 |
| 失败与 skip | `test_execution_lifecycle.py`、`test_execution_collect.py`、`test_ledger_v2.py`、阶段测试 | 分开测试非零退出、缺失/畸形产品、不完整执行、允许 skip；不能误报成功或自动重跑 |
| AutoHiC 恢复 | `test_hic_scaffolding.py::HiCScaffoldingStageTests` 用伪环境覆盖执行 checkpoint 收集回放、采纳/保留 | 独立真实工具分离人工门恢复，断言执行数不变 |
| Base Polish 恢复 | `test_base_polish.py` 用伪工具覆盖收集回放、损坏证据拒绝、成对评估、SQLite/CLI 恢复 | 真实 Racon/minimap2 冒烟及代表性质量验收 |
| 占位边界 | `test_registry_executable_invariant.py`、`test_finishing_plan.py`；Base Polish 已可执行 | 未来请求占位必须确定失败、记录失败、无成功交付、不进下游。不支持输入不是占位或成功 skip |

**真实工具检查必须显式选择。** 普通 `pytest -q`、`server_test.sh` 排除 `real_tool`。收集测试不探测 Conda 或生物工具；被选择测试在执行时检查前置条件，缺工具则失败。`Phase4ReadQCRealSmokeTests` 使用活动注册表中 `read_qc.long_read_stats.seqkit_stats` 的映射，经正式 CLI 预检、冻结后执行 SeqKit；标记的 hifiasm help/version 分别使用 `assembly.candidate.hifiasm` 和 `assembly.candidate.hifiasm_ont` 的映射，不使用 `HIFIASM_BIN` 或外层 PATH 选择二进制。这是 CLI 兼容检查，不是组装冒烟。开发主机缺二进制不是代码缺陷；取消选择不证明真实执行成功。

Read QC 也支持独立的 paired short-DNA 输入：在 `read_qc.short_reads` 中冻结
fastp 参数即可按每个 lane 的 R1/R2 执行，不要求 long reads。Hi-C 使用
`read_qc.hic_reads`；默认 `preserve_hic_junction: true`，避免 adapter trimming
破坏连接位点。完成后通过 `resource_refs` 交付 clean reads、paired-member
身份、read-set manifest 和 fastp 报告；恢复时可以删除 work 目录，collect 会从
已发布 artifact 恢复而不重跑 fastp。下游 AutoHiC/Hifiasm 消费 clean artifact，
即使选择锚点来自原始资产也不会回退到 raw 路径。可直接复制
`docs/examples/hifi_complete_run_config.template.json` 中仅用于 Hi-C 的
`read_qc.hic_reads` 段；`contig_run_config.template.json` 没有 `read_qc`
段。材料身份、配对读段和清洗边界见[材料与组装路线](materials-and-assembly.md)。

<a id="2-run-small-real-tool-smoke-tests-for-each-key-stage"></a>

### 2. 对关键阶段运行小型真实工具冒烟

使用有界且符合工具要求的输入，实际调用适用的 SeqKit、Jellyfish/GenomeScope2、Flye 或所选兼容组装器、purge_dups、QUAST、BUSCO、AutoHiC、TGS-GapCloser、Racon/minimap2。检查冻结命令/输入身份、退出状态、必需原生产物、收集、索引、Ledger 结果、失败/恢复。版本/help 探测不够。记录版本、数据来源/校验和、资源限制；不要求冒烟数据产生优秀生物学组装。

已有真实入口包括显式选择 SeqKit、[可选 TGS-GapCloser 冒烟](#hifi-gap-closing-from-an-existing-assembly)、可选 Racon/minimap2 合成 HiFi 切片：

```bash
python -m pytest -q -m real_tool \
  tests/test_base_polish.py::test_real_racon_hifi_smoke
```

单独选择 SeqKit：

```bash
python -m pytest -q -m real_tool tests/test_read_qc.py::Phase4ReadQCRealSmokeTests
```

单独选择 hifiasm CLI 兼容检查（两个方法可映射到不同环境）：

```bash
python -m pytest -q -m real_tool \
  tests/test_phase10_parameter_catalog.py::ParameterCatalogSchemaTests::test_cataloged_hifiasm_flags_exist_in_runtime_help_when_binary_available
```

hifiasm 的简短帮助不列出所有已实现选项。检查对未显示的目录参数追加末尾 `-h`，验证真实解析器接受参数语法并退出，不启动组装；同时用不存在的选项验证解析器确实拒绝未知参数。参数值用占位值，仅证明选项可解析，不证明推荐值有效或组装质量。[0.25.0 官方源码](https://github.com/chhylp123/hifiasm/blob/0.25.0/CommandLines.cpp)中 `--ul-m` 的赋值处理尚被注释，两个目录已移除 `ul_mode`，避免顾问推荐不会生效的参数。

GapCloser 与 Racon 切片读取活动环境注册表（默认 `config/envs.yaml`，可由 `GENE_AGENT_ENVS_CONFIG` 指定），冻结依赖后通过各方法的 Conda 映射执行；运行 pytest 的 Python 环境不需安装这些工具，也不需临时修改 PATH。两者明确采用单轮 `deterministic_baseline`，不调用顾问或最终 Review 的 QUAST/BUSCO。Racon 检查成对比对、采纳门、收集回放，不建立代表性质量或独立 QV。其他所列方法尚无当前测试集证明的专属小数据真实冒烟，不能以伪工具替代。输入须满足真实约束（尤其 BUSCO/AutoHiC），不削弱校验以接受无效极小数据。合法 skip 与执行路径分别测试。

<a id="3-first-real-e2e-the-small-five-stage-contig-route"></a>

### 3. 首个真实端到端：小型五阶段 contig 路线

**操作者已跑过五阶段，复用该运行证据，不为填检查表逐项重跑成功阶段。** 仍需检查实际方法、skip、恢复分支；本文未审计尚未提供的服务器运行目录。Hifiasm 成功不覆盖 Flye，Dedup skip 不覆盖 purge_dups 执行。只对未覆盖路径或行为改动增加针对性测试。

新数据集或缺失分支使用小型真实可追溯数据运行 **QC → Survey → Assembly → Dedup → Review**，见[下方 contig 模板](#five-stage-contig-input)。保留准确事实和平台，选择适合 Survey/Dedup 的数据，不强迫不适材料执行。验收时人工门保持 `review_policy.max_blocked_seconds: -1`。

验证每个下游冻结尝试消费前阶段预期产物，不是手替路径。要求目标方法真实执行证据、收集指标、结算 occurrence、请求绑定决策、分离检查后哈希验证最终组装。合法 skip 要报告覆盖缺口并单测执行分支。失败时检查日志，证明受支持恢复或明确新会话，不重写冻结证据制造完成。见[五阶段缺口](../architecture/reference/conformance.md#five-stage-real-tool-contig-acceptance-is-not-automated)。

<a id="4-validate-hi-c-as-an-independent-e2e-slice"></a>

### 4. 独立验证 Hi-C 端到端切片

从已知良好组装和足量小型真实配对 Hi-C 开始，覆盖 AutoHiC → 必需产品收集 → 人工采纳门 → 接受/保留 → 分离恢复。使用真实决策 `accept_candidate`、`keep_prior_assembly`、`stop_human_review`，不编造通用 reject。`reject_parameters` 属参数审批，`request_parameter_retry` 在采纳门明确请求新尝试。

检查全部 AutoHiC 产品及采纳/复用交接。在人工门关闭进程/checkpoint 连接，用绑定决策恢复，断言已完成 AutoHiC 尝试不会再次启动：无第二进程/start 事件，尝试身份、输出哈希不变。明确请求新参数重试不同于回放。

<a id="5-validate-finishing-stages-independently"></a>

### 5. 独立验证精修阶段

向 GapCloser、Base Polish、T2T 提供已有组装和各自读段/事实，不为测试重跑 QC 到 Hi-C。独立运行以 `assembly_fasta_registered` 声明已验证源 FASTA，让 Inventory 创建新运行的规范候选/引用。验收证据保留源运行身份/校验和，不把其他运行局部引用复制到新 checkpoint。

- GapCloser：真实内部缺口执行与无缺口复用。
- Base Polish：真实校正、必需前后证据、采纳/源保留、成对最终评估。同读段一致性不是独立 QV，不预设改善。
- T2T：支持延长证据与无延长路径，人工采纳/保留，以及恢复不重跑完成比对。

全部切片用下方[独立命令](#independent-slice-server-runbook)：普通 run、明确
RunConfig、同一冻结会话只读运行 `route_acceptance`，不需另一个阶段 Runner。
模板已可供服务器运行，不等于真实冒烟或端到端已执行。

<a id="6-only-then-run-the-complete-real-e2e"></a>

### 6. 最后才运行完整真实端到端

前层通过后才用全规模或代表性真实数据。[完整工作流](#complete-hifihic-server-acceptance)
是最终集成验收，不是发现普通输入、收集或恢复 bug 的首选。`route_acceptance`
只读取冻结 RunConfig 投影出的全部 occurrence。不要为满足检查器加入无用阶段。

**当前优先级：** 复用完成 contig，补已证明的代码测试/隔离和未覆盖方法/分支，再跑独立 Hi-C/finishing。不要仅因已验收执行没用专属冒烟 wrapper 就重跑，不先开十阶段。本文规定验收顺序，不声称待办测试或服务器运行已经执行。

<a id="before-a-real-run"></a>

## 真实运行前

按[新主机配置](../deployment/migration.md)准备 Linux/服务器检出，再运行：

```bash
bash scripts/server_check.sh
```

检查范围不同：

- server_check.sh 先探测编排器，再通过活动映射探测默认 contig 模板或首参 RunConfig 的固定工具/包闭包。Assembly 延迟，候选阶段检查；也验证 Review 所需 BUSCO/NCBI，不验证真实输入或 LLM。
- server_test.sh 是安装编排器后立即运行的早期回归门禁，其命令与位置归新主机指南；真实工具隔离规则见上。
- gene-agent run 在发布冻结前做输入、固定工具/包、数据库、LLM 专属检查；延迟 Assembly 后续从冻结快照检查。

<a id="five-stage-contig-input"></a>

## 五阶段 contig 输入

使用配套模板：

```bash
cp docs/examples/contig_manifest.template.yaml /server/path/manifest.yaml
cp docs/examples/contig_run_config.template.json /server/path/run_config.json
```

这是根 README 指定的权威五阶段示例，精确 occurrence 列表保留在 JSON，不在其他指南重复。
冻结时 `workflow_steps_from_run_config` 将 RunConfig 的字面阶段顺序投影为线性计划，并隐式加入
Inventory 首步；单个 QC/Survey、Assembly 和重复阶段均保留为独立 occurrence，不去重。RunConfig
配置的阶段和执行授权是执行与验收的唯一来源，不由注册表顺序或验收器补阶段。
替换所有服务器本地路径占位。模板有意不设 Assembly 方法/参数，QC/Survey 后由阶段推荐确认兼容方法，
再推荐参数。活动映射须声明运行可选择的每个候选工具需求。

完整 schema 有效载荷由示例文件拥有；RunConfig 仍是字面 occurrence/授权权威。延迟 Assembly 方法/参数是阶段证据，不是启动配置。当前计划是线性 occurrence 序列，不是通用并行 DAG；LangGraph 在 occurrence 内承载审批、恢复和参数重试回环，但不能改写冻结顺序。

<a id="public-data-comparison"></a>

论文公开数据只作为可追溯材料和结果参照：系统先依据 manifest、QC/Survey 事实和阶段证据推荐方法，
再执行并与论文公开结果对比；这不是复刻论文参数。验收退出 0 仅证明执行证据完整，生物学质量另行比较。

审计**已有**五阶段运行，不执行工具：

```bash
python -m app.diagnostics.route_acceptance \
  --run-dir /server/path/data/runs/EXISTING_CONTIG_SESSION \
  > /server/path/contig-acceptance.json
```

检查冻结计划中每个 occurrence 的进程/收集、产物交接和适用的最终 Review。还应检查 QC/Survey 产品及冻结输入依赖；前驱组装检查从组装交接开始，不是所有上游读段转换的全面证明。仅 QC/Survey 的
RunConfig 不要求 `final_assembly`；从已有组装开始的消费者要求 Inventory 提供已验证源，后续组装生产
occurrence 仍检查前一交付的输入和自身交接。最终 Review 按冻结的 `review_target.producer_step_id`
决定是否检查 Base Polish source/polished pair；不论是否有 pair，验收都只是执行证据检查，生物学质量另行比较。

<a id="hifi-gap-closing-from-an-existing-assembly"></a>

## 从已有组装进行 HiFi 补缺口

使用 [manifest](../examples/gapcloser_manifest.template.yaml) 和 [RunConfig](../examples/gapcloser_run_config.template.json) 完成独立 GapCloser，不重跑 Assembly、Hi-C、T2T：

```bash
cp docs/examples/gapcloser_manifest.template.yaml /server/path/manifest.yaml
cp docs/examples/gapcloser_run_config.template.json /server/path/run_config.json
```

替换组装/读段路径、会话 ID，提供完整当前 HiFi 集。活动主机配置（默认 `config/envs.yaml`）须把 `gapcloser.tgs_gapcloser` 映射到含 TGS-GapCloser 1.2.1、minimap2、GNU getopt（不是 macOS BSD）的环境。默认 advisor 模板还需 `gapcloser.eval.quast` 环境和 LLM 端点，通过缺口/填补报告与 QUAST 反馈推荐参数；不需 BUSCO 数据库。显式 `deterministic_baseline` 才使用操作者设置执行单轮，不调用 LLM 或 QUAST，不称最优 HiFi 推荐。

工具生成候选 FASTA、原生片段细节、索引校验报告，测量缺口/长度变化与复制片段一致性，不是 QV 或改善。用 review 或请求绑定文件执行 accept_candidate、keep_prior_assembly、stop_human_review。本节人工验收配置不自动采纳；完成后 inspect 报告最终采纳/复用组装。无内部 N 缺口则不调用工具，复用输入。

此处不支持 ONT/CLR/CycloneSEQ/未知/混合读段。完整工作流推荐不保证输入兼容，应明确省略不支持 occurrence；Base Polish 当前也要求 HiFi。独立质量评估需显式追加 Review 并配置工具、数据库、物种。精确范围见[GapCloser 契约](../architecture/reference/stage-contracts.md#8-gapcloser)。

配置主机可显式运行真实工具合成冒烟：冻结示例、运行 Inventory/GapCloser、关闭 SQLite，再经 CLI 恢复和最终 inspect。活动环境注册表需将 `gapcloser.tgs_gapcloser` 映射到含 TGS-GapCloser/minimap2/GNU getopt 的 Conda 环境；Racon 切片对应 `base_polish.racon`，需 minimap2/Racon。沿用正式运行的注册表和冻结解析，不使用 pytest 解释器的环境前缀：

```bash
python -m pytest -q -m real_tool tests/test_gapcloser.py -k real_tgs
```

确定性合成缺口不是代表性生物学验收。

<a id="independent-slice-server-runbook"></a>

## 独立切片服务器操作手册

**已跑过五阶段无需重跑。以下从已有组装拆分，在服务器仓库根、已激活编排器环境运行。** 使用 inspect JSON 的 final_assembly 中完整已知良好 FASTA，或可追溯外部组装。切片是**新会话**，不是中途恢复源会话。Inventory 导入并哈希；不要粘贴其他运行 ArtifactRef 到 manifest/checkpoint。

| slice / 审计范围 | RunConfig 模板 | Manifest 模板 | 工作 |
|---|---|---|---|
| `hic` | [hic](../examples/hic_run_config.template.json) | [hic](../examples/hic_manifest.template.yaml) | AutoHiC、收集、采纳、分离恢复 |
| `dedup` | [dedup](../examples/dedup_run_config.template.json) | [hifi_finishing](../examples/hifi_finishing_manifest.template.yaml) | purge_dups 顾问多轮、早先候选采纳、保留源 |
| `gapcloser` | [gapcloser](../examples/gapcloser_run_config.template.json) | [gapcloser](../examples/gapcloser_manifest.template.yaml) | TGS 与采纳/无缺口复用 |
| `base-polish` | [base_polish](../examples/base_polish_run_config.template.json) | [hifi_finishing](../examples/hifi_finishing_manifest.template.yaml) | Racon 与成对最终 QUAST/BUSCO |
| `t2t` | [t2t](../examples/t2t_run_config.template.json) | [hifi_finishing](../examples/hifi_finishing_manifest.template.yaml) | 端部延长证据、补丁采纳/保留 |
| `finishing` | [finishing](../examples/finishing_run_config.template.json) | [hifi_finishing](../examples/hifi_finishing_manifest.template.yaml) | GapCloser → T2T → Base Polish → Review，不重跑上游 |
| `review` | [assembly_review](../examples/assembly_review_run_config.template.json) | [existing_assembly](../examples/existing_assembly_manifest.template.yaml) | 已有组装仅 QUAST/BUSCO |

<a id="copy-and-edit-one-slice"></a>

### 复制并编辑一个切片

将 slice 改为上表范围。每试验使用**新** acceptance_work 目录，可共用数据根。以下尚不启动工具：

```bash
slice=hic
acceptance_work=/server/path/acceptance/hic-a
acceptance_data=/server/path/data
case "$slice" in
  hic) config_stem=hic; manifest_stem=hic ;;
  dedup) config_stem=dedup; manifest_stem=hifi_finishing ;;
  gapcloser) config_stem=gapcloser; manifest_stem=gapcloser ;;
  base-polish) config_stem=base_polish; manifest_stem=hifi_finishing ;;
  t2t) config_stem=t2t; manifest_stem=hifi_finishing ;;
  finishing) config_stem=finishing; manifest_stem=hifi_finishing ;;
  review) config_stem=assembly_review; manifest_stem=existing_assembly ;;
  *) echo 'Unknown slice'; exit 1 ;;
esac
mkdir -p "$acceptance_work"
cp -n "docs/examples/${manifest_stem}_manifest.template.yaml" "$acceptance_work/manifest.yaml"
cp -n "docs/examples/${config_stem}_run_config.template.json" "$acceptance_work/run_config.json"
```

**先停下编辑两个文件，再执行下一段。** 替换唯一 session_id、sample ID、真实物种、全部绝对输入路径。来源用 `sha256sum /absolute/source/assembly.fasta` 并将摘要/源会话记入资产 metadata；只是注记，不绕过 Inventory 字节验证。format、compression、平台必须匹配文件。保留所选数据集全部所需读段，不只列首文件隐藏其他成员。使用有效小数据，不任意截断 FASTQ。

- Hi-C 需同样本 R1/R2、AutoHiC 包/模型及完整工具闭包。已知时设置真实的 `hic_scaffolding.autohic_parameters.restriction_enzyme`，否则参数门解析；本切片无评估数据库。
- Base Polish/finishing 需 HiFi、真实物种、minimap2/Racon、QUAST/BUSCO 与主机 BUSCO/NCBI。两模板含最终 Review，不能删掉再声称质量验收。
- T2T 需读段和验证的物种/基序。已知时填 `shared_parameters.species_facts.telomere_motif`（及适当 ploidy），否则用持久 resolver；顾问根据端部测量与历次结果推荐阈值。auto_patch 不代表自动采纳。此切片无最终 QUAST/BUSCO，需要时另用 review。
- GapCloser 无缺口复用合法，但没执行 TGS；T2T 无支持延长也不是 bug 或成功延长证明。真实工具执行后保留源仍算执行覆盖。
- 保持人工门 `max_blocked_seconds: -1`；配置的 LLM resolver 仍需端点，server_check.sh 不检查它。

<a id="preflight-run-inspect-and-audit"></a>

### 预检、运行、检查与审计

编辑后在同一 shell 继续：

```bash
acceptance_session="$(python -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["session_id"])' "$acceptance_work/manifest.yaml")"
bash scripts/server_check.sh "$acceptance_work/run_config.json"
```

解决预检失败后，从真实交互终端运行，不把 run 管道接到 tee：

```bash
gene-agent run --manifest "$acceptance_work/manifest.yaml" \
  --run-config "$acceptance_work/run_config.json" --data-dir "$acceptance_data"
```

逐门检查证据。普通切片可当场决策或退出后按[请求绑定恢复](#resume-from-a-decision-file)。Hi-C 在接受执行后采纳门**之前**按下方专属恢复检查。

```bash
gene-agent inspect --session-id "$acceptance_session" --data-dir "$acceptance_data" --json \
  > "$acceptance_work/inspect.json"
if python -m app.diagnostics.route_acceptance \
  --run-dir "$acceptance_data/runs/$acceptance_session" > "$acceptance_work/acceptance.json"; then
  acceptance_status=0
else
  acceptance_status=$?
fi
printf 'Acceptance exit: %s\n' "$acceptance_status"
```

阅读 acceptance.json：退出 **0** 仅证明该冻结 RunConfig 的 occurrence 执行证据完整，生物学质量另行比较；**2** 表示未完成/失败/skip 缺口，检查 coverage_gaps/日志；**1** 表示必需证据不一致/缺失，须调查。RunConfig 不含 Review 时显式 `quality_evaluation_status: not_evaluated`。报告检查 Inventory 源、产物哈希、阶段组装依赖。保留完整运行目录，不仅 JSON。不要修改输入或冻结控制来恢复失败。

<a id="hi-c-prove-detached-resume-does-not-launch-autohic-again"></a>

### Hi-C：证明分离恢复不再次启动 AutoHiC

使用单阶段 hic。正常批准参数，等待 AutoHiC 完成后的 **gate_id: hic_scaffolding** 采纳门，不是 parameter_approval 或执行重试门。在决策选择提示处 Ctrl-C 或留空，持久待决门保留。不打断运行工具，也不选择终态 stop_human_review。Inspect 确认 blocked_human。

进程退出后保存只读执行后基线：

```bash
if python -m app.diagnostics.route_acceptance \
  --run-dir "$acceptance_data/runs/$acceptance_session" > "$acceptance_work/hic-before-resume.json"; then
  hic_before_status=0
else
  hic_before_status=$?
fi
printf 'Expected pending-gate exit 2; got %s\n' "$hic_before_status"
gene-agent inspect --session-id "$acceptance_session" --data-dir "$acceptance_data" \
  --write-decision-template "$acceptance_work/hic-decision.json"
```

基线退出 1 时先解决证据问题。只编辑模板决策字段：accept_candidate 采纳，keep_prior_assembly 成功保留；保留身份和绑定。request_parameter_retry 会新执行，不适用于此检查。然后：

```bash
gene-agent resume --session-id "$acceptance_session" --data-dir "$acceptance_data" \
  --decision-file "$acceptance_work/hic-decision.json" --checkpointer sqlite
python -m app.diagnostics.route_acceptance \
  --run-dir "$acceptance_data/runs/$acceptance_session" \
  --compare-hic-execution-from "$acceptance_work/hic-before-resume.json" \
  > "$acceptance_work/hic-after-resume.json"
```

预期退出 0 且 `resume_execution_check.status: unchanged`。要求同会话/冻结输入/采纳请求、恢复后完成、attempt-start/执行引用/输出哈希不变。拒绝空/执行前基线或新重试，不编辑基线。两分支需覆盖时用不同会话；完成决策不能靠第二次 resume 撤销。

<a id="base-polish-and-t2t-acceptance-points"></a>

### Base Polish 与 T2T 验收要点

模板对 GapCloser、T2T、Base Polish 显式使用 `recommendation_mode: advisor`、`max_attempts: 3`。每阶段先对源做 QUAST；每次执行后，源与全部历次候选在相同设置下重新比较，再进入 `finishing_recommendation` 门。执行前与执行后都是这个门，通过请求绑定区分轮次。需要单轮基线时显式设置 `deterministic_baseline`。

三个独立顾问切片都需要 QUAST 和可用的 LLM 端点；按 [envs.yaml.example](../examples/envs.yaml.example) 增加 `gapcloser.eval.quast`、`t2t.eval.quast`、`base_polish.eval.quast` 环境映射，可共用已有 QUAST 环境。Finishing/看校正质量的 Base Polish 还需最终 Review 的 BUSCO 数据库。

- `follow_recommendation`：查看 response.json 的 action。操作者确认后执行提案中的 `run`（执行新参数）、`accept`（采纳 selected_attempt）或 `keep_source`（保留本阶段原组装）；非零等待的 finishing 门不自动 follow；零等待会在首次执行前自动执行有效参数推荐。`human_review`、非法响应、重复参数、预算耗尽后建议 run，都不会开放 follow。
- `accept_candidate`：覆盖顾问建议，指定候选 `candidate_id: attempt_0` 等；可选回早先的完整候选。T2T 只有实际形成补丁的轮次可采纳。
- `keep_prior_assembly`：成功保留本阶段原组装；`stop_human_review`：终态失败。
- 零等待时 Finishing/Dedup 自动采用已有可用候选，否则执行有效推荐或沿用阶段输入；非零等待沿用既有评审策略。保持 `max_blocked_seconds: -1` 时不自动触发倒计时。T2T advisor 要求 `mode: auto_patch`，baseline 使用独立策略证据。

T2T 的 `deterministic_baseline` 不调用阈值模型或基序模型，要求显式提供 `species_facts.telomere_motif`，否则明确失败。基线使用冻结目录的 L0 阈值、空 L2 与 schema-3 策略请求与批准记录，经 `source=policy` 的已批准链执行单轮；不建立交互式 `parameter_approval` 门，补丁采纳仍经 `t2t_review` 持久门，零等待采用可选择补丁或无补丁时沿用输入，非零等待由人工处理。T2T advisor 只接受 `auto_patch`；`manual_review` 和 `propose_only` 要求显式选择 `deterministic_baseline`，不兼容组合在配置校验时拒绝。

零等待在一次尝试完成并收集评估后直接采用有效候选或沿用输入，不再调用结果顾问；预算耗尽也不调用模型。全部新尝试从相同源组装开始，不把上轮 polishing 结果继续校正。`work/steps/<step>/<stage>/attempts/NNN/` 隔离工具运行，`artifacts/steps/<step>/<stage>/advisor/` 保存设置、QUAST、请求/响应、assessment；approval 决定在 review 目录。顾问依据真实测量作方案选择，仍只能选择已实现的 TGS-GapCloser、端部补丁和 Racon 方法，不能提议未安装的新工具。没有参考的 QUAST 与同批读段一致性不构成独立准确率证明。

在测试机按以下矩阵收口，每个分支使用新会话：

| 范围 | 必须检查 |
|---|---|
| GapCloser | 有缺口真实 TGS；填补/结构报告进入下一次请求；不同参数第二轮；早先候选采纳；保留源；无缺口复用 |
| T2T | 源端部 motif 测量、读段扫描、过滤前每比对两端 MAPQ/重叠/距离及全部拒绝原因进入请求；支持数统计覆盖全部候选行；支持补丁与无延长；两轮隔离；早先补丁采纳时 final bundle 对应其证据；基线零模型调用 |
| Base Polish | Racon window/error/scoring 参数进入命令；源/候选 before/after PAF；同批数据限制明确；不同配置从同一源执行；早先候选采纳/保留源；最终 Review 的源/所选候选 QUAST/BUSCO |
| 共通 | 模型非法响应、重复配置、预算耗尽后继续 run 均转人工；执行后退出，再恢复 accept/keep 不新增生物工具启动；新 run 才增加 attempt |
| 串联 | 新 finishing 会话逐阶段核对 source 与前阶段交付一致；最后 Review 选择最终组装 |

恢复验证先在执行后顾问门保存 `route_acceptance` JSON 和 inspect，退出进程，生成/编辑决策模板，CLI resume 后再次保存审计 JSON。选择 accept/keep 后，同一阶段的 `attempt_starts`、已收集产物哈希应不变；选择 run 则应增加新的独立尝试。T2T 无支持延长、GapCloser 无缺口，以及顾问在执行前建议保留源都是合法结果，但审计退出 2 会如实表明对应工具执行分支未覆盖，不得据此宣布真实工具验收通过。

Base-polish/finishing：执行过 polishing 后，最终 Review 检查源与所选候选同设置的 QUAST/BUSCO。顾问未运行便保留源时，Review 只评估源，报告 `comparison_status: not_executed_source_retained`，该分支不证明 Racon 执行覆盖。多个候选时，验收脚本按最终 Review 实际绑定的 comparison_ref 核对，不要求全会话只有一份 comparison。

<a id="complete-hifihi-c-server-acceptance"></a>
<a id="complete-hifihic-server-acceptance"></a>

## 完整 HiFi/Hi-C 服务器验收

这是**最后一层**。先完成上述代码、小工具、小 contig、独立切片；模板存在不证明前置已通过。

复制[完整 manifest](../examples/hifi_complete_manifest.template.yaml)和[RunConfig](../examples/hifi_complete_run_config.template.json)到服务器本地。路线含 Base Polish 后最终 Review。编辑全部路径、样本身份、大小先验、物种，提供完整当前 HiFi 与明确 Hi-C R1/R2。不把 ONT/CLR/CycloneSEQ 重标 HiFi。

模板等待人工决策（`max_blocked_seconds: -1`）。只有真实样本和目标单倍型表示适合 purge 时，才把 `assembly_dedup.automatic_purge_approved` 设为 `true`，否则 Dedup 保留源，验收报告执行覆盖不完整。已知时在 `hic_scaffolding.autohic_parameters.restriction_enzyme` 中设置真实 Hi-C 酶，在 `shared_parameters.species_facts` 中设置实际倍性和端粒基序，否则用既有批准 resolver 门，不猜事实消除暂停。Assembly 仍阶段选择方法/参数；`assembly.case_source` 可指向服务器活动的 v5 导出案例目录。

按部署指南准备环境、AutoHiC、BUSCO/NCBI。Base Polish 增加 Racon 1.5.x 与 minimap2，GapCloser 需 TGS/GNU getopt。然后：

```bash
bash scripts/server_check.sh /server/path/run_config.json
gene-agent run --manifest /server/path/manifest.yaml \
  --run-config /server/path/run_config.json --data-dir /server/path/data
```

逐门评审。GapCloser、T2T 和 Base Polish 使用上述顾问循环。每个 Racon 候选均在相同设置的两次 minimap2 间执行一轮，执行后可采纳任意已完成候选或保留源。后续 Review 同设置评估源与所选 polishing 候选，由人工选最终。无声明参考时 QUAST 参考相关错误不可用；读段一致性是样本内指标，绝非独立 QV。

运行后只读验收：

```bash
python -m app.diagnostics.route_acceptance \
  --run-dir /server/path/data/runs/SESSION_ID > /server/path/acceptance.json
```

退出 0 仅证明冻结计划中适用的执行证据完整，生物学质量另行比较；2 表示未完成或明确缺口，含合法 skip（如无内部缺口），不能报告工具已运行；1 表示证据不一致/缺失。JSON 含各 occurrence 方法结果、验证执行/收集引用、候选/输出交接、最终组装（若适用）和成对质量指标（若最终 Review 绑定 Base Polish）。失败/阻塞进程需检查日志；开发主机缺二进制不是代码失败。

保留运行目录、冻结输入、索引产物、日志、请求绑定决策。新配置重试不要编辑冻结运行，应创建新会话。人工门中断使用下方普通 inspect/review/resume。

<a id="start"></a>

## 启动

从真实交互终端启动新运行：

```bash
gene-agent run \
  --manifest /server/path/manifest.yaml \
  --run-config /server/path/run_config.json \
  --data-dir /server/path/data
```

manifest 拥有 session_id，输出存于：

```text
/server/path/data/runs/<session_id>/
```

结构化运行全部阶段零等待时允许无 TTY；其他配置及 TUI 入口在冻结前要求 stdin/stdout TTY；occurrence 只有冻结授权允许才启动本地进程。全局 `review_policy.max_blocked_seconds` 应用于全部注册门（包括 Assembly 两门和 Review），`review_policy.stages` 仅提供可选的阶段覆盖。默认和倒计时精确语义归[评审契约](../architecture/reference/data-contract.md#12-review-contract)。门按冻结策略与注册 resolver 人工等待或使用合格附着倒计时。持久门中断后人工恢复，新进程不重建旧倒计时。

可选对话入口：

```bash
gene-agent tui --data-dir /server/path/data
```

TUI 与结构化 run 汇入同一冻结会话执行器。

<a id="inspect"></a>

## 检查

人类可读检查：

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data
```

规范完成后，`final_assembly` 是最新交付组装的验证绝对路径。JSON 还含不可变主输出引用 `primary_output_ref`、候选包、生产阶段和工作流 occurrence；完整性失败报告错误而不返回路径。

对一个或多个独立会话做机器可读轮询：

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --json
```

workflow_progress 来自验证冻结计划、规范 Ledger 终态、验证活动门。Checkpoint、执行标记、锁属于相邻 execution 诊断，不是进度真相。冻结输入无法验证时隐藏计划派生字段，不猜恢复权威。

仅需要时生成诊断会话摘要：

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --trace
```

trace 是报告生成，不是轮询协议。公共输出为 `reports/session_summary.json` 和 `reports/session_summary.md` 的**会话摘要**。冻结计划不可用时可从 occurrence Ledger/产物构建有限诊断视图，不授权控制/恢复。`write_final_review_report`、`latest_final_report` 等内部兼容名不定义第二种外部报告类型。

<a id="review-interactively"></a>

## 交互评审

持久人工门使用：

```bash
gene-agent review \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --checkpointer sqlite
```

命令验证当前请求，写决策证据，再应用恢复事务。

<a id="resume-from-a-decision-file"></a>

## 从决策文件恢复

先生成请求绑定模板：

```bash
gene-agent inspect \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --write-decision-template /server/path/decision.json
```

只编辑模板暴露的决策字段，再恢复：

```bash
gene-agent resume \
  --session-id <SESSION> \
  --data-dir /server/path/data \
  --decision-file /server/path/decision.json \
  --checkpointer sqlite
```

恢复在改状态前验证会话、occurrence/门绑定、不可变证据、决策结构和 checkpoint 可用性。

<a id="autohic-external-recovery"></a>

## AutoHiC 已有中间结果的独立恢复

这是一条外部运维入口，不是 `gene-agent resume`。只在已核对同一轮次 `.hic/.assembly` 的来源、原始 FASTA 与去重复 contacts 后使用。输入要求每个 fragment 恰好放置一次的无 gap 布局；带重复 `hic_gap` 放置的 `FINAL.assembly` 不适用。正式会话失败或 Ledger 截断时保留原运行目录，不编辑冻结证据或伪造完成；本工具不把外部结果登记为原尝试成功。完整正式流程及尚未实现的部分见[AutoHiC](../architecture/topics/autohic.md#resource-failure-and-external-recovery)。

先按[部署指南](../deployment/migration.md#autohic-gpu-environment)验证环境。从仓库根，以 AutoHiC 自身 Python 执行单轮检测/调整；示例参数和路径须按原冻结配置替换。`expected_span` 是核对上游 `get_hic_real_len` 后明确接受的 map 坐标范围，不直接等于 FASTA 总长。rawchrom 第一组可能很短，不能因此默认接受其余区域未被检测。

```bash
expected_span=447502881  # 示例：替换为已核验的官方推理范围
conda run --no-capture-output -n autohic_gpu python -m scripts.autohic.recover \
  --autohic-root /opt/AutoHiC \
  --hic /data/hic/contigs.0.hic --assembly /data/hic/contigs.0.assembly \
  --pair-provenance 'same producer round; cprops+asm and source sequence lengths verified' \
  --expected-inference-span-bp "$expected_span" \
  --output /data/recovery/pass-01 --device auto --threads 8 --min-free-gib 350 \
  --post-review-script /opt/3d-dna/run-asm-pipeline-post-review.sh \
  --reference /data/hic/contigs.fasta --contacts /data/hic/merged_nodups.txt
```

省略最后三项只生成检测证据和 `adjusted.assembly`；三项必须一起提供。可用 `--images /data/verified-images/png` 复用同一 map 的图片及 `info.txt`，索引必须使用仍有效的绝对图片路径。工具检查图片文件；各 resolution 的数量或坐标覆盖与官方窗口不匹配时仅提示，继续处理索引中的图片。覆盖检查不能自动证明图片内容来自给定 map，completed 也不证明图片覆盖完整。`--device auto|cpu|cuda` 控制设备，`--score`、`--iou-score`、`--min-length`、`--max-length` 和调整开关应与预期设置核对；debris 默认为关闭。`--threads` 仅控制图片生成并行数，post-review 使用上游自己的资源设置。`--min-free-gib` 只是预留值，须结合 contacts 大小、3D-DNA 文本放大和其他任务另行评估峰值。

查看输出的 `status.json`、`device_events.jsonl`、`inference_progress.json` 和 `post_review.log`；图片生成数量不等于已推理数量。completed 表示这次独立单轮操作通过校验，仍缺完整 AutoHiC 候选选择/迭代、染色体分配和 HTML。失败时保留部分输出，换新目录；已有目录拒绝覆盖，锁冲突拒绝第二个写入进程。不要重启完整 `autohic.py` 来续跑已完成的 Juicer/3D-DNA。

### 空间清理的依赖顺序

先用 `df -h` 看目标 Linux 文件系统空闲，用 `du` 定位运行目录，再检查 `lsof +L1` 中删除后仍被进程持有的文件。Linux 空闲增加而 Windows `ext4.vhdx` 文件不变是动态虚拟磁盘的正常行为；宿主压缩另安排维护窗口，活动任务期间不要关闭 WSL。

只有确认没有活动进程读写、下游已成功且保留了重新生成依据，才可清理对应旧尝试的中间文件：

| 中间文件 | 清理前必须确认 |
|---|---|
| `splits/*.sam`、`*_abnorm.sam`、`*_unmapped.sam`、对应 `*.sort.txt` | 比对、分类、排序、去重复成功；最终去重复 contacts 完整且可读取 |
| `aligned/merged_sort.txt`、`abnormal.sam`、`unmapped.sam`、`dups.txt` | `merged_nodups.txt` 已验证并保留；后续不再依赖分类/重复读段 |
| 旧轮次 `.mnd.txt`、排序临时文件、重复图片 | 对应 map/assembly 或本轮推理产物已成功验证，未来恢复不依赖这些文件 |
| 整个过时运行工作区 | 原始输入、最终 FASTA、同一版本配对 map/assembly、报告/日志/参数和必要恢复输入另有验证过的保留副本 |

`merged_nodups.txt`、原始 FASTQ/FASTA、仍需 post-review 的输入配对、冻结证据及 checkpoint/Ledger 都不是可无条件删除的缓存。本工具不自动删除任何文件。下次启动前做磁盘预算、并限制并行大型尝试；自动依赖清理和阶段恢复尚待正式执行器实现。

<a id="upgrade-safety"></a>

## 升级安全

当前预发布运行时没有 checkpoint 迁移。在活动会话检出上拉新代码前，先完成会话，或保留旧检出与匹配环境直至终态。报告不是 checkpoint，不能替代持久恢复状态。
