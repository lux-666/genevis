<a id="review-and-resume"></a>

# 评审与恢复

> **状态：** 已实现。
>
> **阅读场景：** 修改人工门禁、决策持久化、审批绑定、恢复校验或评审后路由。
>
> **来源：** 阶段拥有的不可变评审请求与紧凑阻塞 checkpoint。
>
> **下游：** [运行时编排](runtime-orchestration.md)、新参数尝试、资源采纳或终态失败。
>
> **依赖契约：** [运行数据](../reference/data-contract.md)、[执行生命周期](../reference/execution-lifecycle.md)、[阶段契约](../reference/stage-contracts.md)。
>
> **代码依据：** `app/review/gates.py`、`app/review/persistence.py`、`app/review/approval.py`、`app/review/recommender.py`、`app/llm/recommender.py`、`app/llm/json_response.py`、`app/control/resume_transaction.py`、`app/control/inspection.py`、`app/control/terminal.py`、`app/control/resume.py`。

<a id="durable-gate-model"></a>

## 持久门禁模型

评审门禁是四状态持久协议：

1. `awaiting_decision`：阶段已发布不可变评审请求产物，checkpoint 仅携带有界中断游标，含请求引用、哈希、`workflow_step_id`、阶段、门禁和绑定。
2. `decision_committed`：规范化的 `ResumeReviewRequest` 已作为该精确请求身份的不可变评审意图写入。
3. `application_prepared`：门禁已准备并发布类型化评审应用产物，包含已验证的决策产物与必需引用。
4. `applied`：存在已验证决策，且重读 checkpoint 能证明同一已验证应用引用已投影，或证明绑定的 `review_applied` 事件记录了已完成的 checkpoint 转换。若 checkpoint 有证明但事件缺失，恢复必须在图继续前补写 `review_applied`。

展示给人的提示是请求投影，不是恢复权威。checkpoint 不得嵌入完整请求、意图、决策或应用正文；Ledger 事件也不得替代这些产物。

阶段名不是评审身份：冻结计划可多次包含同一阶段。每个评审请求、决策、Ledger 事件与评审路径都绑定当前 occurrence 的 `workflow_step_id`；请求和决策产物位于 `artifacts/steps/<workflow_step_id>/<stage>/reviews/`。恢复拒绝 occurrence 与当前冻结工作流步骤不匹配的请求。

分离检查遵循同一权威边界。冻结工作流输入缺失、无效或与请求的阶段/occurrence 不一致时，可暴露原始 Ledger 请求用于诊断，但必须隐藏当前 occurrence、活动中断、决策要求和可执行评审下一步。未经验证的 Ledger 投影不能授权恢复。

可选 LLM 评审推荐器通过共享 `StructuredRecommender` 调用和解析模型。提供方边界只接受非空字符串或提供方文本块列表，不把任意对象转成看似模型输出的字符串。移除 Markdown 围栏和仅接受 JSON 对象的解析使用共享 `parse_json_object` 契约。Review 保留响应格式绑定兼容处理、`RecommendationError` 分类、会话/阶段/允许决策校验及推荐持久化职责，不维护另一套提供方内容提取器。

附着状态与分离产物检查均通过 `_review_request_decision_context` 投影已验证评审请求。载荷、绑定、阶段、工作流步骤、门禁和请求身份必须一致。两者保留独立信任检查：附着检查验证活动中断和冻结 occurrence；分离检查在投影前验证索引不可变引用及 Ledger 绑定请求身份。

评审注册表集中拥有门禁声明、允许决策、occurrence 校验、类型化 prepare/apply 分发和时序元数据。外部恢复 DTO 是唯一严格的 `ResumeReviewRequest`，decision 字段为字符串。规范化只验证提案，不写入；prepare 拥有门禁专属持久决策/证据创建；验证后 apply 是将注册类型化应用投影到 checkpoint 的纯函数。参数批准、Assembly 采纳、Hi-C 采纳和 T2T 采纳因此仍拥有不同门禁词汇与效果，入口不另设 apply 路径。

<a id="foreground-review-policy"></a>

## 前台评审策略

