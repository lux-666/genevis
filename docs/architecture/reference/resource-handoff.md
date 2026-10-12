<a id="resource-handoff-contract"></a>

# 资源交接契约

> **状态：** 已接受；剩余跨切面缺口见[架构一致性](conformance.md)。
>
> **阅读场景：** 阶段发布或消费物化的生物学文件。
>
> **来源：** [数据与存储](../topics/data-and-storage.md)和阶段提交边界。
>
> **下游：** 下游阶段校验与方法冻结。
>
> **相关契约：** [阶段契约](stage-contracts.md)、[运行数据](data-contract.md)。
>
> **代码依据：** `app/stages/resource_specs.py`、`app/stages/resource_resolution.py`、`app/schemas/resource_ref.py`、`app/control/occurrence_settlement.py`。

本规范拥有跨阶段文件交接。生物学输入、输出和评审触发仍属于[阶段契约](stage-contracts.md)；本文定义物化资源如何在阶段间传递。

<a id="state-ownership"></a>

## 状态所有权

`TopLevelState.resource_refs` 是唯一跨阶段文件交接表。`stage_outputs` 包含阶段私有、可 checkpoint 的游标，可持有状态、指标、证据引用和恢复元数据。下游不得扫描其他阶段输出或猜键名寻找输入。它是当前 occurrence 的临时投影，不是历史表；再次进入同名阶段时，从冻结种子替换其槽位。既往 occurrence 历史保留在步骤范围的产物、评审记录、执行证据和 Ledger 事件中。

持久 `ResourceRef` 结构如下：

```json
{
  "resource_type": "assembly",
  "paths": ["/run/artifacts/steps/step-004-assembly/assembly/draft.fasta"],
  "source_stage": "assembly",
  "workflow_step_id": "step-004-assembly",
  "metadata": {
    "candidate_bundle_ref": {
      "schema_version": 2,
      "artifact_id": "art_...",
      "path_ref": "artifacts/steps/step-004-assembly/assembly/attempts/000/candidates/hifiasm_candidate.executed.json",
      "sha256": "...",
      "size_bytes": 1234,
      "artifact_type": "draft_assembly.candidate"
    },
    "selected_method": "assembly.candidate.hifiasm",
    "candidate_id": "session:assembly.candidate.hifiasm:parameter_attempt_0"
  }
}
```

当前约束：

- 表键必须匹配 `resource_type`。
- `paths` 至少含一个非空字符串。
- `source_stage is None` 当且仅当 `workflow_step_id is None`。
- 存在时，两者均为非空字符串。
- 普通 manifest/外部资源不带这两个来源字段；阶段资源记录规范生产阶段和精确 occurrence。已注册组装由 Inventory 有意包装为 executed candidate，因此中央 `assembly` 交接带 `source_stage="inventory"`、`workflow_step_id="step-000-inventory"`。
- 完整表仅含 JSON，最多 128 KiB。
- reducer 验证并替换完整表。
- 阶段节点返回增量，不修改输入状态。
- 大型证据、日志、命令、指标表位于产物或 Ledger；metadata 只放紧凑引用。

基因组大小等标量观测属于参数或测量证据，不是路径资源。

<a id="resolution"></a>

## 解析

所有阶段通过 `app.stages.resource_resolution.resolve_stage_resource` 按以下顺序解析：

1. `state.resource_refs[logical_resource]`。
2. 按 `ResourceSpec` 资产类型与优先级查询资产注册表。
3. `found=False`。

解析器没有独立显式运行覆盖层。阶段仍可在适配器边界消费运行配置字段，但那不是第四种跨阶段资源来源。

