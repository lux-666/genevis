<a id="execution-evidence-contract"></a>

# 执行证据契约

> **状态：** 已实现。
>
> **阅读场景：** 精确区分运行时结果、持久化进程结果、日志和结果游标。
>
> **来源：** [执行](../topics/execution.md)。
>
> **下游：** [数据与存储](../topics/data-and-storage.md)、评审与恢复。
>
> **相关契约：** [执行生命周期](execution-lifecycle.md)、[运行数据](data-contract.md)。
>
> **代码依据：** `app/schemas/execution.py`、`app/execution/evidence.py`、`app/storage/publish.py`。

<a id="contract"></a>

## 契约

进程结果只有一种规范持久表示。进程运行期间，运行时代码和阶段游标可使用本地或绝对路径；规范执行证据正文不得持久化 `ExecutionResult` 路径字段。冻结命令输入及部分阶段局部投影属于独立运行时/依赖记录。

```text
LocalRunner -> ExecutionResult（运行时路径）
            -> stdout/stderr 各发布并索引一次
            -> ExecutionEvidence（仅 ArtifactRef）
            -> 不可变执行记录
            -> checkpoint 保存 ArtifactRef 及可选的有界投影
```

`ExecutionResult` 是运行时值。`stdout_path`、`stderr_path`、`output_paths` 只供 Runner 和直接发布者移动或检查文件，不能直接序列化到持久状态。

`ExecutionEvidence` 是规范持久结果正文：

```json
{
  "attempt_id": "session:<workflow_step_id>:attempt-000",
  "success": true,
  "return_code": 0,
  "status": "ok",
  "status_detail": "",
  "error": "",
  "stdout_ref": {
    "schema_version": 2,
    "artifact_id": "...",
    "artifact_type": "command.stdout",
    "path_ref": "artifacts/steps/<workflow_step_id>/<stage>/attempts/attempt-000/invocation.stdout.log",
    "sha256": "...",
    "size_bytes": 0
  },
  "stderr_ref": {
    "schema_version": 2,
    "artifact_id": "...",
    "artifact_type": "command.stderr",
    "path_ref": "artifacts/steps/<workflow_step_id>/<stage>/attempts/attempt-000/invocation.stderr.log",
    "sha256": "...",
    "size_bytes": 0
  }
}
```

模型禁止未知字段，尤其 `stdout_path`、`stderr_path`、`output_paths` 是非法持久字段，不是兼容别名。可选有界失败摘要由 `error` 拥有；stderr 仍是权威进程输出。

<a id="ownership-and-lifecycle"></a>

## 所有权与生命周期

- 共享方法 Runner 执行后立即将 stdout/stderr 发布到运行目录并登记索引，失败时也一样。
- 执行证据边界将已索引日志转换为 `ArtifactRef`，写入或加载不可变执行记录前验证两项引用。
- 外层执行记录将规范正文绑定到精确 `frozen_attempt_ref`，并记录生命周期是否遇到边界失败；这些字段不扩展 `ExecutionEvidence` 本身。
- 阶段可用专属不可变记录包装规范结果正文，但不可修改或扩展正文 schema。
- 人工批准后的模型推荐参数化执行中，当前及历史执行记录引用仅属于对应 `ParameterAttempt`；阶段游标按 ID 选择尝试，不复制方法专属执行游标。普通方法及有序批次通过共享类型化 `execution_attempts` 游标绑定冻结和执行引用。成功标志、返回码、有界错误等小型不可变投影可用于路由和展示；执行记录仍是权威。
- Collect 仅在需要日志字节时解析引用；它注册生物学输出、报告和其他阶段产物，不重复注册 Runner 日志。
- 候选和评审记录可投影日志 `path_ref` 供展示。绝对路径或工作目录路径不是规范执行证据，尽管部分阶段 checkpoint 仍保留这类运行时投影。

冻结 Runner 输入是独立的执行前事实。恢复从不可变引用加载，禁止从当前目录、配置或环境重新生成命令。记录包含 Runner 路径、环境投影、预期输出和 Conda 选择。脚本使用尝试工作目录内保留的绝对路径，并绑定 SHA-256 摘要。工作目录和预期输出必须限制在当前运行内；运行内脚本符号链接可解析到同一运行的不可变产物。脚本缺失或改变、路径越界、校验和漂移均拒绝。恢复不得依据当前配置重建命令。

模型推荐的 Assembly、AutoHiC、T2T 执行中，同一 `ParameterAttempt` 同时拥有 `frozen_attempt_ref` 和后续 `execution_ref`。封存时将两者绑定进不可变 manifest；重试推进当前尝试后，仍可确定早先结果对应哪份 Runner 输入。

<a id="loader-boundary"></a>

## 加载边界

加载器只接受基于 ArtifactRef 的执行证据，拒绝基于路径的旧结果。恢复不猜测引用，也不改写证据格式。

<a id="enforced-constraints"></a>

## 强制约束

1. 每份持久执行结果都通过 `ExecutionEvidence` 校验。
2. 阶段不得将 `ExecutionResult.model_dump()` 序列化到 checkpoint。
3. 阶段不得读取持久化 `stdout_path`、`stderr_path`、`output_paths`。
4. stdout/stderr 引用缺失、不匹配或校验和无效时拒绝。
5. 每份命令日志仅登记一次产物索引，由共享 Runner 路径拥有。
6. 每份执行记录恰好绑定一个冻结尝试引用；恢复要求精确依赖，拒绝歧义记录。
