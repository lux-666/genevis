<a id="run-data-contract"></a>

# 运行数据契约

> **状态：** 已接受；当前生命周期/证据缺口见[架构一致性](conformance.md)。
>
> **阅读场景：** 精确事实所有权、运行路径、checkpoint 预算、崩溃行为、回放或保留规则。
>
> **来源：** [数据与存储](../topics/data-and-storage.md)。
>
> **下游：** [执行](../topics/execution.md)、[评审与恢复](../topics/review-and-resume.md)。
>
> **相关契约：** [资源交接](resource-handoff.md)、[执行证据](execution-evidence.md)。
>
> **代码依据：** `app/entrypoints/run.py`、`app/storage/paths.py`、`app/storage/frozen_inputs.py`、`app/storage/artifacts.py`、`app/storage/ledger.py`、`app/schemas/state.py`、`app/schemas/run_config.py`、`app/review/gates.py`、`app/review/persistence.py`、`app/graph/checkpoint_compaction.py`、`app/reports/`、`app/storage/audit.py`。

**范围：** 当前工作流会话。

**兼容边界：** 每个会话使用一种表示。

<a id="1-purpose"></a>

## 1. 目的

Gene Agent 运行带参数推荐、人工评审、重试、大型工具输出和持久恢复的长时生物学工作流。数据模型必须使其可审计，同时避免同一可变事实散布于产物、索引、Ledger、checkpoint 和报告。

所有阶段共用 ArtifactRef、Ledger、评审 intent/application 与执行生命周期。参数持久化有明确任务范围：Assembly/AutoHiC 及 T2T 单轮 schema-3 路径使用参数快照/manifest/批准尝试；GapCloser、T2T、Base Polish 默认 advisor 使用 finishing 共同任务记录，不沿用 schema-3 参数 manifest。两种记录不可互换。

<a id="finishing-task-evidence"></a>

### Finishing 任务证据

`app/stages/finishing/` 拥有 GapCloser/T2T/Base Polish 顾问的设置、源绑定历史、请求/响应、结果选择和执行审批。当前 occurrence 的 `advisor/config.json` 固定源候选与设置/预算；`recommendations/NNN/request.json` 保存精确消息、QUAST 与各轮原生测量，`response.json` 保存解析完整提案、原始模型输出和错误。`attempts/NNN/assessment.json` 绑定源、参数、推荐、人工决策、候选主输出、原生证据与执行引用。T2T 的每轮 `frozen.json` 固定阈值、基序、源/读段/工作目录与审批引用。Checkpoint 保留 finishing 游标和引用，不保存模型正文。

`finishing_recommendation` 复用持久评审请求和 intent/application，但其效应为 `finishing.recommendation.v1`，不创建 schema-3 ParameterAttempt。审批精确绑定设置、源、历次 assessment、推荐与 QUAST；执行前重验 occurrence、参数、预算和来源。同源的新参数尝试目录隔离；恢复复用已发布响应与执行证据；早先候选的交付与最终 T2T 报告使用所选轮次证据。

T2T `alignment_observations.tsv` 保存过滤前每条 PAF 两端的测量、全部拒绝原因及冻结尝试/执行/PAF 引用。请求汇总遍历完整表，仅示例限 100 行。补丁候选表继续只包含进入支持分组的行；其中 supporting_reads 去重后的支持数汇总覆盖全部行。没有比对或没有通过阈值的候选必须可区分。

