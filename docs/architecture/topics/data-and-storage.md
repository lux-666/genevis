<a id="data-and-storage"></a>

# 数据与存储

> **状态：** 已实现以下有界契约；回放能力通过冻结输入、机器依赖和尝试证据核验。

评审应用和 checkpoint 证明由 `app/review/ledger.py` 校验后提交事件；Ledger 不导入评审业务模块，继续在独占锁内维护顺序、自然键幂等及终态冲突约束。Assembly 的请求语义验证器由调用者交给参数证据读取器，存储层只拥有引用、字节与证据链验证。

`ArtifactIndexView` 为一次读取调用提供只读索引视图，按 ID 和路径查询复用同次解析；每次解析 ArtifactRef 仍校验当前文件大小及 SHA256。写入后须重新构建视图，不跨事务缓存执行决定。
>
> **阅读场景：** 判断事实归属、追踪跨阶段文件，或调试发布、恢复、审计和报告。
>
> **来源：** [运行时编排](runtime-orchestration.md)、[方法执行](execution.md)、Inventory、人工评审。
>
> **下游：** [参数推荐](parameter-recommendation.md)、[AutoHiC](autohic.md)、下游阶段解析、[评审与恢复](review-and-resume.md)、操作者报告。
>
> **依赖契约：** [运行数据](../reference/data-contract.md)、[资源交接](../reference/resource-handoff.md)、[执行证据](../reference/execution-evidence.md)、[架构一致性](../reference/conformance.md)。
>
> **代码依据：** `app/schemas/state.py`、`app/schemas/resource_ref.py`、`app/schemas/assets.py`、`app/storage/asset_registry.py`、`app/storage/artifacts.py`、`app/storage/ledger.py`、`app/control/occurrence_settlement.py`、`app/graph/checkpointer.py`、`app/graph/checkpoint_compaction.py`、`app/reports/review_report.py`、`app/reports/trace_writer.py`。

<a id="one-run-several-owners"></a>

## 一次运行，多个责任所有者

`runs/<session_id>/` 是工作流运行的逻辑所有权边界。不同存储回答不同问题，不是同一个数据库的可互换副本。

| 层 | 权威范围 | 非权威范围 |
|---|---|---|
| `resource_refs` | 当前跨阶段逻辑路由投影，例如采纳的 `assembly`、当前 `long_reads` | 指定 occurrence 已交付资源的不可变证明、输入成员来源、执行历史、大型证据正文 |
| 资产注册表 | Inventory 注册的外部输入成员身份、声明来源、平台/格式、路径、验证事实 | 下游当前采纳哪个派生输出 |
| 不可变产物 | 推荐、评审、命令、执行和生物学输出的精确证据与文件字节 | 单靠自身确定因果顺序 |
| 产物索引 | 运行内位置、类型、哈希、大小、生产者身份、紧凑依赖元数据 | 业务状态、评审决策、模型推理、工作流路由 |
| Ledger | 幂等因果事件；occurrence/method/attempt 终态单例；不可变组装交付事实；共享评审状态；唯一规范会话终态及证据引用 | 证据正文、资产注册表、结果表、最新恢复游标 |
| Checkpoint | 最新有界控制前沿、最新 `workflow_step_outcome` 结算交接、继续执行所需紧凑引用 | 审计历史、持久终态/交付真相、报告、日志、命令、模型载荷 |
| 报告 | 尽力提供的操作者/读取模型投影；会话摘要使用持久记录，其他视图可使用实时紧凑状态 | 工作流控制、产物身份、因果事件、评审状态、恢复 |

职责划分是核心规则。其他位置可为路由或展示保留小型不可变投影，但投影不能成为第二权威。精确落点、引用、大小、保留和崩溃规则见[运行数据契约](../reference/data-contract.md)。

<a id="progress-is-direct-consumption-not-another-store"></a>

## 进度直接消费事实，不新增存储

