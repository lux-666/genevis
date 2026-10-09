<a id="parameter-recommendation"></a>

# 参数推荐

> **状态：** Assembly、AutoHiC schema-3 推荐与 finishing 顾问已实现；T2T 单轮基线保留 schema-3 消费者。
>
> **契约：** [参数推荐](../reference/parameter-recommendation.md)、[评审](../reference/data-contract.md#12-review-contract)、[执行生命周期](../reference/execution-lifecycle.md)。
>
> **代码依据：** `app/domains/parameter_recommendation/`、`app/llm/parameters/`、`app/storage/parameter_evidence.py`、`app/stages/*/parameters/workflow.py`。

推荐器在方法选择之后运行。Assembly 等待已确认的 `assembly_selection`；固定方法阶段已知方法。规范事实和选定方法依次生成阶段专属请求、不可变模型证据、稀疏推荐、审批请求，最后生成执行使用的已批准 `ParameterAttempt`。

以下说明 schema-3 稀疏参数链的所有权，适用于 Assembly、AutoHiC、T2T 单轮基线及旧初始推荐记录。T2T 默认 advisor 使用 finishing 的完整参数/结果反馈契约：

- Assembly 提供路线、读段类型、目标层级和方法指导。
- AutoHiC 提供物种事实、Hi-C 上下文、限制性内切酶证据和可调整参数目录。
- T2T 提供物种事实、已解析端粒基序、长读段上下文和阈值目录。

精确 DTO 字段、稀疏响应校验、持久化、审批时机、重试语义和命令解析优先级由链接的参考契约定义。

Assembly 方法选择和参数推荐共用同一案例来源与检索器。跨组装器的广泛材料匹配先支持方法选择；方法确认后，直接案例、跨方法案例、软件与参数解释共同支持稀疏参数映射。参考工具参数始终保留其来源身份。

[Assembly 案例契约](../reference/casepack.md)拥有已实现的交换格式和切换边界；[架构一致性](../reference/conformance.md#assembly-software-explanation-coverage)记录剩余软件指导覆盖差距。审批和执行边界保持既定职责。Hi-C/T2T 的文献与方法扩展仍在规划中。Assembly Dedup 另有已实现的推荐与 QUAST 反馈循环，GapCloser/T2T/Base Polish 默认 advisor 也有 finishing 任务契约，复用模型、执行和审批组件，但这些任务契约都不应被误认为 schema-3 L2 参数事务；见[阶段契约](../reference/stage-contracts.md)。