有效冻结评审策略复制到不可变请求，使门禁可在不读取当前可变配置的情况下验证。精确默认值、允许的阶段覆盖及数值意义由[评审契约](../reference/data-contract.md#12-review-contract)拥有。

| 等待配置 | 行为 |
| --- | --- |
| `-1` | 人工决定，无倒计时 |
| `0` | 按下表注册的明确动作立即推进；错误退出，无人工回退 |
| 正数 | 回车认领转人工；到期仅使用合格 resolver，没有合格动作则保持人工 |

本页矩阵是等待模式与默认动作的规范来源；其他阶段文档只描述门禁效果。非零等待沿用原有门禁策略。Assembly Review 保留证据资格与绑定 LLM 推荐产物；Assembly 路线选择使用注册的超时请求规范化器。Assembly、AutoHiC、T2T 参数审批使用同一注册规范化器，只绑定不可变请求中的推荐。没有合格规范化器的门禁保持人工。非零等待的自动事务失败后，仅当 `review_status` 仍为 `awaiting_decision` 且同一已验证 `blocked_human` 请求仍存在时，才可返回人工评审。意图一旦提交，操作者不得替换；恢复以 `request=None` 重入事务并应用已提交意图。任何身份或完整性漂移都拒绝。回车认领门禁后取消计时器并打开正常人工提示。

计时器不是持久调度器：Ctrl-C、EOF 或进程退出保留请求和 `blocked_human` checkpoint。全部阶段等待时间为 0 的结构化新运行允许无 TTY；存在非零等待时，缺少 TTY 仍在冻结前失败。TUI 交互入口仍要求 TTY。没有守护进程或后台唤醒。再次运行同一冻结会话会发现阻塞 checkpoint，不调用图，只打印人工恢复命令。后续 `review`/`resume` 始终是人工恢复，不从旧 `blocked_at` 重建或到期倒计时。接口不包含 strict/auto 评审模式或公开恢复超时覆盖。

## 零等待：先完成流程，再分析质量

`max_blocked_seconds: 0` 使用注册表中的明确推进策略，不读取菜单位置，也不将质量证据缺口作为人工暂停理由。`-1` 保持人工，正数保持既有倒计时策略。

| 门禁 | 零等待动作 |
| --- | --- |
| Inventory | 继续；此前输入解析和完整性错误仍失败 |
| Assembly 路线、Assembly/AutoHiC 参数 | 执行有效且绑定请求的推荐，保留尝试上限 |
| Hi-C、baseline GapCloser/BasePolish 结果 | 采用已完成并通过基本有效性校验的产物 |
| baseline T2T | 采用可选择的 patch；无可选择 patch 时沿用输入 |
| finishing / Dedup 顾问 | 有已生成候选时采用最近的可用候选；已尝试但无新候选时沿用阶段输入；首次执行前自动执行有效推荐；合法保留建议或预算耗尽时沿用输入；非法推荐报错 |
| Assembly Review | 完成评估和报告后采用当前交付候选；比较基线不替换当前候选，不调用 LLM 决定是否终止 |

基本有效性包括产物存在、哈希和来源绑定，以及组装 FASTA 非空、序列合法、标识不重复；不新增 BUSCO、QV、N50 等质量阈值。失败的执行、缺失/损坏输出或推荐调用错误不能伪装成保留源成功。零等待自动请求构造或应用失败时报错退出（非零退出码），保留原 checkpoint 以供排查，不悄悄打开人工菜单或消费失败请求。

finishing/Dedup 在零等待下完成一次尝试并收集评估后，生成明确的 `source=policy` 响应，采用最新有效候选或沿用阶段输入，不再依赖第二次 LLM 调用。预算耗尽同样生成明确策略记录。初始必要推荐仍调用模型；解析和阶段语义校验错误写入评审请求的 `recommendation_error`，不能当作合法保留建议。

最终 QUAST 与比较评估共用表结构校验；最终报告还检查基本组装指标、数值及单候选列。BUSCO 检查必要计数、百分比及计数自洽。合法缺测值保持缺测；损坏报告进入 collect 失败，不能标记为 available。Dedup 在发布候选前检查 FASTA 格式、字母表和标识唯一性。

`auto_decidable` 仅表示 LLM resolver 资格，不是所有自动动作的总开关。零等待策略由注册表的 `zero_wait_policy` 声明，检查投影同时给出该字段；执行前仍须验证不可变请求。

每次动作仍使用不可变 request → intent → application → checkpoint 协议，`decided_by=auto_rule`，保留原输入、候选、尝试历史、评估、采用/沿用记录。该决策只代表当前执行策略采用产物，不代表质量合格。已有暂停或终止会话不因修改等待时间自动重写历史。

LLM 的最终复核建议写入 notes，不能提交为终止决策；提示词不得要求它生成显式人工停止动作。非零等待路径遇到旧 `stop_human_review` 建议时保留暂停点供人工处理；只有显式人工停止可以将该审查终止。

<a id="parameter-approval-timeout-behavior"></a>

## 参数审批超时行为

只有参数推荐已解析、验证、持久化到完整性根目录下并绑定到不可变 `parameter_approval` 请求后才开始倒计时。它只能解决该精确推荐，不能生成、编辑、合并或替换参数。精确计时数值契约留在运行数据参考中。

一个共享超时请求规范化器强制 `decision=approve_parameters` 和 `decided_by=auto_rule`，补充审计元数据。它不改变状态或持久应用；公共意图/应用协议随后委托注册阶段门禁完成准备和验证后的纯 apply。Assembly、AutoHiC、T2T 保留各自请求、推荐 manifest、尝试链、前驱、工作流 occurrence 校验，超时处理不复制这些规则。

自动决策绑定不可变评审请求与推荐哈希，并记录前台倒计时来源、配置秒数和实际经过秒数。已认领门禁仍是人工决策。若自动规范化或事务准备在意图提交前失败，零等待运行报错退出并保留 checkpoint；非零等待且持久 checkpoint 仍可验证为 `blocked_human` 时，同一前台终端返回人工评审。已提交意图只能恢复，不能人工替换。请求、证据、checkpoint 或 occurrence 完整性损坏时必须拒绝，不伪造审批。

<a id="resume-validation"></a>

## 恢复校验

更改状态前，恢复重载当前 checkpoint，验证会话、工作流步骤身份、阶段、门禁、请求 ID 和哈希、决策词汇、manifest 身份及推荐/尝试绑定。缺失、过期、跨会话、冲突或已消费输入都拒绝。相同不可变字节的重复发布幂等，但不授权再次应用已消费门禁。

恢复不使用前次前台调用已过去的墙钟时间。它等待提供的人工决策（交互 `review` 或已验证决策文件），不把持久阻塞转为自动批准。只有恢复事务的可信来源和哈希绑定倒计时元数据识别当前 `foreground_countdown` 时，才接受倒计时触发的自动决策。决策来源和理由仍来自门禁 resolver（`llm_provisional` 或 `auto_rule`）；即使决策文件自称自动超时，人工 `review`/`resume` 来源也不能冒充。

恢复流程如下：

```text
获取活动 FrozenExecutionLease
  -> 读取 checkpoint 并投影共享评审状态
  -> 仅在 awaiting_decision 时规范化传入提案
  -> 持久化不可变意图
  -> gate prepare 创建类型化决策/证据效果
  -> 持久化并验证不可变应用
  -> 验证后的 gate apply 返回纯紧凑状态更新
  -> graph.update_state(..., as_node="<评审所属阶段>_stage")
  -> 重读 checkpoint 并证明应用已落入 checkpoint
  -> record_review_applied
  -> graph.invoke(None, ...)
  -> 到达终态时完成权威终态单例
  -> 记录恢复完成或失败
```

这是跨产物、Ledger 和 SQLite checkpoint 操作的幂等感知 saga，不是跨存储 ACID 事务。每个持久步骤都支持回放并验证前序证据。共享状态投影是恢复决策点：后续 `request=None` 调用恢复已提交或已准备应用的意图；只有 `awaiting_decision` 允许替换决策。

恢复更新必须显式归属于评审所属阶段节点，使其后续边返回 `route_controller`，由控制器结算已完成步骤并初始化下一步骤。阻塞运行的最后写入节点是 `route_controller`；让 LangGraph 自动推断恢复更新的节点会绕过控制器，直接调度尚未进入的下一阶段。

<a id="decision-effects"></a>

## 决策效果

决策可批准参数、接受/拒绝生物学候选、保留旧资源、授权有界重试或停止；不得静默创建未注册方法或绕过冻结尝试边界。

终态完成或失败可触发报告生成与 checkpoint 压缩。不可变评审请求、决策、执行证据及已采纳生物学产物独立于 checkpoint 历史保留。
