<a id="recommendation-policy-contract"></a>

# 推荐策略契约

> **状态：** Assembly 方法/路线顾问、Assembly 参数顾问和 AutoHiC 参数顾问已实现。
>
> **阅读场景：** 修改消融策略、确定性基线、顾问预检或推荐证据。
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

每个值为 `"advisor"` 或 `"deterministic_baseline"`。两者独立，使实验可以只消融一个顾问边界，不改变另一个；不存在通用 enabled 布尔开关。

`RunConfig.hic_scaffolding.parameter_advisor` 在 AutoHiC 参数边界使用同样两个值。AutoHiC 的阶段方法固定，因此没有方法选择顾问。

策略可以替换这两个顾问的提案来源。在顾问模式下，不能绕过方法/路线人工确认，也不能绕过参数审批。确定性参数基线记录自己的不可变策略决策和已批准尝试；它是独立策略路径，不是审批门禁旁路。

<a id="persisted-advisor-response-envelope"></a>

## 持久化顾问响应封装

Assembly 路线和参数顾问写入共用 `schema_version`、`raw_response`、`validity`、`error`、`model_id`。`app/llm/json_response.py` 拥有公共传输结构的构造与校验，包括 `validity` 的 `parsed` 和 `invalid` 值，但不拥有特定版本的回放兼容性。

schema-v1 路线恢复器将缺失或非字符串 `model_id` 规范化为 `null`；schema-v2 Assembly 参数恢复器拒绝非字符串 `model_id`。两者的 schema 专属恢复策略必须保持区别；两种 schema 的新写入均为字符串或 `null`。

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

Assembly/AutoHiC 的 advisor 使用 schema-3 稀疏参数事务；GapCloser/T2T/Base Polish 的 advisor 使用 finishing 完整提案、QUAST/原生证据反馈和 finishing_recommendation 人工门，持久布局见[运行数据](data-contract.md#finishing-task-evidence)。

Assembly、AutoHiC 与 T2T 的 `deterministic_baseline` 从同样已验证的事实、目录、上下文和依赖引用生成空 L2 的 schema-3 基线。它记录策略摘要、原本将发送的精确请求以及显式策略响应和解析记录；通过同一 schema-3 manifest 边界，以自动策略决策创建哈希绑定的已批准初始尝试。决策元数据使用 `source="policy"` 并记录完整自动解决审计，从而区别于前台倒计时。执行适配器使用基线解析器，此模式不得应用 L2 映射。AutoHiC 中操作者提供的限制性内切酶仍是 L1 事实，其他值取目录 L0 默认。T2T 基线要求已提供端粒基序，不调用阈值或基序模型，冻结目录 L0 后执行单轮，仍保留 t2t_review 人工采纳。初始基线策略不支持重试；重试请求必须拒绝，不得静默切回顾问模式。

<a id="readiness-and-evidence"></a>

## 就绪检查与证据

就绪检查遵循冻结策略。Assembly 两个顾问均为基线时不要求 LLM 端点；AutoHiC 参数顾问也为基线时，Hi-C 也不要求。T2T 显式基线也不要求端点，但必须提供基序；其 advisor 和其他仍使用顾问的阶段要求端点。精确结构化端点探测和失败不创建会话的语义由[执行生命周期](execution-lifecycle.md#whole-run-readiness-boundary)拥有。Assembly 游标持久化两个策略模式，参数基线显式标记每条跳过模型的记录，不能靠产物缺失来表达。

顾问模式中，方法请求携带显式 `evidence_status` 映射。标为 `"unavailable"` 的上游字段为 null，不得推断或当作观测；除非现有证据支持一条路线，否则提示词要求返回 `insufficient_data`。模型不能猜测恢复屏蔽字段；缺乏充分路线/读段兼容性却指定方法的响应必须校验失败。持久位置见[运行数据](data-contract.md#101-assembly-method-recommendation)。
