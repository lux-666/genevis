<a id="parameter-recommendation-contract"></a>

# 参数推荐契约

> **状态：** 证据与超时审批契约已实现。
>
> **阅读场景：** 精确模型 DTO、schema-3 持久化、稀疏响应、审批或重试语义。
>
> **来源：** [参数推荐](../topics/parameter-recommendation.md)。
>
> **下游：** 已批准方法尝试冻结与评审。
>
> **相关契约：** [执行生命周期](execution-lifecycle.md)、[运行数据](data-contract.md)。
>
> **代码依据：** `app/domains/parameter_recommendation/`、`app/schemas/parameter_recommendation.py`、`app/schemas/parameter_catalog.py`。

<a id="definition"></a>

## 定义

参数推荐器在方法选择后运行。Assembly 的方法选择有意属于阶段：上游 QC/Survey 证据具备后，持久化并确认 `assembly_selection`，推荐器再接收已验证 `primary_method`。它请求 LLM 提出参数，绑定到可评审的审批请求，不选择方法、不路由父图、不批准自身答案、不合成后备答案。

Assembly、Hi-C、T2T 共享生命周期和持久化概念，但没有一个通用生物学 DTO。

<a id="reusable-model-boundary"></a>

### 可复用模型边界

`app/llm/recommender.py:StructuredRecommender[T]` 拥有一次模型调用、原始响应捕获、JSON/函数参数提取和任务解析。调用者提供解析器，子类可绑定输出 schema 与任务提示词。`RecommendationResult[T]` 保留解析数据、原始文本、可获得的提供方消息和明确失败信息。

支持 `prompt_json`、原生 `json_schema`、通过唯一 `submit_recommendation` 函数强制 `function_calling`。函数仅提交数据；不会执行模型给出的函数名。没有隐式传输回退或额外模型重试。端点能力必须匹配所选传输；严格提供方 schema 要符合其支持的 JSON Schema 子集，本地解析仍必需。

Assembly、Hi-C、T2T 参数推荐器和 T2T 基序解析复用此边界，同时保留既有 prompt-JSON 契约、稀疏映射和持久封装。任务请求构建器和规范化器仍独立。Review 决策也复用调用/解析边界，同时保留请求/决策校验与发布。调用者拥有的格式适配器保留绑定时 `json_schema` 到 `json_object` 的既有后备（以及没有 `bind` 的可注入模型），绝不重试失败的模型调用。新任务可像 `app/llm/dedup.py:PurgeRecommender` 一样继承边界并使用不同类型化输出。

