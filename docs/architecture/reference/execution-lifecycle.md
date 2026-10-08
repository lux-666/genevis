<a id="method-execution-lifecycle-contract"></a>

# 方法执行生命周期契约

> **状态：** 已接受并实现。
>
> **阅读场景：** 整次运行就绪、精确 freeze/run/collect 路由、尝试所有权、恢复或失败策略。
>
> **来源：** [执行](../topics/execution.md)。
>
> **下游：** 阶段收集、评审、持久证据。
>
> **相关契约：** [运行数据](data-contract.md)、[执行证据](execution-evidence.md)、[参数推荐](parameter-recommendation.md)。
>
> **代码依据：** `app/entrypoints/run.py`、`app/storage/frozen_inputs.py`、`app/execution/run_readiness.py`、`app/execution/lifecycle.py`、`app/execution/method_outcomes.py`、`app/execution/attempt_owners.py`、`app/control/occurrence_settlement.py`。

<a id="scope"></a>

## 范围

本规范拥有冻结前运行专属就绪边界和每个可执行方法的共享执行生命周期，定义图边界、尝试所有权、恢复路由、进程失败行为，以及执行内核与阶段适配器的分工。

[运行数据契约](data-contract.md)拥有持久事实位置和引用规则；[执行证据](execution-evidence.md)拥有规范进程结果正文；[参数推荐](parameter-recommendation.md)拥有模型请求、响应、解析和审批行为。本规范连接这些契约，不取代它们。

<a id="whole-run-readiness-boundary"></a>

## 整次运行就绪边界

真实 TTY 检查后、任何冻结会话输入发布前，结构化 run 和 TUI 都计算规范有效本地方法闭包。就绪检查通过活动机器映射解析每个固定本地方法，要求非空工具/包探测声明，验证环境并探测声明工具。随后同一选定闭包捕获到整次运行依赖快照。就绪失败不创建会话。

Assembly 是延迟绑定例外：整次运行就绪捕获已配置候选依赖范围，但不假装某个候选已被选择。上游证据具备后，Assembly 从冻结范围选择，并在冻结方法尝试前预检所选方法。

