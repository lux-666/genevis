<a id="recommendation-policy-contract"></a>

# 推荐策略契约

> **状态：** Assembly 方法/路线顾问、Assembly 参数顾问和 AutoHiC 参数顾问已实现。
>
> **阅读场景：** 确定性基线、顾问预检或推荐证据。
>
> **输入：** 操作者草稿策略或结构化 RunConfig。
>
> **下游：** [运行数据](data-contract.md)、[阶段契约](stage-contracts.md)、[参数推荐](parameter-recommendation.md)、[TUI 与规划](../topics/tui-and-planning.md)。
>
> **代码依据：** `app/schemas/tui_intake.py`、`app/schemas/run_config.py`、`app/entrypoints/ask_planner.py`、`app/entrypoints/run.py`、`app/llm/json_response.py`、`app/stages/assembly/decision.py`、`app/llm/parameters/assembly/recommender.py`、`app/stages/assembly/nodes.py`、`app/stages/assembly/parameters/workflow.py`、`app/stages/hic_scaffolding/parameters/workflow.py`。

<a id="definition"></a>

## 定义

Assembly `RecommendationPolicy` 拥有两个独立且冻结的顾问模式：

```json
{
  "method_route_advisor": "advisor",
  "parameter_advisor": "advisor"
}
```

每个值为 `"advisor"` 或 `"deterministic_baseline"`，两者独立冻结。顾问模式调用阶段专属模型并经过相应评审门；确定性基线记录策略决策，不调用模型。不存在通用 enabled 布尔开关。

`RunConfig.hic_scaffolding.parameter_advisor` 在 AutoHiC 参数边界使用同样两个值。AutoHiC 的阶段方法固定，因此没有方法选择顾问。

