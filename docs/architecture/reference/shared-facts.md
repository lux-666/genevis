<a id="shared-biological-facts-contract"></a>

# 共享生物学事实契约

> **状态：** 已实现。
>
> **阅读场景：** 修改跨阶段生物学事实、规划器事实采集、基因组大小解析或共享参数冲突规则。
>
> **来源：** 操作者 RunConfig、TUI 规划或冻结输入 manifest。
>
> **下游：** [阶段契约](stage-contracts.md)、[运行数据](data-contract.md)、[资源交接](resource-handoff.md)、[TUI 与规划](../topics/tui-and-planning.md)。
>
> **代码依据：** `app/schemas/shared_parameters.py`、`app/schemas/run_config.py`、`app/domains/genome_size.py`、`app/entrypoints/ask_planner.py`、`app/entrypoints/tui_intake.py`、`app/stages/inventory/nodes.py`、`app/stages/assembly/nodes.py`、`app/stages/assembly_review/nodes.py`。

<a id="definition"></a>

## 定义

`SharedParameters` 拥有投影到运行时状态的跨阶段生物学事实。Inventory 一次性协调 manifest 拥有的目标物种和基因组大小先验；RunConfig 可补充其他事实，但不得与 manifest 身份冲突。阶段输出可添加派生测量，不能覆盖冻结事实。

schema 拒绝未知字段。所有字段可选；阶段需要的事实缺失时，必须在自身边界拒绝，不能编造默认值。

```json
{
  "platform": "ont",
  "read_type": "ont_raw",
  "estimated_genome_size": 500000000,
  "species_facts": {
    "scientific_name": "Homo sapiens",
    "taxon_id": 9606,
    "clade": "mammal",
    "ploidy": 2,
    "expected_chromosome_count": 23,
    "telomere_motif": "TTAGGG"
  }
}
```

<a id="field-semantics"></a>

## 字段语义

### `platform`

manifest schema 中的单一测序平台标识，是分类提示，不是方法选择。仅注册一个平台时可推导 `read_type`；混合或未知平台时须显式提供，或延迟到运行时推荐门禁。

### `read_type`

Assembly 选择域中的规范读段类型，约束兼容方法，是延迟路线选择器的主要输入。冻结值不得被平台推断静默覆盖；冲突属于校验错误。

### `estimated_genome_size`

单位为碱基对的正整数，表示操作者先验，不是测量。已知大小时可跳过 Survey，或在缺乏 Survey 读段时提供备用来源。

解析优先级确定且单轨：

1. 运行时协调投影出的 `shared_parameters.estimated_genome_size`。
2. manifest 投影出的 `inventory.genome_size_estimate`。
3. `genome_size_source == "survey"` 时的 `genome_survey.genome_size_prior`。

当前 `RunConfigSharedParameters` 有意不含此字段。来源 1 是运行时投影，不是冻结文件输入。较早来源优先，较晚来源不得覆盖。Genome Survey 需要大小时，若既无明确值也无有效 Survey 估计，以 `GENOME_SIZE_UNAVAILABLE` 失败。Assembly 候选规划时，若方法需要大小而尚未解析出先验，则以 `genome_size_required` 拒绝该候选。

### `species_facts`

用于方法和参数推荐的生物学身份。规范学名属于 `input_manifest.user_metadata.target_species`，请求 Assembly Review 时必需，且必须是 NCBI 拉丁学名；不存在俗名或 LLM 解析路径。Inventory 将冻结学名投影为 `species_facts.scientific_name`；RunConfig 副本冲突时在执行前失败。若 RunConfig 带学名副本，它只是相等性断言，不是公共物种输入入口；模板和 TUI 不生成该副本。其他物种事实无需该副本也有效，例如 RunConfig 只提供 `species_facts.ploidy`，Inventory 在协调时补充 manifest 拥有的学名。`telomere_motif` 规范为大写 ACGT，至少两个碱基。

<a id="planning-and-freeze-validation"></a>

## 规划与冻结校验

规划器不询问 `estimated_genome_size`。读段类型和基因组大小是不同条件：单一已注册平台可推导一个共享 `read_type`；混合或部分未知平台保留为 Assembly 输入集合，延迟到方法推荐门禁。

延迟的本地 Assembly 在构造 RunConfig 前，还必须具有以下基因组大小来源之一：

- `input_manifest.genome_size_estimate` 提供明确操作者先验；或
- 每个本地 Assembly occurrence 前都有已授权本地执行、具备可用 Survey 读段的 Genome Survey occurrence。

缺乏大小来源时，计划编译校验失败；读段类型不能替代大小来源。提交运行前还会针对规范冻结输入重验同一生物学规则。

存在前置 Survey 时，规划以 `awaiting_genome_survey` 表示该事实，不将它作为可变草稿值采集。

<a id="runtime-reconciliation"></a>

## 运行时协调

Inventory 协调冻结 RunConfig 事实与运行时投影：

- 运行时值与已配置 RunConfig 共享事实冲突即报错。
- manifest `genome_size_estimate` 与运行时 `estimated_genome_size` 冲突即报错。
- RunConfig 共享事实与 manifest 值都存在且相等时，以已配置值为准。

协调后，`shared_parameters` 在剩余运行中不可变。阶段输出可增加派生事实（例如 Survey 测量），不能修改此块。

<a id="conflict-matrix"></a>

## 冲突矩阵

| 来源 | 优先级 | 冲突行为 |
|---|---|---|
| 已配置共享事实 | 1（最高） | 权威值，运行时必须一致 |
| Manifest | 2 | 补充缺失值；与运行时冲突时报错 |
| Genome Survey | 3（最低） | 仅在没有明确值时使用 |

<a id="related-contracts"></a>

## 相关契约

- Genome Survey 输入和跳过语义：[阶段契约](stage-contracts.md)。
- 标量观测证据边界：[资源交接](resource-handoff.md)。
- 延迟 Assembly 方法绑定：[运行数据](data-contract.md)。
