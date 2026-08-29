# Case 知识库 V1：设计与实施计划

状态：已确认，Phase 0 实施中
实施位置：`main` working tree
实施原则：保留现有严格校验、curation 门禁、comparison-group 检查、确定性检索与 trace；V1 只建立一套 case 表示和一条运行时链路。

## 1. V1 边界

一个 case 表示某个样本在一个 stage 中实际执行的一条独立技术路线：

```text
Case
├── source              paper / supplement / script
├── stage               assembly / hic_scaffolding / t2t
├── context             当时的数据与基因组条件
├── action              method + parameters
├── outcome             该 stage 直接产生的结果
├── comparison_group?   同条件横向比较的多个独立 case
└── rationale?          可选原文理由
```

同一论文、同一样本、同一 stage 的不同路线分别建 case。case 不表示整篇论文，不把 method、parameter、strategy 拆成额外一级实体。

V1 不做：DB/embedding、Workflow/Decision/case_type、evidence strength、统一胜负分、taxonomy 相似度、near-T2T objective。

## 2. 存储布局与标识

```text
descriptors/knowledge_packs/stage_cases/
├── manifest.yaml
├── sources/<paper_id>/
│   ├── paper.md
│   ├── supplement.md
│   └── scripts/...
├── cases/
│   ├── assembly/<case_id>.yaml
│   ├── hic_scaffolding/<case_id>.yaml
│   └── t2t/<case_id>.yaml
└── templates/
    ├── assembly.yaml
    ├── hic_scaffolding.yaml
    └── t2t.yaml
```

- 证据文本按 paper 存一份，供不同 stage 的 case 共用；仓库只保存核对过的逐字文本摘录，不保存 PDF。
- `source.documents[].path` 必须位于该 case 的 `sources/<paper_id>/` 内。
- `case_id` 为 `<paper_id>.<sample_id>.<stage>.<arm_slug>`，文件名必须等于 `<case_id>.yaml`。
- 每个 ID 分段只允许 `[a-z0-9][a-z0-9_-]*`，分段内部禁止 `.`；DOI 单独保存，不进入 `case_id`。
- loader 机械互检 ID、`source.paper_id`、`source.sample_id`、`stage` 与所在目录。

## 3. Schema 与校验

### 3.1 Typed stage models

`assembly`、`hic_scaffolding`、`t2t` 各自使用严格的 context/outcome Pydantic 模型，不保留通用 fact-tree 或开放式业务字段。

单位只保存 canonical 值：

- `genome_size`、`assembly_size`、`*_n50`：`bp`
- `coverage`：`x`
- `heterozygosity`、`repeat_fraction`、`busco_*`、`anchored_fraction`：`fraction`，范围 `0–1`
- `qv`：`phred`
- `*_count`、`*_gaps`、`*_ends`：`count`

百分数和 Mb/Gb 不直接入库；换算后的值附 `derivation`，例如 `1.05 Gb -> 1050000000 bp`。`platform`、`data_type`、`subtype`、`role` 使用闭合枚举。

输入数据的唯一配对键为：

```text
(data_type, platform, subtype, role)
```

同一 case 内不得重复，HiFi 与 ONT 不会在展平时覆盖彼此。

`scientific_name` 在 ingest/curation 时规范化并持久化。loader 只校验格式，不在运行时依赖 species alias 文件。V1 不计算分类学距离。

`runtime_method_id` 可选；非空时只校验其存在且 stage 一致。历史工具和历史参数不因当前运行时 registry 缺失而被拒绝。

### 3.2 Source spans 与 curation

方法、参数和数值通过 span 指向逐字摘录。每个已声明 span 都必须满足：文件存在、路径确界、quote 是源文本的精确子串。

- `draft`：允许缺少 outcome、curator 和 `reviewed_at`；已填写的事实仍必须通过完整的来源校验。
- `approved`：必须有 curator、`reviewed_at`，并至少包含一个该 stage 的直接 outcome 指标。
- 每个 approved 数值都必须有 source span；发生单位换算时还必须有 `derivation`。
- 只有摘要级来源的旧材料只能形成 draft，不能 approved。
- `rationale` 仅为可选自由文本和 `source_span_ids`，不建立 claim 或 evidence-strength 结构。

同一 `comparison_group` 的 case 必须：

- stage、sample 和可比较 context 一致；一致性比较忽略 provenance 字段；
- curation status 一致；
- arm 数量不超过固定上限。

### 3.3 单次校验边界

`load_case_library()` 完成 schema、ID、词表、单位、路径、quote、group 和 registry 的一次性全量校验，并返回 loader 拥有的不可变 `CaseLibrary`。

`select_cases()` 只接受 `CaseLibrary` 和查询对象，只做内存检索；不接受目录路径，不读取源文件，不重复校验。调用方不能通过修改普通 list/dict 绕过 loader 门禁。

CI contract test 直接校验仓库真实库。draft 只豁免其允许缺失的字段，不豁免已经声明的事实或 span。

## 4. 检索与 prompt 投影

### 4.1 Context 匹配

检索先按 `stage` 和 `status: approved` 过滤。参数推荐若已经确定 method，再按 `runtime_method_id` 或规范化 method 精确过滤。