Dedup 的 QUAST/执行/推荐/评审循环由阶段拥有，不隐藏在模型基类中。评审门禁拥有确认与前台倒计时，执行生命周期拥有进程恢复。见[去冗余阶段契约](stage-contracts.md#5-assembly-dedup)和[操作者配置](../../user_guide/parameter_recommendation.md#iterative-purge_dups-recommendations)。

<a id="module-ownership"></a>

## 模块所有权

```text
app/llm/parameters/
├── assembly/
│   ├── request.py       # 严格模型输入 DTO
│   ├── prompt.py        # DTO -> 精确提供方消息
│   ├── recommender.py   # 模型调用与 JSON 解析
│   └── response.py      # 唯一声明的响应契约
├── hic/
│   ├── request.py       # AutoHiC 专属模型输入 DTO
│   └── recommender.py   # 提示词渲染、模型调用、响应解析
└── t2t/
    ├── motif.py         # 物种基序请求、模型调用、响应解析
    ├── request.py       # T2T 阈值模型输入 DTO
    └── recommender.py   # 提示词渲染、模型调用、响应解析

app/domains/parameter_recommendation/
└── facts.py             # 权威映射 -> 规范类型化事实

app/schemas/
└── parameter_recommendation.py
                         # 规范事实、类型化上下文、v3 快照/manifest

app/stages/assembly/parameters/
├── context.py           # 类型化 Assembly 阶段上下文
├── basis.py             # 不可变目录/证据快照
├── catalog.py           # 执行器/目录元数据
├── workflow.py          # 持久化、审批、重试转换
└── storage.py           # Assembly 产物适配器

app/stages/hic_scaffolding/parameters/
├── catalog.py
├── context.py
├── workflow.py
└── storage.py

app/stages/t2t/parameters/
├── catalog.py
├── context.py
├── workflow.py
└── storage.py

app/storage/
└── parameter_evidence.py
                         # 严格 v3 链加载器与已批准包边界
```

`llm/parameters/<stage>` 包拥有模型边界全部内容。阶段包拥有工作流状态、持久证据、审批、重试和执行交接。目录留在阶段侧，因为它也定义命令构建，不只是提示词资产。

<a id="required-flow"></a>

## 必需流程

```text
固定阶段所选方法，或已验证 Assembly assembly_selection
  + 允许列表中的生产者输出/运行事实
  -> 经防腐边界转为规范类型化事实
  -> 带类型化阶段上下文和依赖引用的 schema-3 输入快照
  -> 不可变依据快照
  -> 严格阶段专属请求 DTO
  -> 持久化精确渲染消息
  -> 将同一消息对象发送给模型
  -> 持久化原始响应
  -> 响应结构解析器
  -> 持久化精确稀疏参数
  -> 不可变哈希绑定参数审批门
  -> 已批准参数交给冻结/命令构建
```

映射只属于本次尝试，可为空，只包含模型推荐显式设置的参数。解析与不可变审批门之间没有缺键列表、推荐补丁合并、代理默认值、目录值验证器、硬规则验证器或自动生物学审批。未知名称和异常值只要符合声明 JSON 结构就保留；认领门禁的操作者看到精确解析映射。

前台时序、默认值、阶段继承与倒计时触发由[评审契约](data-contract.md#12-review-contract)拥有。参数门注册的超时 resolver 为同一持久映射、请求和推荐哈希提交 `approve_parameters`，然后沿类型化意图/应用事务执行。它不得编辑映射、调用推荐器、选择后备，或绕过请求、occurrence、manifest、前驱和尝试链校验。

执行是独立边界。审批后，命令构建仍可拒绝所选工具不能安全表达的参数。这是执行定义错误，不是 LLM 推荐校验器，也不允许静默替换值。

<a id="request-dtos"></a>

## 请求 DTO

Assembly `AssemblyParameterRequestDTO` 包含：

- `schema_version: 3`。
- `recommendation_contract: parameter_recommendation_v1`。
- `method_id` 和 `task`。
- 严格 `CanonicalRecommendationFacts` 类型的 `facts`。
- 类型化 `assembly_context`：路线、规范读段类型、目标层级、重试时有界时间顺序尝试历史。
- `adjustable_parameters`：描述/类型元数据，不带默认值。
- `knowledge_evidence`：任务专属紧凑案例、原生设置、`source_method.setting_explanations`；完整选定证据留在 basis。
- `material_context`、`method_decision`、`method_guidance`；空证据集合显式表达，不回退旧来源。

AutoHiC `AutoHiCParameterRequest` 包含：

- schema-3 与推荐契约。
- 当前任务和固定方法 ID。
- 规范物种事实。
- 类型化 `autohic_context`：文库协议、明确提供的限制性内切酶、重试时有界时间顺序历史。
- 可调整 AutoHiC 目录：限制性内切酶、编辑轮次、校正开关、错误检测阈值。

T2T `T2TParameterRequest` 包含：

- schema-3 与推荐契约。
- 当前任务和固定 minimap2 方法 ID。
- 规范物种事实。
- 类型化 `t2t_context`：解析后的端粒基序、来源、保留平台的输入集合中精确规范长读段成员数。
- 可调整端粒与比对阈值目录。

阈值请求前，仅当规范已提供物种事实没有基序时，T2T 才单独调用基序解析。已提供基序经校验后以 `provided_species_fact` 持久化，不调用模型；否则要求提供学名，持久化精确消息、原始响应、解析结果和解析来源。当前 schema 之外的 motif/config checkpoint 字段拒绝。阈值快照绑定已验证基序解析引用，所有扫描/补丁消费者只使用该证据。

全部 DTO 禁止未知顶层字段，排除会话身份、路径、产物 ID/哈希、存储元数据、凭据及无限制注册表元数据。审计产物可保留这些事实，模型 DTO 不可。

<a id="response-contracts"></a>

## 响应契约

每个初始和重试响应结构一致：

```json
{
  "parameters": {"parameter_name": "explicit_value"},
  "summary": "可选的评审说明。"
}
```

`parameters: {}` 合法。模型不返回审批决策、重试处置、停止指令、专家评审路由、缺键列表或映射补丁；这些转换由哈希绑定审批门与操作者重试策略控制。

契约精确：`params`、`recommended_params`、`parameter_patch`、顶层直接参数键、丰富逐参数包装对象等别名均拒绝。解析器只检查响应结构和继续工作流所需少数字段，不检查生物学正确性或目录范围。

各阶段 `parameters` 都是当前尝试显式 L2 映射，不是补丁，也不要求列举全部 L2 参数。省略键按冻结命令契约继续使用已提供事实、执行器默认或隐式工具默认。重试独立声明下一映射；应用不继承前次省略参数，也不要求任一键改变。

<a id="durable-records-and-checkpoints"></a>

## 持久记录与 checkpoint

`input_snapshot.json` 是严格 `RecommendationInputSnapshotV3`，含 `CanonicalRecommendationFacts`、一个带判别字段的阶段上下文及不可变依赖引用。`basis_snapshot.json` 冻结选定目录与阶段知识依据。`recommendation_manifest.json` 是严格 `ParameterRecommendationManifestV3` 根，将记录绑定到方法、任务、推荐 ID 和前驱身份。

`model_request.json` 拥有精确紧凑 JSON 消息和提示词身份。Assembly 记录还以 SHA-256 绑定输入和 basis 产物；同尝试恢复拒绝缺失/不一致绑定，不重建提示词。Basis 方法、消费者版本和证据目标检查适用于初建与恢复。运行、请求关联、检索查询身份从运行路径、推荐目录、manifest 推导，不重复存于输入快照、basis 或模型请求正文。

`model_response.json` 拥有原始提供方响应；`recommendation.json` 拥有解析推荐；`response_parse.json` 只记录声明结构是否解析成功，失败时记录解析错误。

每个推荐或尝试参数证据目标位置由当前 `workflow_step_id`、规范阶段、范围、非负标识、安全文件名一次推导。`_parameter_evidence_location` 为发布和崩溃恢复共同拥有该身份，避免路径漂移。发布独立强制可变源位于当前 occurrence 工作树内；恢复独立验证不可变产物类型、方法、metadata、哈希和索引目标。

v3 加载器在返回链前验证产物类型、哈希、schema、阶段、方法、任务、尝试链身份。非 v3 根、不兼容引用名、混合身份均拒绝。响应、解析、推荐、T2T 基序 manifest 叶子保留其声明版本；v3 适用于推荐快照/manifest 根和已批准尝试边界。Checkpoint 只含有界游标和引用，不含请求/响应正文。Assembly 紧凑阶段游标可保留恢复所需的已验证选择/主方法游标；完整推荐、审批、模型、命令、证据留在不可变产物和 Ledger。审批门解决前不增加执行尝试。

审批后，紧凑 `ParameterAttempt` 是批准参数、构建命令、冻结 Runner 输入、执行和评审证据的唯一关系所有者。阶段游标按 ID 选择当前尝试，不用方法专属键镜像引用。

创建重试前，封存并验证前驱。下一模型请求接收全部早先推荐、审批、冻结有效值、执行和收集/评审结果的确定性、有界、时间顺序投影。历史只作证据；应用不评分、不推导参数差分、不选择下一映射。

Assembly 输入存在性信号来自资产注册表，快照进类型化 `AssemblyInputProfile`。例如 raw/clean RNA-seq 资产设置 `has_rnaseq`；合成 `stage_outputs["rnaseq"]` 既非合法阶段也非输入信号。此上游路由 profile 不是参数推荐证据；推荐器另行持久化前述 schema-3 规范输入快照。

<a id="conformance-rules"></a>

## 一致性规则

测试必须证明：

- 请求 DTO 拒绝误入审计字段。
- 会话身份和存储定位符不进入模型消息。
- 持久消息与实际发送消息一致。
- 只接受声明的响应结构。
- 未知参数名和值原样通过解析并到达审批门。
- 别名、补丁、丰富包装、未声明顶层字段失败。
- schema-3 快照/manifest 拒绝 v3 前引用名与混合身份。
- 模型失败产生 `response_invalid`，不编造默认值。
- 人工与超时审批都以哈希绑定不可变推荐。
- 超时审批只接受当前持久映射，保留阶段专属校验边界。
- 超时决策记录请求理由、配置和实际经过的前台倒计时审计事实。
- 自动事务失败仅在同一不可变阻塞请求仍通过校验时返回人工评审。
- 批准参数不变地成为命令构建 L2 输入。
- 空稀疏映射仍到达审批。
- 重试映射绝不与前驱合并。
- 每次重试提示词包含全部早先尝试的有序证据。
- 冻结证据中已提供事实、执行器/工具默认值与 LLM 推荐可区分。

<a id="assembly-case-evidence"></a>

## Assembly 案例证据

`parameter_recommendation_smoke` 支持成对初始请求消融。两臂保持相同事实、确认方法、目录、材料上下文和软件指导；都移除可能含文献推理的方法选择理由，仅 `knowledge_evidence` 不同。来源请求必须包含选定文献案例并匹配输入快照。重试请求因历史结果可能混淆比较而拒绝。准备阶段不调用模型；在线评估使用相同模型配置，在新输出目录记录各臂请求与原始响应，不覆盖已有目录。解析/目录检查成功不证明生物学改善或 RAG 收益。

每个新 Assembly 推荐（含重试）都从配置的实时目录读取 `knowledge_evidence`。没有运行级案例快照。未配置 `assembly.case_source` 时文献选择为空；软件知识与当前材料事实仍可用。

[Assembly 案例契约](casepack.md)拥有提供来源方法感知、直接/跨方法证据和参数解释的唯一消费者。新参数请求使用确认方法、决策上下文、可调整目录。外部设置是解释证据，不扩充目标目录。每次尝试记录选定证据与解释；新重试重读案例，同尝试恢复使用记录的 basis、目录、精确模型消息/DTO，不在模板变化后重新渲染或投影。输入事实、任务、上下文仍须匹配不可变记录，冲突拒绝。Assembly 请求必须将 `input_snapshot_sha256`、`basis_sha256` 绑定到精确 manifest 引用，并含有效双消息请求/DTO。恢复、审批、执行在共享链加载边界使用同一验证器，确定性基线也一样。未绑定旧记录需要新运行，不事后补哈希。其他阶段保留自己的请求契约。稀疏 L2 解析、目录所有权、审批与执行解析继续由本契约拥有。
