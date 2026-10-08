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
> **代码依据：** `app/review/gates.py`、`app/review/persistence.py`、`app/review/approval.py`、`app/review/recommender.py`、`app/llm/recommender.py`、`app/llm/json_response.py`、`app/control/resume_transaction.py`、`app/control/lifecycle.py`。

<a id="durable-gate-model"></a>

## 持久门禁模型

人工门禁是四状态持久协议：

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

计时器不提供默认决策。Assembly Review 保留证据资格与绑定 LLM 推荐产物；Assembly 路线选择使用注册的超时请求规范化器。Assembly、AutoHiC、T2T 参数审批使用同一注册规范化器，只绑定不可变请求中的推荐。没有合格规范化器的门禁保持人工。自动事务失败后，仅当 `review_status` 仍为 `awaiting_decision` 且同一已验证 `blocked_human` 请求仍存在时，才可返回人工评审。意图一旦提交，操作者不得替换；恢复以 `request=None` 重入事务并应用已提交意图。任何身份或完整性漂移都拒绝。回车认领门禁后取消计时器并打开正常人工提示。

计时器不是持久调度器：Ctrl-C、EOF 或进程退出保留请求和 `blocked_human` checkpoint。缺少 TTY 能力时，新运行在冻结前就失败。没有守护进程或后台唤醒。再次运行同一冻结会话会发现阻塞 checkpoint，不调用图，只打印人工恢复命令。后续 `review`/`resume` 始终是人工恢复，不从旧 `blocked_at` 重建或到期倒计时。接口不包含 strict/auto 评审模式或公开恢复超时覆盖。

<a id="parameter-approval-timeout-behavior"></a>

## 参数审批超时行为

只有参数推荐已解析、验证、持久化到完整性根目录下并绑定到不可变 `parameter_approval` 请求后才开始倒计时。它只能解决该精确推荐，不能生成、编辑、合并或替换参数。精确计时数值契约留在运行数据参考中。

一个共享超时请求规范化器强制 `decision=approve_parameters` 和 `decided_by=auto_rule`，补充审计元数据。它不改变状态或持久应用；公共意图/应用协议随后委托注册阶段门禁完成准备和验证后的纯 apply。Assembly、AutoHiC、T2T 保留各自请求、推荐 manifest、尝试链、前驱、工作流 occurrence 校验，超时处理不复制这些规则。

自动决策绑定不可变评审请求与推荐哈希，并记录前台倒计时来源、配置秒数和实际经过秒数。已认领门禁仍是人工决策。若自动规范化或事务准备在意图提交前失败，且持久 checkpoint 仍可验证为 `blocked_human`，同一前台终端返回人工评审。已提交意图只能恢复，不能人工替换。请求、证据、checkpoint 或 occurrence 完整性损坏时必须拒绝，不伪造审批。

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
  -> graph.update_state(...)
  -> 重读 checkpoint 并证明应用已落入 checkpoint
  -> record_review_applied
  -> graph.invoke(None, ...)
  -> 到达终态时完成权威终态单例
  -> 记录恢复完成或失败
```

这是跨产物、Ledger 和 SQLite checkpoint 操作的幂等感知 saga，不是跨存储 ACID 事务。每个持久步骤都支持回放并验证前序证据。共享状态投影是恢复决策点：后续 `request=None` 调用恢复已提交或已准备应用的意图；只有 `awaiting_decision` 允许替换决策。

<a id="decision-effects"></a>

## 决策效果

决策可批准参数、接受/拒绝生物学候选、保留旧资源、授权有界重试或停止；不得静默创建未注册方法或绕过冻结尝试边界。

终态完成或失败可触发报告生成与 checkpoint 压缩。不可变评审请求、决策、执行证据及已采纳生物学产物独立于 checkpoint 历史保留。
