# CasePack V1 消费计划

状态：Case loader 与严格合同已实施；selector、projector 与运行时接入待执行

输入：Knowledge Repo 发布的 CasePack V1

职责：Load → Retrieve → Reason → Execute

## 1. 仓库边界

```text
Knowledge Repo
        │
        │ CasePack V1
        ▼
Gene Agent Repo
Load → Retrieve → Reason → Execute
```

两个仓库通过版本化数据包交接。Gene Agent 负责 CasePack 校验、内存检索、prompt 投影、
推荐推理和执行编排。

## 2. CasePack V1 Contract

```text
<casepack-root>/
├── manifest.yaml
├── sources/<paper_id>/
│   ├── paper.md
│   ├── supplement.md
│   └── scripts/...
└── cases/
    ├── assembly/<case_id>.yaml
    ├── hic_scaffolding/<case_id>.yaml
    └── t2t/<case_id>.yaml
```

Manifest：

```yaml
schema_version: 1
stages: [assembly, hic_scaffolding, t2t]
```

Case 表达一个样本、一个 stage、一个独立技术 arm：

```text
Case
├── source
├── stage
├── context
├── action
├── outcome
├── comparison_group?
└── rationale?
```

`case_id` 使用 `<paper_id>.<sample_id>.<stage>.<arm_slug>`。每个 evidence quote 是
CasePack source Markdown 的精确子串。

## 3. Load

`load_case_library(casepack_root)` 执行一次性全量校验并返回只读 `CaseLibrary`：

- manifest schema version 和 stage 集合；
- Case schema、ID、stage model、枚举和 canonical units；
- source 相对路径和 paper 边界；
- evidence quote 与 source 文本的精确对应；
- approved Case 的 curator、review time、直接 outcome 和证据覆盖；
- comparison group 的 paper、sample、stage、context、status 和 arm 上限；
- `runtime_method_id` 与 Gene Agent method registry 的 stage 对应关系；
- Case、quote 和 source 文件预算。

CasePack root 由运行配置提供。加载完成后，selector 只接收 loader 拥有的不可变
`CaseLibrary`。

## 4. Retrieve

### 4.1 Context matching

检索先按 `stage` 和 `status: approved` 过滤。参数推荐在 method 已知时按
`runtime_method_id` 或规范化 method 匹配。

- 输入数据按 `(data_type, platform, subtype, role)` 配对；
- 已提供的可比较字段参加评分；
- 类别字段精确匹配贡献 1，差异贡献 0；
- 数值字段使用已定义的归一化距离；
- 得分为实际参与字段的加权平均；
- trace 记录字段值、匹配方式、贡献和候选筛选原因；
- `case_id` 作为稳定排序的末级键。

### 4.2 Comparison group

comparison group 是检索单位：

1. 独立 Case 形成单元素 unit；
2. comparison arms 形成 group unit；
3. group score 取 arms 中最高 context score；
4. units 按 score 和稳定键排序；
5. 入选 group 的全部 arms 相邻投影；
6. group arm 上限与 selection 字符预算共同控制 prompt 大小。

## 5. Reason

prompt projector 投影：

- paper/sample/stage/case ID；
- context；
- action.method 与 action.parameters；
- stage-direct outcome；
- comparison group arms；
- 实际使用的 evidence quotes 和 locators；
- retrieval trace。

method recommendation 与 parameter recommendation 共享 CaseLibrary、selector 和 evidence
合同。

## 6. Execute

推荐结果通过现有 method registry、parameter catalog、review gate 和 execution lifecycle
进入 Gene Agent 执行链。Case provenance 与 retrieval trace 写入推荐 DTO 和运行记录。

## 7. 实施阶段

### Phase 1：External CasePack loader（已完成）

- typed stage models；
- CaseLibrary 只读边界；
- manifest、schema、ID、source、quote、unit、group、curation 和 registry 校验；
- 显式 `casepack_root` 输入；
- 合法与异常 CasePack contract tests。

### Phase 2：Selector、query adapter 与 projector

- 实现纯内存 `select_cases(CaseLibrary, query)`；
- 以 comparison group unit 排序和展开；
- 完成字段级 trace、稳定排序和 selection 预算；
- 完成 assembly query adapter 与 CasePack prompt projector；
- 建立 selector 和 projector snapshots。

### Phase 3：运行配置与加载

- 在运行配置中加入 `casepack_root`；
- 启动时加载并固定 CaseLibrary snapshot；
- 将 CasePack schema version 和内容 hash 写入运行 provenance；
- 建立配置、加载和空 pack 行为检查。

### Phase 4：推荐链切换

- parameter basis 使用 CaseLibrary 与 V1 selector；
- request DTO 使用 V1 projector；
- retrieval query 使用 canonical runtime facts；
- 推荐 DTO 固定 Case、quote、locator 和 trace snapshots。

### Phase 5：Stage 扩展

Assembly、Hi-C scaffolding 与 T2T 推荐入口共享相同的 CaseLibrary、query、selector 和
projector 契约，并使用各自 typed stage model。

## 8. 完成定义

1. Gene Agent 从显式 `casepack_root` 加载 CasePack V1；
2. loader 一次完成 manifest、Case、source、evidence、curation、group 和 registry 校验；
3. selector 纯内存运行并产生字段级 trace；
4. comparison group 作为一个 unit 被检索和完整投影；
5. prompt 包含结构化 context/action/outcome、证据原文和 locator；
6. 推荐记录包含 CasePack version、content hash、case IDs 和 retrieval trace；
7. Assembly、Hi-C scaffolding 与 T2T 共用一套消费合同。
