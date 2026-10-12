<a id="architecture-conformance"></a>

# 架构一致性

本页记录已接受架构与当前工作树之间的已知差异。它不是兼容性契约，也不削弱负责该主题的参考规范。

> **状态：** 一致性欠账
>
> **阅读场景：** 设计声明与当前代码看似不一致时。
>
> **来源：** 架构审查与仓库验证。
>
> **下游：** 关闭所列缺口的范围明确的实现工作。
>
> **相关主题：** [数据与存储](../topics/data-and-storage.md)、[执行](../topics/execution.md)、[运行时编排](../topics/runtime-orchestration.md)、[Bootstrap](../topics/bootstrap.md)。
>
> **代码依据：** 每一项均列出现有证据路径。

<a id="open-gaps"></a>

## 未关闭缺口

截至 2026-10-09，操作者确认本轮 finishing smoke 已完成；GapCloser、T2T、Base Polish 的独立阶段验收和串联验收均尚未执行，暂缓推进。本记录未附运行目录、工具版本或 smoke 日志，不将 smoke 扩大解释为这三个阶段的完整覆盖。Assembly 各方法及 Dedup 的代码覆盖与待验边界见[实现地图](../langgraph-implementation-map.md#assembly-dedup-validation-map)。新菌种实测与测试机操作仍未启动；操作者已有 PacBio、Illumina、Hi-C 数据，PacBio 的具体类型须在执行时确认。

<a id="gapcloser-representative-sample-acceptance-and-noisy-read-routes"></a>

### GapCloser 代表性样本验收与含噪读段路线

已实现的 TGS-GapCloser 切片只接受明确分类的 HiFi 读段，校验原生产物，并要求持久人工采纳。测试使用 fixture 可执行文件覆盖共享生命周期，并提供可选真实二进制合成缺口检查；两者都不是代表性生物样本，也不能证明共识准确性改善。没有声明 ONT/CLR/CycloneSEQ 校正路线。Base Polish 在下方有独立 HiFi-only 契约。

- 当前证据：`app/stages/gapcloser/`、`tests/test_gapcloser.py`。
- 契约所有者：[GapCloser](stage-contracts.md#8-gapcloser)。
- 关闭条件：在已配置主机上运行代表性 scaffold/HiFi 数据集，检查 gap-detail 报告，独立评估质量，并记录人工采纳。其他平台须另行接受一条路线。

<a id="base-polish-representative-sample-acceptance-and-independent-accuracy"></a>

### Base Polish 代表性样本验收与独立准确性

仅 HiFi 的 minimap2/Racon 路线、成对前后比对证据、持久人工采纳/源保留以及最终成对 QUAST/BUSCO 评估已实现。fixture 可执行测试证明进程契约，不证明生物学正确性。同一批用于校正的读段不能建立独立共识 QV。

- 当前证据：`app/stages/base_polish/`、`tests/test_base_polish.py`、`app/diagnostics/route_acceptance.py`。
- 契约所有者：[Base Polish](stage-contracts.md#10-base-polish)。
- 关闭条件：先运行真实 Racon/minimap2 冒烟和从已有组装开始的独立 Base Polish 切片，比较两个候选并记录采纳或保留；底层通过后，再按[验收顺序](../../user_guide/run-and-recovery.md#acceptance-order-code-tests-before-real-end-to-end-runs)执行完整路线。若无独立真值数据支持，独立准确性仍未测量。开发主机缺少工具是部署前置条件，不是方法代码缺陷。

<a id="assembly-software-explanation-coverage"></a>

### Assembly 软件解释覆盖

单一 Assembly 消费者、跨方法检索和冻结的方法/参数基础已实现。来源设置独立保留，不受目标 L2 键影响。解释来自可执行目录及 hifiasm、Flye、NextDenovo 的机制指导；新增指导引用官方说明且在目标方法参数请求中冻结。尚未覆盖全部外部/历史设置或版本交互；无法解释的设置保留为未知。

- 当前证据：`app/domains/assembly/software_knowledge.py`、`method_guidance.py`、`app/knowledge/assembly_retrieval.py`。
- 契约所有者：[Assembly 案例消费](casepack.md)。
- 关闭条件：为真实案例库中遇到的设置补充经引用的机制/版本指导，超出当前目录的覆盖范围。这是内容覆盖，不是第二消费者，也不要求将外部设置翻译成可执行参数。Hi-C/T2T 工作流及推荐器已实现；待补的是对应案例与文献方法知识覆盖。

<a id="assembly-corpus-handoff-and-measured-recommendation-coverage"></a>

### Assembly 案例交接与已测推荐覆盖

部分读段观测仍缺少处理状态绑定。它们仍可作为上下文读取，但不能与运行时 raw/filtered 观测做数值匹配。不要通过把 unknown 当通配符或从标签推断状态来关闭缺口。跨方法 NextDenovo 测试使用合成 fixture，证明接线与原生设置保留，不证明真实案例库覆盖或生物学收益。

- 当前证据：`app/knowledge/assembly_cases.py`、`tests/test_assembly_case_consumption.py` 及 Git 忽略的本地 `descriptors/knowledge_packs/assembly/cases/`。
- 契约所有者：[Assembly 案例消费](casepack.md)。
- 关闭条件：为已交付案例补齐经来源核验的状态绑定，验证软件包和实际外部工具检索在方法与参数请求中的表现，再将推荐结果与软件测试分开测量。案例库维护以案例文件本身为准。

<a id="dedup-advisor-real-tool-acceptance"></a>

### Dedup 顾问真实工具验收

结构化推荐和有界 purge_dups/QUAST 评审循环已实现。聚焦图测试覆盖三次 purge 尝试、四次累积 QUAST 评估/模型请求、响应恢复与评审期间早先文件不变、冻结原始草稿输入，以及采纳首个候选。父图 fixture 测试另覆盖 read_depth/self_alignment 两条路径的 SQLite 关闭重开、CLI 恢复、两轮 purge 后采纳早先候选或保留源；最终选择不重复执行工具。外部注册组装可绑定本次长读段为深度比对证据，不能将这些读段推断为原始组装来源。fixture 检查不能证明真实工具的生物学质量，也不能证明服务器分离进程验收。

- 当前证据：`app/stages/assembly_dedup/`、`tests/test_dedup_recommendation_loop.py`、`tests/test_assembly_dedup_parent_recovery.py`。
- 契约所有者：[Assembly Dedup](stage-contracts.md#5-assembly-dedup)。
- 关闭条件：使用真实工具验证隔离阶段，包括分离进程恢复，并在声称代表性样本验收前保留验收证据。

<a id="five-stage-real-tool-contig-acceptance-is-not-automated"></a>

### 五阶段真实工具 contig 验收尚未自动化

五阶段原始读段 contig 路径已有聚焦单元和集成覆盖，包括整图生命周期测试，但尚无一次自动验收运行在代表性输入上通过规范完成流程调用真实 SeqKit、Jellyfish、GenomeScope2、所选组装器、purge_dups、QUAST、BUSCO 和配置的 LLM 端点。多数方法测试使用 fixture、stub 或修补外部边界隔离阶段。现有真实工具 Read QC 冒烟不能关闭整条路径缺口。

操作者报告完成了一次真实五阶段运行。请用 `route_acceptance --route contig` 复用并审计其保留证据；本检出中没有捆绑自动数据集/测试框架，不等于服务器执行未发生。同一诊断现在也支持独立的 Review、Hi-C、GapCloser、T2T、Base Polish + Review 和 finishing 范围，并提供已发布 RunConfig/输入模板。这些条目不能证明对应服务器真实工具运行已通过。

`server_check.sh` 证明固定主机工具/包/数据库闭包，`server_test.sh` 是回归入口，不是全流程生物学执行证据。普通回归排除显式标记的真实工具检查（包括 SeqKit 和 hifiasm CLI 验证），且测试收集期间不探测工具。这两个脚本通过都不能证明 contig 运行成功。

- 当前证据：`tests/test_read_qc.py`、`tests/test_genome_survey.py`、`tests/test_assembly.py`、`tests/test_assembly_dedup.py`、`tests/test_assembly_review.py`、`scripts/server_check.sh`、`scripts/server_test.sh`、`app/diagnostics/route_acceptance.py`、`tests/test_route_acceptance.py`。
- 契约所有者：[阶段契约](stage-contracts.md)与[部署边界](../../deployment/README.md)。
- 关闭条件：审计保留的真实运行证据，补齐已证明的代码测试缺口并隔离真实工具测试。仅为尚未证明的方法/分支增加小数据真实工具覆盖；使用规范模板和 `gene-agent run`，不要建立第二执行框架。可复现服务器验收应保留可追溯输入/工具身份，并通过分离检查得到规范完成终态、五个已结算分析 occurrence 和哈希验证的 `final_assembly`。聚焦 stub 测试不满足条件。Hi-C 与 finishing 阶段在全流程验收前先作为独立切片；单一操作者顺序由[运行与恢复](../../user_guide/run-and-recovery.md#acceptance-order-code-tests-before-real-end-to-end-runs)拥有。

<a id="update-rule"></a>

## 更新规则

每条记录都要标明接受的所有者、当前代码证据和客观关闭条件。关闭缺口的同一变更中删除对应条目。