交互执行不引入持久进度投影。附着和分离界面调用同一纯 reducer；精确输入、occurrence 计数、冲突处理和当前步骤规则由[运行数据契约](../reference/data-contract.md#410-one-workflow-progress-reducer)拥有。checkpoint 可用性、执行标记/锁、游标、Ledger 尾部、路线轨迹是诊断状态，不是其他进度权威。调用者可在运行目录外汇总多个只读检查，但批量投影不成为工作流真相。

<a id="cross-stage-handoff"></a>

## 跨阶段交接

`TopLevelState.resource_refs` 是阶段间传递文件资源的唯一表。阶段发布逻辑资源，下游按逻辑名解析。阶段私有游标留在 `stage_outputs`，不得成为隐藏的第二交接协议。

解析优先使用当前 `resource_refs` 项，再按资源规格使用资产注册表，不扫描其他阶段的输出键。注册表后备让原始 manifest 输入可初始化逻辑资源，后续采纳可替换资源而不改写原始来源。精确优先级、逻辑资源集合和 FOFN 边界见[资源交接契约](../reference/resource-handoff.md)。

组装当前表被替换时，配套记录绑定 occurrence 的不可变 `resource_delivered` Ledger 事实。它绑定生产者 occurrence 与精确执行候选、主输出引用，包括复用旧字节的透传 occurrence。下游 Review 要求生产者完成终态、交付事实、当前投影一致。Read QC 同样在当前交接中保留精确源成员/读段集合谱系；详细载荷由资源和阶段契约定义。

资产注册表本身是不可变产物。checkpoint 仅持有其已验证 `ArtifactRef`；消费者加载文档、解析有序成员 ID，并重验成员路径、大小和就绪校验和。`ResourceRef` 可投影成员 ID 与紧凑来源，但不嵌入注册表。

<a id="publication-and-recovery"></a>

## 发布与恢复

正常持久转换单向推进：

```text
工作文件或内存记录
  -> 不可变运行产物
  -> 幂等产物索引登记
  -> 方法/评审语义 Ledger 事件
  -> 紧凑 checkpoint 更新
  -> 可选报告投影
```

Occurrence 完成增加共享结算后缀，控制器和终态恢复共用：验证明确的 `workflow_step_outcome`，追加/回放各组装 `resource_delivered` 事实，追加/回放唯一阶段终态，再渲染附着进度。只有交付却没有完成终态，是可恢复的崩溃前缀，不能供下游消费。

产物索引使 `ArtifactRef` 可定位并可验完整性；Ledger 使转换可重建因果；checkpoint 告诉 LangGraph 从哪里恢复；报告方便人读取同一批事实。相同不可变路径和字节的重复发布幂等，冲突字节必须拒绝。

中央 checkpoint 写入边界验证 JSON 结构、已知阶段名、大小预算和误嵌入的产物/冻结尝试正文。多数阶段增加类型化游标适配器，但中央边界尚未对所有阶段局部游标施加统一 schema，不得宣称更强约束。

报告通过 `ProjectionBoundary`：普通投影失败保留为有界运维警告，不得改变评审状态、checkpoint、Ledger 终态权威或恢复结果。删除报告不得改变恢复或审计真相。会话摘要从 Ledger 和已索引产物重建；当前状态 writer 仍消费阻塞或失败边界传入的实时紧凑结果；阶段拥有的 `assembly_review.md` 使用实时阶段状态和已索引评审包生成。不要求所有报告均可独立重建或具有等价水位线；恢复由 checkpoint 和持久证据负责。

<a id="storage-audit-boundary"></a>

## 存储审计边界

`storage-audit` 只报告存储用量、重复内容、checkpoint 结构、产物完整性和可达性，不改变运行。不分类待删除文件，也不输出清理 manifest。自动删除文件和产物索引压缩不在当前契约内。

保留冻结尝试和恢复所需的 `work/` 与持久证据。报告是可选操作者视图，不要求重建所有报告。