T2T 的 deterministic_baseline 使用单轮 schema-3 策略请求与批准记录，要求已提供基序，以目录 L0、空 L2 和不可变策略审批冻结；不调用基序/阈值模型，不建立交互式 `parameter_approval` 门，补丁采纳仍走 `t2t_review`。默认 T2T advisor 使用 finishing 共同任务记录，设置、请求/响应、assessment/history 和每轮 `frozen.json` 按 finishing 证据链保存。模式与交接边界见[阶段契约](stage-contracts.md#9-t2t-finishing)。

<a id="2-product-decisions"></a>

## 2. 产品决策

1. 每个 `runs/<session_id>/` 是逻辑所有权边界。运行所属审计/决策证据不得依赖其他会话或全局内容寻址对象库；外部输入保持显式依赖。
2. 持久恢复面向最新安全前沿，尤其是阻塞人工门禁。
3. 报告文件名和产物类型是内部实现细节。
4. 当前参数代码中的“Planner”指阶段参数推荐器，不是父图规划或独立 Assembly 方法顾问。
5. 公共确定性运行使用 `gene-agent run --manifest PATH --run-config PATH --data-dir PATH`。manifest 单独拥有会话身份，`data-dir` 决定 `runs/<session_id>/` 布局。RunConfig 必须明确包含 `requested_stages` JSON 列表，各项为 `{stage, allow_local_execution}`。固定方法名单由描述符默认值推导，只存于冻结 `workflow_steps`。Assembly 是阶段拥有的例外，可省略 `selected_method_names`，在上游 QC/Survey 后推荐确认；省略表示延迟绑定，显式空名单可读取但不活动。工作流投影始终写固定名单；状态读取器为恢复接受缺失固定名单，但计划生成不会产生它。列表保留字面顺序和重复 occurrence；stage 是路由选择，allow_local_execution 是进程授权，没有独立本地执行数组。`requested_stages` 不得为 null，`[]` 是合法空生物学计划；`None` 只表示未完成规划/TUI 草稿，公共入口在冻结/执行前拒绝。冻结生成唯一运行时权威 `workflow_steps`，从隐式 `step-000-inventory` 开始，后续位置 ID 如 `step-001-t2t`。运行时不得追加、重排或替换。
6. Review 位置保留字面 RunConfig 数据；Assembly/T2T 不建立强制 Review，冻结不合成。明确请求的 Review 带 `review_target = {resource_type: "assembly", producer_step_id: "..."}`，绑定最近前置组装交接 occurrence。
7. Gene Agent 是单操作者本地 pip 工具，信任控制运行目录的操作系统账户。完整性校验针对部分写入、崩溃、不兼容版本、意外损坏；不提供针对同一操作者的对抗隔离。

<a id="3-replay-terminology"></a>

## 3. 回放术语

区分两种回放：

- **审计回放：** 重建精确输入、模型请求、保存响应、解析结果、人工决策、冻结命令、执行结果、因果顺序。记录外部依赖，但不保证仅复制运行目录就能获得其字节。
- **执行回放：** 重新运行命令，还需要工具环境、参考数据库、生物学输入字节。每项依赖须记录不可变来源和哈希，但不强制复制共享环境/数据库。

规划中的运行元数据回放能力词汇（不属于当前收口门禁）：

- `fully_materialized`：所需输入字节全部由运行拥有。
- `verified_external_dependencies`：需要外部输入/数据库，并已记录不可变 manifest 和哈希。
- `audit_only`：原命令可检查，但不能承诺再次执行。

当前运行时**不发布** `control/run.json` 或其他携带该枚举的单一运行字段。已持久化评估回放所需冻结输入、机器依赖、每次尝试依赖，但消费者必须检查这些记录，不能相信不存在的声明。运行级摘要及候选 `control/run.json` 所有者留作规划；当前收口门禁按冻结输入和每次尝试依赖核验，不要求这项尚未实现的摘要字段。

“自包含运行”只在原记录布局完整时保证审计回放。执行回放还须冻结证据证明全部字节和环境仍可用；未来运行级声明可摘要，不能替代证据。迁移外部依赖或绝对阶段游标路径不在保证内。

<a id="4-fundamental-invariants"></a>

## 4. 基本不变量

<a id="41-one-authoritative-record"></a>

### 4.1 唯一权威记录

每个持久事实只有一个权威所有者。其他层只可复制有界、确定性的查找标量，如 ID、状态枚举、时间戳、短错误码、计数、内容哈希。每个投影须标明或能推导权威源。

规则不是“值只能出现一次”，而是“只有一份记录权威，复制值必须小型、不可变、可推导”。

<a id="42-immutable-evidence"></a>

### 4.2 不可变证据

Ledger 公开读取在文件共享锁内完成解码与校验，等待持有独占追加锁的写者提交完整行；锁释放后仍残缺的 JSON 明确报损坏。读取不存在的 Ledger 不创建运行目录。锁内追加使用已持锁的读取器，不递归获取锁。该约定保证协作进程的完整前缀读取，不承诺断电后的文件系统耐久性。

证据发布后不可变。向既有路径发布不同字节是错误；逻辑文档改变时使用新 ID 和路径。

<a id="43-storage-owned-references"></a>

### 4.3 存储拥有的引用

存储不可变产物引用（`ArtifactRef.path_ref`、索引行、经 `SessionPaths` 解析的引用）是相对运行根的 POSIX 路径，不得包含：

- 主机绝对路径。
- `data/` 前缀。
- 运行根之下再次出现当前 session ID。
- `..` 路径穿越。
- 逃出运行根的符号链接。

规则范围比“所有以 `_ref` 结尾的字符串”更窄。当前运行时也持久化外部声明输入和部分阶段执行游标的规范绝对路径；它们是已验证运行时/依赖投影，不是可移植运行产物引用。

<a id="44-checkpoint-is-a-recovery-cursor"></a>

### 4.4 Checkpoint 是恢复游标

Checkpoint 保存最新控制前沿和不可变引用，不是执行历史、审计存储、报告缓存或参数尝试数据库。

游标必须足以恢复，标明继续所需全部不可变记录。冻结和推荐决策从记录加载，恢复时绝不按当前代码/配置重算。

<a id="45-ledger-is-a-causal-event-stream"></a>

### 4.5 Ledger 是因果事件流

Ledger 记录发生了什么、顺序如何、由哪些不可变记录证明，不嵌入候选表、信号数组、资产注册表、模型载荷、响应解析文档或执行结果。

每个阶段运行事实绑定 occurrence：携带规范阶段、非空 `workflow_step_id`、感知 occurrence 的 subject 或幂等身份。会话创建、终态、checkpoint 压缩等运行级事实是明确例外。类型化 `record_occurrence_event` 是领域事实常规写入边界；中央 Ledger 拒绝所有带 stage 却缺步骤 ID 的运行封装。`session_terminal` 是显式诊断例外，可保留 stage 标签而不把运行结果归给某 occurrence。

生命周期有三个独立身份域，不能相互推断：

- **工作流 occurrence 结果：** `stage_completed`、`stage_skipped`、`stage_failed` 之一，键为冻结 `(workflow_step_id, stage)`。
- **方法处置：** `not_started`、`skipped`、`completed`、`failed` 之一，键为 `(workflow_step_id, stage, method_name)`；skipped/failed 必需原因，not_started 断言没有尝试开始。
- **尝试终态：** `attempt_succeeded`、`attempt_failed` 之一，以不可变 `attempt_id` 为键，绑定阶段、方法、occurrence。

固定阶段 occurrence 拥有有序 `selected_method_names` 名单。它是不变意图，不是另一个生命周期结果；注册表成员或展示候选不代表选择，后续授权/环境检查可使名单方法 not_started/skipped。Assembly 可在冻结省略名单；随后依据 QC/Survey 持久化、必要时人工确认 `assembly_selection`，只绑定验证后的 `primary_method` 用于候选规划和参数推荐。方法必须在冻结快照中并通过阶段预检，不读实时配置。结算要求 `primary_method == assembly_selection.primary_method` 且已有方法处置。显式空名单不能由计划生成，但可读为不活动名单，永不等同延迟绑定。重试仍属于同一已绑定方法。

RunConfig 和持久规划记录不携带 Assembly 方法或方法参数块。规划 schema 拒绝 `method_selection` 澄清。对话工具/伴随读段提示是临时采集输入，在草稿持久化前丢弃。唯一持久方法权威是阶段时 `assembly_selection`。

三个域正交：方法 skip 后阶段仍可能交付完成；方法未开始前阶段也可能失败；提供或评审证据可使阶段无进程完成。反过来，进程成功不证明收集、资源交付或阶段契约成功。

方法处置不从尝试/occurrence 终态推导。选定方法可有零次或多次尝试，occurrence 终态记录交付而非选择。Freeze/run/collect/共享无尝试适配器仅在最终方法边界向注册结果策略暴露类型化持久游标/证据。共享方法结算按当前名单验证 `MethodOutcome`，是唯一处置写入者；阶段不得直接调用 writer 或从私有游标文字推断。任何 occurrence 终态前，结算要求有效名单和持久处置精确集合相等；固定阶段用冻结名单，延迟 Assembly 用阶段绑定。缺失/多余方法都拒绝；开放人工门或重试可暂时不完整，因为尚非终态。

阶段到父图的 `workflow_step_outcome` 是紧凑 checkpoint 交接，不是持久真相。包含精确身份、`completed | skipped | failed`、skip/fail 必需原因和完成时的组装交付声明。完成/skip 恰好推进一个冻结位置，失败不推进。控制器/恢复共享边界按当前冻结 occurrence 验证并持久化 Ledger。游标移动、阶段 `status`、`route_trace` 不能替代。

<a id="46-reports-are-projections"></a>

### 4.6 报告是投影

报告由权威产物、Ledger 及必要的实时紧凑状态生成，不拥有业务真相。控制与恢复不得依赖报告；并非所有报告均能只从持久记录独立重建，具体范围见第 15 节。

<a id="47-shared-stage-contract"></a>

### 4.7 共享阶段契约

已实现可执行阶段共用不可变证据、必要审批、冻结尝试、执行、评审、重试概念。阶段可增加类型化产物内容，不可新增存储层或所有权模型。

<a id="48-boundedness-is-enforced"></a>

### 4.8 强制有界

紧凑状态必须由 schema 和测试强制，注释或命名 compact 不够。

<a id="49-declared-input-content-is-logically-non-empty"></a>

### 4.9 声明输入的逻辑内容非空

每个声明生物学成员冻结前须为逻辑非空常规文件。未压缩输入要求正大小；gzip 必须打开声明流并读到至少一个解压字节，头尾不算，截断或不可读流无效。Bzip2/xz 因执行链无端到端契约而预检失败。这是轻量内容边界检查，不是完整 FASTQ/FASTA/BAM 语义解析；格式验证仍归阶段。

该不变量适用于用户输入。运行内过滤可合法产生零读段与零汇总，方法结果可审计，但不能发布为可用下游资源；所属阶段按契约结算。

<a id="410-one-workflow-progress-reducer"></a>

### 4.10 唯一工作流进度 reducer

附着和分离进度共用纯 occurrence reducer，权威输入为：

1. 已验证冻结有序 `workflow_steps`。
2. 规范 occurrence 终态事件。
3. 规范会话终态，失败时包括 failure scope。
4. 存在时的已验证活动人工门。

按精确步骤 ID 计数，重复阶段独立。只有运行非终态时，首个未终结 occurrence 才是当前；完成/失败会话无当前 occurrence。阻塞门的 stage/step 必须匹配首个非终态位置才能标识当前。`stage_entered`、Ledger 尾顺序、checkpoint 游标移动、`route_trace` 仅诊断，不决定当前或增加终态数。

失败 `session_terminal` 恰好含一个 `failure_scope: occurrence | controller_boundary`。Occurrence 要求连续 completed/skipped 前缀后恰好一个匹配 failed，之后无终态；controller_boundary 要求无 stage_failed，完整连续前缀只有 completed/skipped，且终结器从终态 checkpoint 证明前一已结算、下一从未进入。无支持 scope 的失败终态标降级，不猜测。

展示 `finished` 始终等于 `completed + skipped`；不得将当前索引、缺当前或终态游标解释为计数。

新写入不能产生重复/冲突 occurrence 终态。若旧 Ledger 已存在，按保守 `failed > skipped > completed` 投影，输出完整性诊断并标降级；无法证明计划前缀完整时不提供权威当前 occurrence。

<a id="5-fact-ownership"></a>

## 5. 事实所有权

| 事实 | 权威所有者 | 其他位置允许 |
|---|---|---|
| 运行身份与请求配置 | `control/` | ID、引用 |
| 物化时的运行内复制输入字节 | 输入产物 | 引用、哈希、大小、来源摘要 |
| 外部生物学声明/成员身份 | 冻结 manifest + 不可变资产注册表 | manifest/注册表引用、有序资产 ID、计数、验证摘要 |
| 机器环境/包/数据库选择 | 冻结依赖 manifest | 引用、验证状态 |
| 参考/数据库来源与内容验证 | 冻结主机数据库注册表 + 阶段不可变证据 | 引用、有界身份、验证状态 |
| 逐文件测序身份、平台、格式、声明来源 | 不可变资产注册表 | 注册表 ArtifactRef、有序成员 ID、计数、平台/格式集合 |
| 派生读段到来源映射 | 不可变 read-set manifest | ArtifactRef、有序源 ID、计数 |
| 组装主产品与方法原生产物 | 不可变 executed candidate 包 + 索引 | 包引用、选定方法/候选 ID |
| 推荐使用的精确 profile | 输入/profile 快照产物 | 引用、哈希 |
| 描述符、目录、案例、策略依据 | 不可变运行证据快照；案例只保存每次推荐选中内容 | 引用、哈希、选定 ID |
| 模型或策略请求 | Assembly/AutoHiC 的 `model_request.json`；T2T baseline 的 `policy_request.json` | 请求引用、模型 ID（调用时） |
| 模型或策略响应 | Assembly/AutoHiC 的 `model_response.json`；T2T baseline 的 `policy_response.json` | 响应引用、解析状态 |
| 解析的方法/参数推荐 | `recommendation.json` | 引用/哈希、紧凑接受投影 |
| 响应结构解析结果 | `response_parse.json` | 引用、parsed/invalid、解析错误 |
| 人工请求 | 不可变请求产物 | 请求 ID/引用/哈希、门 ID、步骤 ID |
| 人工决策 | 不可变决策产物 | 决策枚举、行动者、引用/哈希、步骤 ID |
| 冻结 Runner 输入 | 不可变冻结尝试 | 引用、尝试状态 |
| 执行脚本/命令身份 | 冻结尝试 + 保留 `work/` 脚本 | 尝试引用、状态 |
| 执行结果 | 不可变执行记录 | 引用、终态、返回码 |
| 方法处置 | occurrence 绑定 Ledger 事实，适用时带证据引用 | 阶段游标投影、报告状态 |
| Occurrence 终态 | 唯一 occurrence 终态 Ledger 单例 | 最新紧凑交接、报告投影 |
| 尝试终态 | 唯一尝试终态 Ledger 单例 | 执行游标投影、报告状态 |
| stdout/stderr、工具输出 | 不可变产物 | 引用、紧凑计数 |
| occurrence 组装交付 | `resource_delivered`，引用精确执行候选与主输出 | 当前 `resource_refs["assembly"]` 路由投影 |
| 全读段 QC 测量 | 从精确有序成员直接计算的不可变汇总 | 摘要产物、紧凑阶段投影 |
| 生命周期顺序 | Ledger | 读取模型的最后事件序号 |
| 产物位置/完整性元数据 | 产物索引 | `ArtifactRef` |
| 最新恢复前沿 | Checkpoint | 不能提升为业务真相 |
| 当前状态/会话摘要 | `reports/` 投影 | 工作流状态不引用 |

输入 profile 是由上游证据导出的决策快照，不替代 manifest、原始输入或上游结果。

<a id="6-physical-layout"></a>

## 6. 物理布局

下树定义所有权根。除专属规范声明外，子文件名只是示例。

```text
data/
├── layout.json
└── runs/
    └── <session_id>/
        ├── control/
        │   ├── tui_state.json（仅 TUI）
        │   ├── manifest_draft.yaml（仅 TUI）
        │   ├── input_manifest.yaml
        │   ├── run_config.json
        │   ├── dependency_manifest.json
        │   └── frozen_inputs.json
        ├── ledger/
        │   └── events.jsonl
        ├── checkpoints/
        │   └── checkpoints.sqlite
        ├── artifacts/
        │   ├── index.jsonl
        │   ├── inputs/
        │   ├── snapshots/
        │   └── steps/
        │       └── <workflow_step_id>/
        │           └── <stage>/
        │               ├── parameter_recommendations/
        │               ├── attempts/
        │               ├── outputs/
        │               └── reviews/<review_request_id>/
        ├── reports/
        │   ├── current_status.json
        │   ├── assembly_review.md
        │   ├── session_summary.json
        │   └── session_summary.md
        └── work/
            └── steps/<workflow_step_id>/<stage>/...
```

规则：

- `layout.json` 声明 `schema_version: 2`，防止静默混合不兼容布局。
- 结构化 run 和 TUI 仅从冻结 `input_manifest.yaml`、`run_config.json` 执行。TUI 专属草稿/state 是可变采集记录，不是执行器权威；无可变 `manifest.yaml` 执行权威。
- 最后发布 `frozen_inputs.json`，绑定 manifest、run config、dependency manifest 哈希。
- `control/run.json` 是接受的未来元数据所有者，当前不创建；见回放缺口。
- 阶段目录不重复 session ID。
- 数值推荐/尝试目录使用 occurrence 局部补零 ID，如 `000`、`001`、`002`。
- 准备尝试时 `work/` 可变，之后作为恢复工作区保留，恢复原地校验脚本。
- 报告是投影，故 `reports/` 不在 `artifacts/` 内。
- 逻辑引用契约没有全局 `data/artifacts/` 或对象库。

物理硬链接/reflink 可节省空间，但运行引用仍须在运行内解析；复制运行不得留下指向全局对象的 ArtifactRef。这不改写外部 manifest 或局部运行游标的绝对路径。

<a id="7-artifact-reference-v2"></a>

## 7. ArtifactRef v2

每份存储不可变引用使用同一类型契约：

```json
{
  "schema_version": 2,
  "artifact_id": "art_opaque_unique_id",
  "path_ref": "artifacts/steps/step-004-assembly/assembly/attempts/000/command.json",
  "sha256": "<sha256-of-exact-file-bytes>",
  "size_bytes": 1234,
  "artifact_type": "assembly.command"
}
```

要求：

- ID 不透明且运行内唯一，不能只由阶段、方法、basename 推导。
- `path_ref` 相对运行根且恰好解析一个文件。
- `sha256` 哈希精确文件字节，不删除自引用字段后只哈希语义子集。
- 需要语义哈希时另用明确字段，如 `proposal_semantic_hash`。
- `size_bytes` 支持不打开文件的完整性和存储审计。
- 索引可加来源元数据，但 ArtifactRef 本身足以定位与验证。

<a id="8-artifact-index"></a>

## 8. 产物索引

`artifacts/index.jsonl` 是不可变产物的追加查找索引。每行完整 ArtifactRef，另含：

- 工作流步骤、阶段、方法 ID。
- 适用的推荐、尝试或评审 ID。
- 创建时间。
- 可获得时的生产事件 ID。
- 紧凑来源元数据。
- 可达性分析需要的直接依赖引用。

索引不拥有业务状态、人工决策、模型推理或执行结果。同一注册路径不能有两个哈希；已注册字节不可改变。派生报告不进索引；若对外交付报告须成为保留记录，应导出为带源水位和内容哈希的新版本证据产物。

<a id="9-run-snapshots"></a>

## 9. 运行快照

快照保留参数推荐使用的精确依据：

- 方法描述符和能力。
- 参数目录。
- 选定文献/证据案例。
- 知识包元数据。
- 重试限制与审批策略。
- 相关工具和参考数据来源。

重试从验证后的初始链继承可执行目录、材料事实、策略。Assembly 每次新推荐含重试都重读实时案例与软件解释，只记录选中证据，不创建运行级案例快照；同一尝试恢复保持原请求。AutoHiC 保留初始依据语义并写尝试范围副本。实时库边界见[案例契约](casepack.md#live-directory-model-projection-and-ownership)，不授权改批准参数或可执行目录。

跨运行物理去重只是实现细节；不得将会话相对快照引用替换为全局纯哈希引用。

<a id="10-recommendation-data-boundary"></a>

## 10. 推荐数据边界

<a id="100-policy"></a>

### 10.0 策略

精确策略由[推荐策略](recommendation-policy.md)拥有。冻结 `RunConfig.assembly` 带 `method_route_advisor`、`parameter_advisor`，`RunConfig.hic_scaffolding` 带自身 `parameter_advisor`。Assembly/Hi-C 游标重复适用冻结模式，使恢复和证据读取无需重开配置解释记录。

<a id="101-assembly-method-recommendation"></a>

### 10.1 Assembly 方法推荐

顾问模式下，延迟 Assembly 在 QC/Survey 后进行一项持久事务：

1. 持久精确方法请求，含允许方法集合。
2. 调用模型一次，单独持久原始响应。
3. 验证明示 `recommended_method`、读段类型、路线、输入兼容、描述符支持身份。
4. 持久解析推荐，请求人工确认。
5. 确认后绑定紧凑 `assembly_selection`，启动独立参数推荐事务。

选定路线却无明确方法的响应拒绝。原始响应发布后、下一 checkpoint 前崩溃时，通过不可变请求哈希恢复，不再次调用。Checkpoint 只存三个引用和紧凑接受选择/profile；提示词、响应、推理、QC/Survey 证据、候选目录留在产物。

请求带按 `read_qc`、`genome_survey` 索引的 `evidence_status`，值为 `"available"` 或 `"unavailable"`。不可用配对字段为 null，不能当观测；可用证据不支持单一路线时提示词要求 `insufficient_data`。状态从已完成前置步骤推导，不看游标字段是否存在：过期或 skip 生产者即使游标含值也屏蔽。

<a id="102-parameter-recommendation"></a>

### 10.2 参数推荐

[参数推荐](parameter-recommendation.md)拥有模型 DTO、提示词、响应解析、审批、阶段/模型职责划分；数据 v2 只拥有持久位置。

使用参数推荐的方法：

- 调用前持久精确请求。
- 原始响应与解析推荐是不同不可变事实。
- 评审请求以引用和哈希绑定推荐。
- 拒绝或解析失败不创建执行尝试。
- 重试绑定已封存前驱，不静默更换可执行目录、材料或策略；Assembly 的新文献检索遵循第 9 节明确例外，并记录新选定证据。

推荐 manifest 组织验证所需引用。Checkpoint 只携带有界游标/引用，不含请求、响应、profile、basis 或推荐正文。阶段专属 DTO/存储适配器是预期设计，第二事实所有权模型不是。

<a id="11-execution-attempt-data-boundary"></a>

## 11. 执行尝试数据边界

执行前完成冻结；适用推荐/审批时须先完成，否则从已验证确定性输入直接冻结。

通用持久最小集合有意保持小型：

1. 标识精确 Runner 输入的冻结尝试记录。
2. [执行证据](execution-evidence.md)定义的规范执行记录。
3. 业务输出发布后的不可变引用。

冻结记录包含完整规范 Runner 输入：`script_path`、`working_dir`、预期输出路径、环境投影、Conda 选择、类型化依赖，并遵守秘密策略。注册依赖保留资产身份及大小/SHA-256；运行依赖保留完整 ArtifactRef 和精确命令路径；明确外部文件保留大小/SHA-256；目录保留确定性树哈希、文件数、总字节。记录在启动前持久化，执行前立即重验全部依赖。

来自模型推荐/审批生命周期的参数化执行中，`ParameterAttempt` 是 `frozen_parameters_ref`、`command_ref`、`frozen_attempt_ref`、`execution_ref`、`review_ref` 的唯一 checkpoint 关系所有者。外层阶段游标存有序尝试链、`current_parameter_attempt_id`、生命周期状态、有界路由投影，不得用 `*_frozen_attempt_ref` 或 `*_execution_result` 重复引用。重试追加新尝试并推进 ID，不覆盖前驱引用。封存 manifest 和终态时间线包含同一组引用，包括 Runner 实际使用的精确冻结引用。

非参数化方法的类型化游标可直接拥有冻结/执行引用。两者恢复均加载冻结记录，不按当前描述符、目录、配置、环境重建命令。

引用脚本保留在 `work/`，恢复验证存在性与冻结 SHA-256。人工删除引用工作文件后，该尝试不再可恢复，必须拒绝。脚本哈希用于完整性校验，不能恢复被删字节；系统不因删除而从当前配置重建命令。

只有重试/评审需要把多份独立记录绑定为不可变链时，才要求封存 manifest。简单一次性方法不必仪式化创建 `frozen_parameters.json + command.json + run.sh + attempt_manifest.json` 全套。文件名是细节，schema、类型、哈希、引用才是契约。

尝试终态只描述该冻结调用。Ledger 在序号分配同一独占锁内，对不可变尝试 ID 最多允许成功/失败之一。精确语义回放返回原行；改变终态类型、阶段/方法/occurrence、摘要或引用集产生 `LedgerConflictError`，即使幂等键不同也一样。

方法处置是独立 occurrence 事实，覆盖有/无尝试的方法：

| 处置 | 意义 | 尝试关系 |
|---|---|---|
| `not_started` | 具备执行资格前前置条件失败 | 无 attempt-start 或终态 |
| `skipped` | 刻意不调用选定可执行方法 | 无 attempt-start 或终态，需原因 |
| `completed` | 必需方法工作与收集完成 | 可执行时至少一个匹配成功尝试；planner-only 透传无尝试完成 |
| `failed` | 开始的方法工作或收集失败 | 已冻结尝试时有匹配失败尝试，需原因 |

单个方法处置不能自行结算 occurrence；阶段契约决定仍交付、明确 skip 或失败。`MethodOutcome` 是内部结算值，不是 checkpoint 新权威。共享生命周期/无尝试节点经明确注册策略从持久类型化游标/证据导出并在父路由前结算。可重试失败在策略确定最终方法结果前只发尝试事实。

<a id="12-review-contract"></a>

## 12. 评审契约

每个人工门在以下位置创建不可变请求与决策：

```text
artifacts/steps/<workflow_step_id>/<stage>/reviews/<review_request_id>/request.json
artifacts/steps/<workflow_step_id>/<stage>/reviews/<review_request_id>/decision.json
```

支持证据包和可视附件为请求引用的不可变产物。候选比较是评审包的一节，不是第二份近似 Markdown 报告。

Checkpoint `active_interrupt` 只含有界控制数据：

```json
{
  "stage": "assembly",
  "gate_id": "parameter_approval",
  "review_request_id": "review_...",
  "request_ref": {"...": "ArtifactRefV2"},
  "request_hash": "...",
  "blocked_at": "..."
}
```

该示例省略完整 ArtifactRef 等展开字段；实际中断还必须按 schema 绑定当前 `workflow_step_id`。允许决策、候选、信号、指标、推荐理由、评审策略属于请求产物，经验证引用加载。应用前恢复验证身份、请求哈希、决策 schema、绑定推荐/尝试哈希。

`payload.max_blocked_seconds` 是有效冻结策略值。运行全局 `ReviewPolicyConfig.max_blocked_seconds` 默认 300；唯一覆盖为 `review_policy.stages[stage].max_blocked_seconds`，只允许注册的评审阶段。有效值冻结到每个请求的评审所属阶段。一次阶段覆盖作用于该阶段全部门，包括 Assembly 参数审批与路线选择，没有门专属超时配置。Assembly Review 除非通过 `review_policy.stages.assembly_review` 显式覆盖，否则沿用全局值。

值表示前台行为，不是持久截止时间。新运行冻结前验证 stdin/stdout TTY 后进入同一附着执行器。`-1` 无限等，`0` 立即触发注册合格 resolver，正数是回车认领倒计时，到期触发同一 resolver。前台循环共享，决策保持门专属，由注册表声明。

不可变请求存在后才计时。参数门超时只批准请求绑定的精确推荐，不生成、编辑、合并、替换值。Resolver 资格、请求/推荐绑定、理由产物、类型化意图/应用生命周期仍权威。自动决策还须内部恢复事务来源和绑定倒计时 metadata 指明当前 `foreground_countdown`；决策来源仍由 resolver 拥有（`llm_provisional` 或 `auto_rule`），请求 metadata 不能自授权。

Ctrl-C、EOF、退出留下 `blocked_human`，不持久化或启动后台/守护计时器。重执行同一冻结会话不调用阻塞 checkpoint 或恢复计时器。后来人工 review/resume 不按 `blocked_at` 推断经过时间，没有公开超时覆盖或 strict/auto 模式。

评审注册表是门 ID、允许决策、occurrence 校验、时序 metadata、prepare/apply 的唯一声明和类型化分发点，不拥有阶段载荷含义或提示词分支；注册阶段准备持久化领域效果，验证后的注册应用将类型化效果投影到 checkpoint。

## 13. Ledger v2

每行 `ledger/events.jsonl` 使用公共封装：

```json
{
  "schema_version": 2,
  "sequence": 42,
  "event_id": "evt_...",
  "idempotency_key": "...",
  "session_id": "...",
  "timestamp": "...",
  "event_type": "attempt_succeeded",
  "workflow_step_id": "step-004-assembly",
  "stage": "assembly",
  "subject_id": "attempt_000",
  "refs": [],
  "summary": {}
}
```

要求：

- sequence 会话内单调增加。
- event_id 唯一，幂等键防止节点回放/进程重启重复效果。
- refs 指向权威产物，summary 仅有界查询字段。
- Review 事件按步骤和请求 ID 绑定，不只按阶段名。
- 每个冻结 occurrence 最多一个 completed/skipped/failed。追加按步骤 ID 唯一并验证阶段；精确语义回放返回原行，结果或摘要/引用改变则冲突，不受幂等键影响。同一追加锁还证明持久处置名集合等于有效方法名单；延迟 Assembly 使用绑定 selection 的验证主方法。
- 每个不可变尝试最多一个 succeeded/failed，同样受追加锁保护。
- `method_disposition_recorded` 绑定 occurrence 并以方法名为键，只记录四种处置、必要有界原因和证据引用。
- `resource_delivered` 绑定 occurrence；当前 v1 只处理 assembly，记录 materialized/reused 和精确候选/主输出角色，在 stage_completed 前写。
- `env_preflight_blocked`、`candidate_comparison_generated`、`registered_assembly_candidate_bound`、`automatic_review_failed` 都是阶段事实，必需精确生产 occurrence；幂等身份包含 occurrence，防止同阶段两次失败合并。
- 路线事实使用 stage_entered、三种 occurrence 终态、session_terminal。Stage_skipped 仅限明确阶段内决策，省略 occurrence 不产生。
- 失败 session_terminal 仅在匹配失败 occurrence 时使用 occurrence scope，或终结器证明已结算前缀与未进入下一步后使用 controller_boundary。完成终态无 failure scope。
- 不复制产物 metadata 到 Ledger；尝试/评审事件只含解释因果转换所需引用。

所有追加路径（含会话终态）在同一独占锁内强制两个独立不变量：先验证事件域自然键唯一，记住精确域回放但不立即返回；再验证幂等键最多拥有一个语义事实，既有者具有相同规范语义正文。两者都通过才返回精确回放，否则追加一行。自然键回放不能隐藏该调用幂等键被别的事实占用。

完整有序路线、明确方法处置/阶段 skip 原因、不可变交付、评审生命周期、终态必须无需 checkpoint/报告即可重建。

运行进度没有独立持久投影。附着/分离调用 4.10 reducer。附着渲染前父图已共享结算交付与终态；控制状态可提供候选活动门，不能提供终态计数。分离检查从验证冻结控制、Ledger/评审、会话终态导出同一输入。SQLite、进度标记、锁观察是相邻运维信号，不是状态权威。计数/百分比是临时展示算术，不得持久为产物、报告、Ledger、checkpoint、进度表或批量状态。

两种文本渲染直接以 completed_steps + skipped_steps 输出 finished。仅 `projection_authority == "degraded_stage_terminal_projection"` 才警告降级，非空 `"authoritative"` 不是警告条件。不提供当前时显示未知标记，不能编造 done 或将游标位置变进度。

机器可读分离检查为 `inspect_schema_version: 1`。`workflow_progress` 只用验证的冻结 RunConfig 作有序分母，用 occurrence 限定 Ledger 终态计数。重复名按步骤 ID 区分；失败/阻塞单列，不算 finished。冻结输入缺失/无效时 plan_source=unavailable，全部计划派生字段为 null，不按注册顺序/已见事件猜测。验证冻结输入前还隐藏当前 occurrence、active_interrupt、decision_requirements、可执行 review next_action；原 Ledger 仍可诊断，但不能授权恢复。

规范完成会话的 `final_assembly` 只来自终态范围内最新 assembly resource_delivered。候选和主引用须匹配索引，通过大小/SHA-256；失败则不提供最终组装，报告 `final_assembly_integrity_error`。Checkpoint 字段或报告路径不能宣布最终 contig。

重复/冲突阶段和尝试终态绝不“最后事件获胜”；保守读取模型输出完整性诊断，进度降级，方法失败列表只含规范尝试投影，不能同时报告成功和失败。

相邻 `execution` 报告 `frozen_inputs_status`、只读持久进度 fresh/checkpoint/partial/unavailable、本地咨询性锁观察 true/false/未知 null。锁不证明远程存活或调度器状态，检查不得创建控制目录/锁文件。只有冻结输入验证且证明当前会话 checkpoint 行时 `durable_resume_available` 才真。仅 SQLite 文件存在、不可读库或无本会话的库仍可报告 sqlite 持久化类型，但不能宣称可恢复。

## 14. Checkpoint

允许内容：

- 会话/thread ID。
- 不可变 workflow_steps、当前索引、阶段、监督状态。
- 最新紧凑 workflow_step_outcome，等待或反映共享结算；可替换恢复状态，不是终态权威。
- 不可变阶段种子；重复 occurrence 取新副本，不用专属方法配置。
- 延迟 Assembly 紧凑验证 selection/主方法、有界接受 profile、不可变方法请求/响应/推荐引用。
- 当前阶段状态、参数推荐引用、当前尝试 ID 与紧凑有序 ParameterAttempt 链；非参数化则直接尝试引用。
- 紧凑活动中断、重试计数、控制/配置引用。
- 路由/展示所需有界阶段评审投影，如 Review 候选、信号、指标摘要。
- 有界诊断路线尾部。

紧凑结算交接结构：

```json
{
  "workflow_step_id": "step-004-assembly_dedup",
  "stage": "assembly_dedup",
  "outcome": "completed",
  "reason": null,
  "deliveries": [
    {
      "resource_type": "assembly",
      "delivery_mode": "reused",
      "candidate_bundle_ref": {"schema_version": 2, "artifact_id": "..."},
      "primary_output_ref": {"schema_version": 2, "artifact_id": "..."}
    }
  ]
}
```

普通完成无 reason，skip/fail 必需。仅完成时可带 deliveries，最多一项 v1 组装交付，实际使用完整 ArtifactRef（示例省略展开字段）。父图拒绝非当前或刚结算冻结步骤结果。Ledger 终态存在后可被下个交接替换。

禁止内容：

- 参数目录、规则、案例、描述符快照。
- 原始证据 profile、提示词/模型上下文、候选目录。
- 模型响应/推理。
- 完整或无界候选、信号、指标、比较表正文。
- 响应解析文档。
- argv、脚本、环境映射、执行结果。
- stdout/stderr 尾部。
- 报告载荷。
- 无界路线轨迹/评审注释列表。

中央边界把 stage_outputs 当已知阶段键的有界 JSON 映射。当前阶段有类型化游标适配器，多数写入经其完成，但中央尚不统一强制逐阶段 schema 或拒绝每个未知游标字段。它强制 JSON、已知阶段、逐游标/总字节限制、产物/冻结尝试正文嵌入防护。映射仅代表各阶段当前/最新游标，重复 occurrence 从 stage_seeds 替换，不能作跨 occurrence 历史；身份与证据在产物、评审、尝试、Ledger 按步骤 ID 索引。有界评审摘要是路由投影，权威证据在不可变索引产物。

初始预算：

- 规范序列化 TopLevelState 目标不超过 64 KiB。
- 测试硬上限 256 KiB。
- active interrupt 不超过 4 KiB。
- route tail 最多 16 项。

增加预算须有测量证据支持的架构变更，不能为嵌入产物扩容。活动/阻塞运行只支持最新可恢复 checkpoint API。终态提交且 Ledger 重建与无 checkpoint 产物可达性检查通过后，数据库压缩为一个独立终态 checkpoint。

<a id="15-reports-v2"></a>

## 15. 报告 v2

报告是读取模型：

- current_status.json 原子覆盖最新操作者状态/动作。
- assembly_review.md 当前从实时阶段状态与索引评审包生成。
- session_summary.json 是结构化结果/时间线。
- session_summary.md 可选渲染同一摘要。

摘要可含组装评审、候选比较、人工决策、失败、缺口、产物链接。只有不同不可变证据或确有不同受众契约才单独文件。

current_status 与摘要 writer 记录至少含最后 Ledger 序号和索引摘要的源水位。报告不是 Ledger、索引行或恢复输入，只是尽力视图。不要求全部可独立重建或有等价水位。会话摘要可从持久记录生成；当前状态和 Review Markdown 可消费实时紧凑状态。恢复依赖 checkpoint 和验证证据。

Trace 验证完整冻结事务，只以验证 RunConfig 投影步骤。标记、哈希、schema、生物学验证失败时不直接解析已变 RunConfig，回退带 occurrence 的 Ledger/产物事实；证据不足报告缺口，不编造注册顺序。

<a id="16-central-path-ownership"></a>

## 16. 中央路径所有权

仅存储层构造运行路径。当前直接或经阶段/存储助手使用的 SessionPaths 接口：

```python
paths.workflow_step_artifacts_dir(workflow_step_id, "assembly")
paths.workflow_step_artifact_path(
    workflow_step_id, "assembly", "parameter_recommendations", "000", "..."
)
paths.workflow_step_artifact_path(
    workflow_step_id, "assembly", "attempts", "000", "..."
)
paths.workflow_step_artifact_path(
    workflow_step_id, "assembly", "reviews", review_request_id, "request.json"
)
paths.workflow_step_work_dir(workflow_step_id, "assembly")
paths.workflow_step_work_path(workflow_step_id, "assembly", "attempts", "000", "...")
paths.report_path("session_summary.json")
```

阶段不得在 run_dir 下追加 session_id 或构造 data/artifacts。契约只有一个写布局。架构测试应拒绝存储 API 外阶段直接构造 artifacts/work/ledger/checkpoints/reports 根。

<a id="17-durable-write-and-crash-protocol"></a>

## 17. 持久写入与崩溃协议

一次证据发布：

1. 在内存序列化并验证完整新证据。
2. 在目标目录写临时文件。
3. Flush、验证、原子重命名到不可变路径。
4. 幂等追加索引。
5. 幂等追加语义 Ledger 事件。
6. 返回紧凑 checkpoint 更新。

Occurrence 结算有更严格共享顺序，父控制器与终态恢复调用同一边界：

1. 阶段发布并索引契约必需结果。
2. 返回已验证 workflow_step_outcome，不写自身终态。
3. 按当前冻结 occurrence 验证；组装交付还验证当前 ResourceRef 投影相同候选/主输出。
4. 追加或精确回放每项 resource_delivered。
5. 追加或精确回放唯一 occurrence 终态。
6. Ledger 事实存在后才能附着渲染转换或展示下一步当前。

交付后、stage_completed 前崩溃留下可恢复交付前缀。回放必须验证精确交付并补终态，不能另建交付或从推进游标推断完成。

崩溃规则：

- 有产物/索引无语义事件只是孤儿候选，不是完成转换。
- 有语义事件而 checkpoint 过期，事件仍是审计权威；当前恢复仍需保留 checkpoint，仅有少数阶段/证据回放例外，无通用 Ledger 到 checkpoint 重建。
- 相同路径/哈希重复发布幂等；不同字节拒绝。
- Checkpoint 不能是完成事实唯一所有者。
- 交付后无对应完成 occurrence 时，即使当前引用已指向字节，Review 也不可消费。

<a id="18-secrets-and-sensitive-data"></a>

## 18. 秘密与敏感数据

持久记录排除 API 密钥/认证头、密码/token/私钥、秘密环境变量、URL 内凭据、无限制完整环境转储。

模型和命令记录使用允许列表配置投影。秘密影响执行时，只记录存在性和源名称，不记录值。

<a id="19-retention-and-storage-audit"></a>

## 19. 保留与存储审计

保留活动恢复所需 checkpoint、冻结控制输入、评审记录、Ledger、索引产物、方法工作文件。压缩遵循第 14 节。

storage-audit 只读诊断层用量、重复内容、checkpoint 结构、产物完整性/可达性，不分类删除候选或提供清理 manifest。自动删除和索引压缩不在契约内。

报告可选，移除不影响恢复/审计，系统不承诺重建全部被删报告。移除引用工作文件/持久证据可阻止恢复，不能从 storage-audit 输出推断安全删除。

运行 ArtifactRef 会话局部，删其他运行/全局缓存不能破坏引用。外部 manifest 和阶段绝对路径仍是显式依赖，迁移/删除源后执行回放仍可能不可用。

<a id="20-session-representation"></a>

## 20. 会话表示

每运行根只读写一种表示，不混布局、双写格式、解析备用布局、混同报告/产物身份。只能用兼容 checkpoint 的代码恢复；不兼容升级前完成活动会话或保留匹配检出。

合理双表示仍允许：原始输出/规范摘要、不可变产物/纯元数据索引、紧凑事件/产物引用、JSON 报告/Markdown 渲染。

<a id="21-conformance"></a>

## 21. 一致性

已实现行为与开放缺口仅在[架构一致性](conformance.md)跟踪。