解析器不扫描 `stage_outputs`。资源缺失时，调用阶段决定失败、跳过或使用明确允许的后备；解析器本身只是缺失时不抛异常的查询。它不拥有环境/LLM 就绪、方法选择或工具预检，不得重读实时 `envs.yaml` 或从资源路径推断方法。检查属于[执行生命周期](execution-lifecycle.md#whole-run-readiness-boundary)与冻结所选尝试的适配器。

Inventory 同一逻辑资源同时含 clean/raw 资产时，由 `ResourceSpec.asset_type_priority_groups` 决定首选类型。后续阶段一旦发布中央资源引用，就不得静默回退到旧 Inventory 资产。

Trio 母本和父本文件是例外的注册范围而非中央资源：Inventory 保留它们在
asset registry 中，供已冻结且带有 `metadata.intake_role` 的显式 Trio 输入引用
使用，但不会发布到中央 `long_reads` 或 `short_reads`。因此中央资源缺失时的
registry fallback 也必须过滤 `trio_maternal_reads` / `trio_paternal_reads`；
Genome Survey 和 Read QC 不得把父母读段当作子代读段。

阶段使用 `update_resource_ref(...)` 发布；它返回新验证表，不修改传入状态。

<a id="logical-resources"></a>

## 逻辑资源

| 阶段 | 必需 | 产生或更新 |
|---|---|---|
| `inventory` | — | 按 manifest 注册 `long_reads`、`short_reads`、`hic_reads`、`assembly` |
| `read_qc` | `long_reads`（可选）、`short_reads`（可选）、`hic_reads`（可选）；至少一族可用 | `long_reads`、`short_reads`、`hic_reads`（按选中方法） |
| `genome_survey` | 完整 Survey 至少需要 `short_reads` 或 `long_reads`，优先短读段族；族内 clean/raw 优先级在阶段前解析；明确基因组大小允许无读段 skip | 仅标量测量证据 |
| `assembly` | `long_reads` | `assembly` |
| `assembly_dedup` | `assembly`；`read_depth` 策略还需 `long_reads` | `assembly` |
| `assembly_review` | `assembly` | `assembly` |
| `hic_scaffolding` | `assembly`、`hic_reads` | 已采纳染色体级 `assembly` |
| `gapcloser` | 当前采纳 `assembly`；存在内部缺口时需完整 HiFi-only `long_reads` | 人工采纳或复用的 `assembly` |
| `t2t` | 当前采纳 `assembly`、`long_reads` | `assembly` |
| `base_polish` | 当前 `assembly`、完整 HiFi-only `long_reads` | 人工采纳或复用的 `assembly`，以及供最终 Review 的成对替代候选 |

运行时适配器强制这些直接依赖。Genome Survey 已允许只有短读段的运行，两族均存在时优先短 DNA 读段。`resource_specs.py` 仅拥有规范资源解析；阶段不共享第二份声明式规划表。

冻结前与读取冻结输入时，`frozen_inputs.py` 使用同一检查验证 manifest 与字面阶段顺序：组装消费阶段必须有已登记组装或前置本地 Assembly；后置 Assembly 不能满足前面的输入。未授权本地执行的 Assembly 也不能被当作交付者。QC 至少有一个可用读段族，选中的 fastp 和 Hi-C scaffolding 要有显式配对输入；Trio 父母读段不计入子代输入。Assembly 所需基因组大小来自 manifest 或此前配置完整的本地 Survey。没有读段但有显式大小的 Survey 仍可按原契约跳过。

进入 Dedup、Review、Hi-C 或 finishing occurrence 前，控制器从冻结顺序推导最近组装交接步骤，核对中央 `ResourceRef` 的生产阶段与 `workflow_step_id`。它与 Review 的 `review_target` 使用同一顺序分析，不新增计划字段或路线列表。来源缺失或不匹配即停止，不能静默使用更早的组装；Review 继续执行下述 Ledger 交付证明校验。QC 清洗结果仍按实际交付更新资源，不把每个计划中的 QC 当作必定产出清洗读段。文件内容、方法适用性、条件执行和人工采纳仍由阶段内检查决定。

Hi-C 读段止于 `hic_scaffolding`。人工采纳后，该 occurrence 替换中央 `assembly`。GapCloser 消费该组装和完整当前 HiFi 集合；采纳则替换，保留旧组装或没有内部缺口则声明复用交付。后续 T2T 解析当时的组装和长读段，不直接消费 `hic_reads` 或 contact map。Base Polish 同样使用当前组装和 HiFi 集合。交接 metadata 携带 `comparison_candidate_bundle_ref`、`comparison_primary_output_ref`、`polish_comparison_ref`。最终 Review 依据不可变来源绑定比较验证两个候选，再用相同 QUAST/BUSCO 设置评估。这些引用描述替代项，不构成第二个当前组装。

候选包、评审决策、执行证据、报告和指标仍是产物。资源 metadata 可引用它们，但不形成第二跨阶段文件表。

<a id="immutable-assembly-delivery"></a>

## 不可变组装交付

`resource_refs["assembly"]` 是可替换的当前路由投影。其 `source_stage` 和 `workflow_step_id` 是分发必需标签，但不是指定 occurrence 已交付候选关系的不可变证明。证明由共享结算边界在 `stage_completed` 前写入的一条 occurrence 绑定 `resource_delivered` Ledger 事实提供。

当前 v1 交付 schema 有意专用于组装：

```json
{
  "event_type": "resource_delivered",
  "stage": "assembly_dedup",
  "workflow_step_id": "step-004-assembly_dedup",
  "subject_id": "step-004-assembly_dedup:assembly",
  "summary": {
    "resource_type": "assembly",
    "delivery_mode": "reused"
  },
  "refs": [
    {"role": "candidate_bundle_ref", "artifact_id": "..."},
    {"role": "primary_output_ref", "artifact_id": "..."}
  ]
}
```

`delivery_mode="materialized"` 表示 occurrence 创建新的 executed candidate 关系；`"reused"` 表示有意原样交付旧的已验证候选。复用不是无交付：Dedup 透传、Hi-C/T2T 保留和 Review 不改变结果的决策都拥有新的交付事实，指向旧候选与主输出。因此产物索引的物理生产者可早于交付 occurrence。

一个完成 occurrence 最多交付一次 `assembly`。相同事件精确回放幂等；更改阶段/步骤绑定、交付模式、候选引用或主引用产生 Ledger 冲突。声明组装交接的完成 occurrence 必须同时具有交付事实和当前 `ResourceRef`，路径与 metadata 投影相同主输出与候选。没有匹配完成 occurrence 的交付事件是未完成崩溃前缀，不可消费。

Assembly Review 加载候选前验证：

1. 冻结 `review_target.producer_step_id` 指向预期的早先生产 occurrence。
2. 该 occurrence 具有规范 `stage_completed` 终态。
3. `assembly` 恰好有一个匹配的 `resource_delivered` 事实。
4. 当前 `ResourceRef` 的阶段/步骤标签、候选引用、主引用、路径与交付一致。
5. executed candidate 包及其嵌入主输出通过验证。

Review 只接受绑定 occurrence 的已验证交付事实，重贴可变 `ResourceRef` 标签不能满足要求。

操作者 `final_assembly` 是交付事实的分离读取模型，不是另一份资源记录。终态范围限制与产物验证由[运行数据契约](data-contract.md#13-ledger-v2)拥有。

<a id="current-assembly-candidate-lineage"></a>

## 当前组装候选谱系

每个表示 executed candidate 的中央 `assembly` 资源（包括 Inventory 包装的已注册组装）都携带当前执行候选包和精确主输出引用。Assembly Dedup、采纳的 Hi-C/GapCloser/Base Polish、采纳的 T2T 补丁或 Assembly Review 校正行动改变组装路径时，在当前 occurrence 发布新执行候选。原当前候选和主输出保留为 `input_candidate_bundle_ref`、`input_primary_output_ref` 谱系 metadata。

通用候选包装严格接受这些主产物类型：`assembly.draft_assembly`、`hic_scaffolding.chromosome_scaffolded_assembly`、`gapcloser.assembly`、`base_polish.assembly`、`t2t.patched_assembly`。包装本身仍为 `draft_assembly.candidate`；派生 FASTA 不复制为仅 draft 类型。各生产者之后 Assembly Review 都消费同一当前候选契约，没有生产者专属后备。Hi-C/GapCloser/T2T/Base Polish 不改变组装的决策保留路径与候选关系，但在当前生产 occurrence 下重新发布中央 ResourceRef，使后续 Review 能验证精确交付。

<a id="read-qc-handoff-lineage"></a>

## Read QC 交接谱系

Read QC 谱系不创建第二读段资源协议。未过滤完成时，重新发布或保留 `long_reads`，携带实际测量的精确有序资产 ID、资产注册表引用、成员平台/格式和路径。长度过滤成功后，发布一个不可变读段集合 manifest，将每个派生成员映射到源资产 ID、源输入引用、过滤方法和批准参数。中央 `long_reads` `ResourceRef` 随后包含有序过滤路径、manifest 引用及源成员身份。

Read QC 发布中央 `long_reads` 后，下游不得回退到旧 raw 资产集。过滤后零读段仍发布方法证据与零值汇总统计，但不以空文件替换中央资源；Read QC occurrence 以 `no_reads_retained` 交付失败。

FOFN、逐文件统计和摘要报告属于产物或适配器局部文件，不是并列交接机制。精确测量来源由[阶段契约](stage-contracts.md)的 Read QC 节拥有。

### paired short-DNA 与 Hi-C fastp

`read_qc.short_reads.fastp` 和 `read_qc.hic_reads.fastp` 使用同一个严格的
paired-member 契约。Inventory 的 raw `short_reads`/`hic_reads` 交接只需提供有序
`asset_ids`、注册表引用以及注册表内的 pair/mate provenance；它不要求复制
`paired_members`。只有 fastp 派生资源必须同时提供 `read_set_manifest_ref` 和有序
`paired_members`；每个 pair 具有非负
`manifest_entry_ordinal`、`pair_index`，并且恰好有 `mate=R1` 与 `mate=R2`。
R1/R2 不能共享 `source_asset_id`，不得通过文件名推断配对。非对象 lane、缺失
mate、重复 lane 或 pair-index gap 都是输入错误，不是可静默丢弃的记录。

fastp 的资源 `paths` 和 paired members 指向发布后的 clean artifact；原始资产
只作为 `source_asset_id` 谱系锚点。Hi-C/Assembly 消费者通过
`resolve_hic_pairs` 读取 paired members，因此即使 `selection_anchor` 指向原始
资产路径，也只选择对应 pair group，返回的 R1/R2 仍是 clean 后代及其
`ArtifactRef`。这保证 AutoHiC/Hifiasm 不会因 registry 回查而重新使用 raw reads。

一个只含 short-DNA paired reads 的运行是合法的：不需要 long-read 资源，也不
会凭空创建 `long_reads` 交接。无选中 fastp 方法的族不产生对应 resource ref。

<a id="sequencing-member-identity"></a>

## 测序成员身份

Inventory 独立注册每个声明文件。两个 `raw_long_reads` 条目或一个条目内的两个文件，即使 basename 相同，也保留独立不透明资产 ID。平台与格式是逐成员注册事实；`asset_type` 不是身份键。

Inventory 生成的 `long_reads`、`hic_reads` metadata 包含类型化资产注册表 `ArtifactRef`、与 `paths` 一一对应的有序资产 ID、成员/读对数、去重平台/格式集合，不嵌入资产记录。消费者通过共享注册表/读段集合解析器重建并验证成员。

Hi-C 读对权威是 manifest 声明投影的 `manifest_entry_ordinal`、`pair_index`、`mate` 事实。文件名和扁平路径位置永不创建读对。

<a id="assembly-input-roles-and-product-handoff"></a>

## Assembly 输入角色与产品交接

Assembly 按平台划分完整有序长读段成员集。HiFi Hifiasm 按位置绑定全部选定 HiFi 成员，可选 ONT ultra-long 合并为一个逗号连接 `--ul`，全部声明 Hi-C lane 合并到逗号连接的 `--h1`/`--h2`。独立 Hifiasm-ONT 方法将 ONT 作为主位置输入并固定 `--ont`，不复用 `--ul`。

只物化一个组装器来源主干。executed candidate 包引用不可变计划、规范主 FASTA 和通用适配器声明的原生输出。原生图、替代 contig、报告、日志或配置保留在包内，不变为并列组装候选或下游组装路径。

Assembly checkpoint 持有单数类型化 `planned_candidate_bundle_ref`、`executed_candidate_bundle_ref`。复数和绝对路径候选游标字段被拒绝。

<a id="fofn-boundary"></a>

## FOFN 边界

规范长读段交接为 `resource_refs["long_reads"].paths: list[str]`。

FOFN 是工具适配器或阶段内部产物：

- Read QC 可为审计或工具调用生成 FOFN。
- NextDenovo 等适配器可从规范路径列表在自身工作目录创建所需 FOFN。
- 下游不消费 `*_fofn_ref`、`assembly_input_ref` 或其他阶段输出交接键。
