<a id="user-guide-index"></a>

# 用户指南索引

本页为安装 Gene Agent、准备运行、监控、评审关卡或恢复持久 session 的操作者提供导航。它仅为索引；可复制示例与链接的操作指南定义具体规程。

<a id="main-routes"></a>

## 主要路径

| 任务 | 从这里开始 | 按需继续 |
|---|---|---|
| 新主机或缺少工具环境 | [新主机安装](../deployment/migration.md) | 主机检查成功后阅读[运行与恢复](run-and-recovery.md) |
| 从 Mac checkout 同步到服务器 | [Mac 开发与服务器执行](../deployment/mac_dev_server_pull.md) | 实际 session 操作见[运行与恢复](run-and-recovery.md) |
| 从原始 reads 运行 contig 流程 | [运行与恢复](run-and-recovery.md) | [Contig manifest](../examples/contig_manifest.template.yaml) 与 [contig RunConfig](../examples/contig_run_config.template.json) |
| 评价已有组装 | [已有组装 manifest](../examples/existing_assembly_manifest.template.yaml) | [组装评价 RunConfig](../examples/assembly_review_run_config.template.json)，然后阅读[运行与恢复](run-and-recovery.md) |
| 理解或决定 LLM 参数 | [参数推荐](parameter_recommendation.md) | 需要精确字段或持久化规则时阅读[参数契约](../architecture/reference/parameter-recommendation.md) |
| 恢复阻塞或中断的 session | [运行与恢复](run-and-recovery.md) | 使用其 `review` 或决策文件 `resume` 路径，不要手工启动第二套工作流 |
| 使用重复阶段实例 | [重复阶段 RunConfig](../examples/repeated_stage_occurrences_run_config.template.json) | 检查与恢复见[运行与恢复](run-and-recovery.md) |

<a id="recommended-reading-order"></a>

## 推荐阅读顺序

全新服务器：

```text
新主机安装
  -> Mac 开发与服务器执行（如果刚拉取代码）
  -> 运行与恢复
  -> examples/
```

已有就绪 checkout：

```text
运行与恢复
  -> 所选入口模式对应的成对示例文件
  -> 出现审批关卡时阅读参数推荐
```

五阶段原始 reads contig 示例是该入口模式的权威可复制示例。已有组装评价与重复阶段示例分别对应独立用例，不是 contig 模板的替代版本。

<a id="boundary-with-architecture-references"></a>

## 与架构参考文档的边界

操作指南解释如何操作。架构参考文档定义冻结、进度、资源、执行证据、评审与恢复的精确契约。如果指南与参考文档不一致，以权威参考契约为准并报告文档漂移，不要另建本地变体。