是否必需 LLM 端点由选定工作流和[推荐策略](recommendation-policy.md#readiness-and-evidence)决定。需要时，就绪检查调用配置端点，只接受精确解析 JSON 对象 `{"ready": true}`。空输出、自由文本、JSON 数组或其他结构对象都失败。这验证结构化输出能力，不只是认证或网络连通性。

此边界只读。Bootstrap 对更广配置目录的验证，以及方法稍后的冻结依赖验证，仍是独立检查。

<a id="canonical-flow"></a>

## 规范流程

所有可执行方法共用执行和结算边界：

```text
固定阶段冻结方法名单
  ->（Assembly：上游证据 -> 方法推荐/确认）
  ->（适用时：参数推荐与审批）
  -> 方法尝试冻结
  -> run
  -> collect
  -> 结算方法结果
  -> 阶段拥有的评审与提交
```

`freeze -> run -> collect -> settle` 由 `app.execution.lifecycle.build_method_graph` 实现。参数推荐是可选准备能力，不是第二执行轨道。

生命周期为共享结算生成类型化**方法结果**，在 Runner 启动时还生成**尝试终态**。结算将方法结果持久化为方法唯一的**方法处置**。两者都不是工作流 occurrence 结果。只有父图结算边界在阶段返回明确 `workflow_step_outcome` 且全部交付声明验证后，记录 `stage_completed | stage_skipped | stage_failed`。正交事实域见[运行数据](data-contract.md#45-ledger-is-a-causal-event-stream)。

方法处置不是重复的尝试摘要。一个 occurrence 可选 planner-only 方法或刻意跳过本地执行，因此没有尝试；可重试方法可有多个尝试终态，但只有一个最终处置。Occurrence 结果回答阶段是否交付，不回答各选定方法发生了什么。

固定方法阶段的选择已冻结在 occurrence 的 `selected_method_names`。Assembly 是唯一延迟绑定例外：整次运行冻结时可省略名单，阶段必须依据上游 QC/Survey 推荐并验证 `assembly_selection` 后才绑定 `primary_method`。运行时不能将展示的候选视为已选，也不能恢复后替换固定名单。动态方法必须从冻结依赖快照解析，通过阶段预检，并在结算时对照 `assembly_selection.primary_method`；其方法处置仍是必需持久事实。这样 `not_started` 才有明确含义，也无需 Assembly 在证据具备前猜方法。

只有持久且确认的 `assembly_selection` 才绑定方法。规划 schema 不含 `method_selection` 澄清或用户可编辑 Assembly 名单。工具名称和伴随读段提示可指导临时资产收集，但在草稿持久化前丢弃，不能作为选择事实进入运行时。

一次 `run` 转换至多推进一个冻结尝试。有序直接批次若仍有待执行游标，方法图保持冻结状态、checkpoint 已完成前缀并返回 `run`。只有所有所属尝试都有持久执行证据才进入收集。

方法子图入口按当前阶段状态选择恢复边界：

- 生命周期 `frozen_status` 直接进入 `run`，不重建命令。
- `ran_status` 直接进入 `collect`，不再次执行。
- 其他状态进入阶段拥有的 freeze 回调。

冻结和每次 run 后，冻结状态循环到下一待执行尝试，ran 状态进入 collect。所有出口经过共享结算节点。纯生命周期专属结果策略只读取前一边界产生的类型化持久游标/证据，返回已验证最终 `MethodOutcome`，或在重试/评审仍可继续时返回无结果，绝不从未注册状态字符串猜测。父阶段路由拥有后续失败、评审、重试和提交决策。

<a id="lifecycle-specification"></a>

## 生命周期规格

`MethodExecutionLifecycle` 只包含执行级配置：

- 规范 `stage` 和 `method_name` 身份。
- 诊断 `name`。
- `frozen_status`、`ran_status` 恢复标记。
- 一个尝试所有者。
- 预期输出清理策略。
- 进程失败策略。
- 可选有界失败类型。

已实现的进程失败策略：

- `"fail"`：持久化失败进程证据并停止生命周期。
- `"collect"`：运行有序尝试批次，由 collect 将个体进程结果翻译成领域信号。

`"collect"` 仅适用于普通进程结果。身份、游标、发布、Ledger 或证据持久化失败立即停止，否则批次将不可审计。

生命周期规格不包含参数目录、提示词渲染、生物学 schema、评审规则或父阶段路由。

<a id="attempt-ownership"></a>

## 尝试所有权

两种所有权策略进入同一运行内核。

<a id="direct-attempts"></a>

### 直接尝试

`DirectAttemptOwner` 在阶段输出上存储非空有序 `execution_attempts` 游标列表。阶段适配器验证结构，拥有：

- 尝试和方法身份。
- `frozen` / `executed` / `execution_failed` 状态。
- 不可变冻结尝试引用。
- 执行后的规范执行证据引用。

单命令方法使用单项列表；QUAST、BUSCO 等有序评估器每个候选一项。执行和收集保留游标顺序，加载时验证方法、尝试身份、状态和引用。执行期间允许已完成前缀与冻结后缀；每次转换只推进第一个冻结游标。

<a id="parameter-attempts"></a>

### 参数尝试

`ParameterAttemptOwner` 将恰好一个冻结尝试和执行引用绑定到当前已批准 `ParameterAttempt`。冻结尝试必须显式携带与当前游标一致的 `parameter_attempt_id`。生命周期只验证选定尝试；追加、重试、封存和时间线代码负责完整链校验。

方法专属冻结或执行键不得重复存储 `ParameterAttempt` 已拥有的引用。

<a id="stage-adapter-boundary"></a>

## 阶段适配器边界

方法适配器拥有两个回调：

- **freeze 回调：** 验证选定阶段输入和批准参数，写方法脚本，构建完整 `FrozenAttemptSpec`，调用 `freeze_method_attempts` 持久化并绑定。
- **collect 回调：** 通过 `load_method_executions` 加载验证后的执行，校验必需生物学输出，发布或注册产物，记录领域 Ledger 事实，返回下一阶段增量。

适配器不调用 Runner、不发布命令日志、不持久化规范执行证据、不创建阶段局部生命周期工厂、不直接写方法处置。Freeze、run、collect 及共享无尝试规划适配器留下已验证类型化游标/证据，由注册结果策略返回 `MethodOutcome`。结果绑定精确工作流 occurrence、阶段、规范方法名、处置、有界原因和不可变证据引用；它是内部结算值，不是新的 `TopLevelState` 或 `workflow_step_outcome` 字段。

共享方法结算节点是处置的唯一写入者：

- 尝试前最终前置条件失败返回 `not_started`。
- 选定可执行方法被刻意不调用返回 `skipped`。
- 选定 planner-only 透传方法应用无进程契约后返回 `completed`。
- 所有必需工作成功收集返回 `completed`。
- 执行或收集失败只有在方法重试/评审策略无继续路径后才返回 `failed`。

允许重试中的单次失败仍是尝试事实，不提前结算方法。有序批次在整个方法收集边界决定最终结果后才结算。精确回放返回原处置，不同结果冲突。规划/无尝试路径在明确决策后立即使用同一结算节点；阶段模块只供纯结果策略，绝不调用 Ledger writer。共享结算写入前拒绝有效名单之外的方法。固定阶段使用冻结名单；延迟 Assembly 使用阶段验证后的单一 `primary_method`。阶段终态前，共享 occurrence 结算要求有效名单中的每个方法且仅这些方法恰好具有一份持久处置。人工阻塞和重试继续不执行终态覆盖检查。

成功 collect 适配器还提供 `attempt_succeeded` 所需证据；run 或收集失败边界只有实际存在尝试时才提供 `attempt_failed` 证据。尝试事实因果上先于最终方法处置。Occurrence 终态仅由父图结算拥有。

<a id="run-boundary"></a>

## Run 边界

任何恢复查找、依赖检查或进程启动前，共享 run 节点验证生命周期状态和所有者游标，再要求全部待执行冻结尝试的 `workflow_step_id` 精确匹配该阶段当前 occurrence。缺失或过期身份产生生命周期有界失败更新；不搜索可恢复执行证据、不调用 Runner、不发布日志、不追加尝试事件。通用产物加载器可检查非当前尝试，但执行不得将其视为当前 occurrence。

该门禁通过后，run 才经验证引用加载下一冻结尝试；阶段、方法、会话、尝试身份必须一致。工作目录必须解析到当前运行 `work/steps/<workflow_step_id>/<stage>/` 内；声明的绝对脚本路径必须在字面上位于该工作目录内，并解析到运行内；所有预期输出也须解析到运行内。保留脚本必须存在且匹配冻结 SHA-256。Runner 以冻结工作目录为 cwd 执行精确声明路径。生产本地分发为直接 argv：`<interpreter> <script_path>` 或前加 `conda run` 包装，始终 `shell=False`。非空 `FrozenAttemptSpec.env_setup` 不受支持，在两种分发前都必须失败；环境覆盖使用类型化 `env` 映射或专门冻结的设置脚本。

Occurrence 门禁后、实时输入检查和 Runner 调用前，run 在产物索引搜索阶段、方法、尝试 ID、精确冻结引用均相同的执行记录。唯一验证匹配可推进所有者游标而不重跑；不匹配或歧义记录拒绝。

每个冻结尝试还声明类型化 `input_dependencies`。注册表外部资产冻结资产 ID、规范路径、大小、SHA-256；运行内输入冻结完整 `ArtifactRef` 和精确命令路径；明确未注册文件冻结路径、大小、SHA-256；外部目录冻结确定性树哈希、文件数、总字节数。共享 run 在调用 Runner 前立即验证全部依赖，恢复后同样如此。缺失、重新注册、路径漂移或字节漂移均不启动进程。单进程中，只有规范路径、设备、inode、大小、纳秒 mtime/ctime 均不变时才复用有界 SHA-256 缓存；任何指纹变化强制重算字节哈希。恢复进程从空缓存开始。

`run_frozen_attempt` 拥有 Runner 调用、stdout/stderr 发布、产物索引登记、输出清理策略、attempt-started/attempt-failed Ledger 事实。任何依据结果的发布或持久化前，必须检查 Runner 返回的尝试 ID。

每个进程结果转为规范 `ExecutionEvidence`，在推进所有者游标前持久化到外层记录，绑定精确冻结引用和生命周期边界处置。预期基础设施失败转为有界失败阶段增量；意外编程错误仍交由图失败边界可见。

规范执行证据提交后可防重跑；但进程退出与证据发布之间发生机器故障时，不提供进程级 exactly-once。本地子进程没有可查询或重新附着的调度器身份。

<a id="collect-boundary"></a>

## Collect 边界

共享图以统一预期失败边界包装每个 collect 回调。声明错误类型涵盖的文件系统、布局、Ledger 格式/冲突、已验证产物加载失败产生有界 `failed` 阶段游标。边界尽力记录因果 Ledger 事件；Ledger 损坏不能阻止失败状态增量。类型化游标或证据正文损坏可抛普通 `ValueError`；普通回调 `ValueError` 及其他编程错误不转为正常工作流结果。

Collect 适配器决定必需/可选生物学输出，成功物化后拥有 attempt-succeeded 事实。预期收集失败在尝试尚未终结时可记录 `attempt_failed`，并返回失败阶段游标；不得写 `stage_failed`，由父图消费阶段明确失败的 `workflow_step_outcome`。

Ledger 追加边界在独占追加锁内保证每个不可变尝试 ID 只有 `attempt_succeeded`、`attempt_failed` 之一。应用代码检查既有成功只是优化，不是该不变量。精确回放返回已有终态，不同结果、身份绑定、摘要或引用集冲突。冲突行保守投影为失败并附完整性诊断，不能按最后事件覆盖。

<a id="conformance"></a>

## 一致性

使用共享生命周期的方法必须满足以下要求。中央 checkpoint 仍只提供公共 JSON/大小边界；这些保证不意味着每个阶段路径投影都是 `ArtifactRef`，或所有游标键在中央边界统一类型化：

1. 每个生产可执行方法都经过 `build_method_graph` 和共享 run 内核。
2. Assembly、AutoHiC、T2T 参数化方法使用 `ParameterAttemptOwner`。
3. 普通方法和批次使用 `DirectAttemptOwner`。
4. 进程成功与失败都持久化规范执行证据。
5. 冻结后恢复不重建命令，任何重跑前先恢复已提交执行证据。
6. 每个命令输入通过类型化冻结输入依赖绑定。
7. 有序批次每次尝试后 checkpoint，保留已完成前缀。
8. 生产代码无方法专属重复游标键、包装产物或阶段局部生命周期实现。
9. 方法处置、尝试终态、工作流 occurrence 结果保持独立；不从游标状态、尝试结果、路由标记或游标推进合成另一个域的终态。
10. 追加任何 occurrence 终态前，有效方法名单与持久处置集合相等；延迟 Assembly 仅从已验证阶段选择导出名单。

参数重试使用新尝试 ID 和专属工作/输出目录。完成前驱保持不可变；后续尝试不能复用其可变结果目录或覆盖已发布输出。