- 输入按 `(data_type, platform, subtype, role)` 配对。
- 缺失字段不参与分数，也不惩罚。
- 类别字段不匹配算作一次比较，贡献为 `0`。
- 两个 case 没有任何可比较字段时，该候选不合格，不产生零分命中。
- V1 对实际参与比较的字段做等权平均；权重留在代码中，不写入数据。
- trace 记录每个字段是否参与、匹配方式、贡献和排除原因。

运行时 query adapter 只使用已有 canonical facts：主读长数据可带数值；伴随数据只根据 asset 表示 available，不推测不存在的 platform-specific 数值。

### 4.2 Comparison group

comparison group 是检索单位，而不是 top-k 后的无界扩张：

1. 独立 case 组成单元素 unit，同组 arms 组成一个 group unit。
2. group score 取组内最高 arm score。
3. 对 units 做确定性排序并选择 top-k。
4. 命中 group 后投影全部 arms，并在 prompt 中相邻呈现。

检索同时限制单组最大 arm 数与整个 selection 的字符预算，避免一个组占满 prompt。

### 4.3 Prompt

prompt 只投影：`context + action + outcome + 实际引用的 quote 原文 + trace`。method recommendation 使用 action.method；parameter recommendation 在 method 已知时使用 action.parameters；二者共享 case 库，不建立两套 schema。

## 5. 提取流水线

1. **Ingest**：PDF、补充材料、作者脚本转为 `sources/<paper_id>/` 下的逐字摘录；表格转 Markdown 行并人工核对。
2. **Draft**：按 stage 模板建立独立 case；无法引用的字段不写，禁止 `null` 占位和推测；换算记录 derivation。
3. **Lint**：机器校验 schema、ID、词表、单位、路径、quote、comparison group 和 registry 关联。
4. **Curate**：人工复核后填写 curator、`reviewed_at` 并改为 approved；没有直接 outcome 的 case 保持 draft。

## 6. 实施阶段

每个阶段一个 commit；运行时切换与旧实现删除分成两个 commit。

### Phase 0：建立隔离边界

- V1 模块、库目录与测试独立存在。
- 生产消费端在 Phase 4A 前保持单一既有行为，不根据 V1 库是否为空动态分支。
- 架构文档只描述当前已生效行为，计划文档描述目标状态。

验证：全量测试与基线行为一致。

### Phase 1：Schema、loader 与仓库 contract

- 在 `app/knowledge/cases.py` 实现 typed models、`CaseLibrary`、路径/quote/group/ID/curation 校验。
- quote 预算常量和异常归 V1 模块所有，不 import 旧 case 模块。
- 加入三个 stage 模板和最小 fixture。

验证：覆盖合法双-arm 往返，以及错单位、未知枚举、重复 input key、quote 不匹配、越界路径、group context/status 漂移、ID 错误、approved 无 outcome、不可变库等拒绝测试。

### Phase 2：纯内存检索、query adapter 与 projector

- 实现无 IO 的 `select_cases(CaseLibrary, query)`。
- comparison group 按 unit 排序和展开。
- 修正双平台配对、零共同字段排除、缺失不惩罚、字段级 trace 和稳定排序。
- 独立完成 assembly query adapter 与 V1 prompt projector，但不切换生产链。

验证：fixture 检索覆盖 group 完整返回、预算上限、双平台不覆盖、排序确定，以及不从缺失 runtime facts 推测数值。

### Phase 3：首批内容与迁移清单

- 从旧 10 篇 assembly 材料逐条归类为 `approved`、`draft` 或 `rejected`，不得静默丢失。
- 优先完整提取 2–3 篇具备论文/补充材料原文和直接 outcome 的论文，争取至少一组真实 comparison group。
- 只有 methods 摘要的材料保留为 draft，等待原始来源补齐。

验证：真实库 contract test 全绿；人工抽查 quote；形成覆盖全部旧 case 的迁移清单。

### Phase 4A：原子切换运行时

- `basis.py` 只加载 V1 `CaseLibrary` 并调用 V1 selector。
- `request.py` 只接收 V1 projector 输出。
- `retrieval_query.py` 只生成 V1 query，不保留 fallback 或混合 DTO。

切换门槛：全部旧 case 已归类；真实库至少有一个 approved V1 case；source/outcome 缺口有明确清单；全量测试通过。

验证：参数推荐 DTO 快照只包含 V1 case、quote 与 trace；空库行为显式且可测试。

### Phase 4B：删除旧链

- 删除旧 selector、v3 evidence 通道、旧 parameter case 数据与引用。
- 删除只服务旧链的 `evidence_case.py`、`numeric_case_retriever.py`、旧 query helpers 和对应测试。
- 更新架构文档、入口索引和 import contract。

验证：全量测试；仓库搜索确认旧 schema 字段和旧入口没有存活引用；检查无死文件和重复表示。

### Phase 5：后置消费端

在 Hi-C scaffolding、T2T 或 method recommendation 出现真实消费端后，再接入相同的 library/query/projector 契约；V1 不为尚不存在的调用方增加抽象。