策略选择 Assembly/AutoHiC 的 advisor 或 deterministic_baseline。Assembly/AutoHiC advisor 使用 schema-3 模型请求、响应、解析和持久参数审批，人工或自动批准遵循[等待模式矩阵](../topics/review-and-resume.md#foreground-review-policy)；对应 baseline 同样保存精确模型请求格式，但生成空 L2 策略响应并自动批准，不调用模型。确定性参数基线是独立策略路径，不是审批门禁旁路。

<a id="persisted-advisor-response-envelope"></a>

## 持久化顾问响应封装

Assembly 路线和参数顾问写入共用 `schema_version`、`raw_response`、`validity`、`error`、`model_id` 与 `model_call`。`app/llm/json_response.py` 拥有公共传输结构的构造与严格校验，包括 `validity` 的 `parsed` 和 `invalid` 值。`model_id` 只接受字符串或 `null`，不把客户端类名当作模型身份；`model_call` 分别记录配置模型、服务返回模型和调用标识。恢复器校验所属契约的版本，恢复已保存的调用身份，不重新调用模型或改写来源。

<a id="draft-and-freeze-boundary"></a>

## 草稿与冻结边界

可变 TUI 草稿在规划前携带 `recommendation_policy`，经 `entry_request.recommendation_policy` 投影给规划器，并在整次运行冻结时编译进 `RunConfig.assembly`。冻结的 Assembly 字段为 `method_route_advisor`、`parameter_advisor`；结构化 RunConfig 还冻结 `hic_scaffolding.parameter_advisor`。冻结后不可改变策略。

策略不得把 Assembly 方法或参数重新塞回 RunConfig。方法身份仍属阶段；策略选择谁提出方法或参数映射，不是可执行答案。

规划 schema 不含 `method_selection` 澄清类型。草稿没有用户可编辑的 Assembly 方法名单：固定阶段使用系统默认，Assembly 在阶段推荐边界绑定。输入中的工具名识别和伴随读段提示只存在于输入函数局部变量，不是持久规划字段。

<a id="methodroute-advisor"></a>

## 方法与路线顾问

[Assembly 案例消费者](casepack.md)定义已实现的顾问证据策略：跨方法的材料/任务匹配与软件能力共同支持初次选择。成对性能比较可以提供支持，但不是给出建议的前提。参数决策在方法确认之后，可使用其他方法的案例而不改变确认的可执行方法。这改变顾问证据，不改变本页定义的独立顾问/基线模式。

`advisor` 使用常规事务：在 QC/Survey 证据具备后，先持久化精确模型请求、原始响应、解析推荐和人工确认，再绑定 `assembly_selection`。

`deterministic_baseline` 跳过模型调用，需要明确路线、规范共享读段类型或单一已注册输入平台。只有描述符支持的兼容集合包含该读段类型的默认方法时，才可选择它。混合输入缺乏充分确定性证据时必须拒绝。选择以策略决策写入，标记 `method_recommendation_mode="deterministic_baseline"`，不打开方法顾问人工门禁。

<a id="parameter-advisor"></a>

## 参数顾问

Assembly/AutoHiC 的 advisor 使用 schema-3 稀疏参数事务；GapCloser/T2T/Base Polish 的 advisor 使用 finishing 完整提案、QUAST/原生证据反馈和 finishing_recommendation 持久门，持久布局见[运行数据](data-contract.md#finishing-task-evidence)。

Assembly、AutoHiC 与 T2T 的 `deterministic_baseline` 从同样已验证的事实、目录、上下文和依赖引用生成空 L2 的 schema-3 策略证据与批准记录。Assembly/AutoHiC 保存渲染请求和模型格式的策略响应；T2T 使用纯策略请求/响应，精确布局由[参数推荐契约](parameter-recommendation.md#durable-records-and-checkpoints)拥有。它们记录策略摘要、请求和解析记录，并通过同一 schema-3 manifest 边界，以 `source="policy"` 的自动策略决策创建哈希绑定的已批准初始尝试。执行适配器使用基线解析器，此模式不得应用 L2 映射。AutoHiC 中操作者提供的限制性内切酶仍是 L1 事实，其他值取目录 L0 默认。T2T baseline 要求已提供端粒基序，不调用阈值或基序模型，冻结目录 L0 后执行单轮，仍保留 `t2t_review` 持久采纳门，采用行为遵循[等待模式矩阵](../topics/review-and-resume.md#foreground-review-policy)；默认 T2T advisor 则只使用 finishing 任务契约。初始基线策略不支持重试；重试请求必须拒绝，不得切换到 advisor。

<a id="readiness-and-evidence"></a>

## 就绪检查与证据

就绪检查遵循冻结策略。Assembly 两个顾问均为基线时不要求 LLM 端点；AutoHiC 参数顾问也为基线时，Hi-C 也不要求。T2T 显式基线也不要求端点，但必须提供基序；其 advisor 和其他仍使用顾问的阶段要求端点。精确结构化端点探测和失败不创建会话的语义由[执行生命周期](execution-lifecycle.md#whole-run-readiness-boundary)拥有。Assembly 游标持久化两个策略模式，参数基线显式标记每条跳过模型的记录，不能靠产物缺失来表达。 Assembly Review 的 LLM 建议是可选自动化能力，不是所有运行的冻结前硬依赖；正数等待且证据完整时倒计时可以触发建议，端点不可用或建议无效时回退人工恢复；零等待按明确策略完成，不调用该模型。

顾问模式中，方法请求携带显式 `evidence_status` 映射。标为 `"unavailable"` 的上游字段为 null，不得推断或当作观测；除非现有证据支持一条路线，否则提示词要求返回 `insufficient_data`。模型不能猜测恢复屏蔽字段；缺乏充分路线/读段兼容性却指定方法的响应必须校验失败。持久位置见[运行数据](data-contract.md#101-assembly-method-recommendation)。

Assembly 路线与读段类型共用 `app/domains/assembly/selection.py` 的映射：`clr` 对应 `pacbio_clr`，`ont` 同时支持 `ont_raw` 和 `ont_hq`，推荐与恢复必须保留具体类型。请求菜单只列当前声明输入支持的路线；参数推荐上下文、产物与 checkpoint 使用同一组路线类型。旧冻结请求不能追加选项：仅在当前输入明确只有 CLR、原请求没有 `select_clr` 时，旧 `select_cyclone` 可作为 CLR 的人工恢复动作，交付仍记录 `clr`/`pacbio_clr`，不改写请求。

在非零等待模式下，注册超时 normalizer 不等于每份请求都能自动执行。Assembly 必须有有效、类型与路线一致且被当前请求允许的推荐；Dedup 和参数审批必须允许各自的推荐执行或批准动作。空推荐、无效推荐或仅允许人工结果选择的请求直接进入人工路径，不用默认选项触发失败的自动事务。有效推荐仍遵循配置的倒计时；其他人工采纳门的既有审批策略不变。

零等待在 finishing/Dedup 已完成尝试后直接生成 `source=policy` 的响应与采用/保留决策，不再调用结果顾问；原始输入、候选和评估仍保留。初始必要参数推荐的解析或阶段语义错误仍报错。零等待（`max_blocked_seconds: 0`）采用[明确推进策略](../topics/review-and-resume.md)：已有可用候选优先采用；无候选时执行有效推荐或沿用输入；最终 Review 评估并生成报告，质量建议不作终止授权。上文有关证据资格和人工回退的描述适用于非零等待。

<a id="finishing-diagnostics-and-method-boundary"></a>

Finishing 顾问的完整提案还可保存三类策略信息：`diagnostics` 按目标绑定已有产物和不确定性，`methods` 引用注册表中的方法适用性，`expected_new_evidence` 说明重试预期新增的证据。它们进入不可变 recommendation response 和 attempt assessment，供恢复与后续顾问读取；原始模型输出不被重新解释为执行计划。当前 T2T 执行器仍固定为 `t2t.alignment.minimap2`，因此 `switch_method` 与 `next_cluster` 只作为未实现动作被拒绝。新增方法必须先进入 descriptor registry、执行生命周期和采纳契约，再允许顾问选择；不能仅增加一个模型枚举值。
